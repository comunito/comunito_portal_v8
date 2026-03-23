from __future__ import annotations

import base64
import copy
import csv
import glob
import json
import os
import queue
import re
import socket
import subprocess
import threading
import time
from collections import OrderedDict
from io import StringIO
from typing import Any

import cv2
import numpy as np
import requests

from app.portal_v8_models import canon_plate, normalize_wl_section, normalize_tag_section, normalize_wh_pair

try:
    import serial
except Exception:
    serial = None

try:
    from fast_alpr import ALPR
    _FAST_ALPR_OK = True
except Exception:
    ALPR = None
    _FAST_ALPR_OK = False


# ============================================================
# Runtime context
# ============================================================

class RuntimeContext:
    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.lock = threading.RLock()

        self.alpr = None
        self.alpr_ok = False

        self._ip_cache = {"mac2ip": {}, "ts": 0.0}

        # Camera registry: key = "entry:1:1"
        self.video_sources: dict[str, VideoSource] = {}
        self.motion_states: dict[str, MotionState] = {}
        self.send_managers: dict[str, SendManager] = {}
        self.camera_states: dict[str, dict[str, Any]] = {}
        self.camera_state_locks: dict[str, threading.Lock] = {}
        self.camera_threads_started: set[str] = set()
        self.last_auth_ts: dict[str, float] = {}
        self.stable_state: dict[str, dict[str, Any]] = {}
        self.last_sent_value: dict[str, dict[str, str]] = {}
        self.last_sent_ts: dict[str, dict[str, float]] = {}
        self.send_lock: dict[str, threading.Lock] = {}

        # Bases por lado
        self.side_indexes: dict[str, dict[str, Any]] = {
            "entry": self._empty_side_index(),
            "exit": self._empty_side_index(),
        }

        self.last_wl_refresh: dict[str, dict[str, float]] = {
            "entry": {"owners": 0.0, "visitors": 0.0, "tags": 0.0},
            "exit": {"owners": 0.0, "visitors": 0.0, "tags": 0.0},
        }

        self.sys_status = {"temp_c": None, "cpu_pct": 0.0}
        self.cpu_prev = (0, 0)

        self.hb_status = {
            "last_try_ts": 0.0,
            "last_ok_ts": 0.0,
            "last_code": None,
            "last_err": "",
            "sent": 0,
            "fail": 0,
            "dropped": 0,
            "pending": 0,
        }

        self.gate_serial = GateSerialManager(self)

        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = (
            "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay|"
            "reorder_queue_size;0|max_delay;0|stimeout;3000000"
        )

    def _empty_side_index(self):
        return {
            "owners": {},
            "visitors": {},
            "tags_owners": {},
        }

    def camera_key(self, side: str, lane_no: int, cam_no: int) -> str:
        return f"{side}:{lane_no}:{cam_no}"

    def parse_camera_key(self, camera_key: str) -> tuple[str, int, int]:
        side, lane_no, cam_no = camera_key.split(":")
        return side, int(lane_no), int(cam_no)

    def get_side_cfg(self, side: str) -> dict[str, Any]:
        return self.cfg[side]

    def get_lane_cfg(self, side: str, lane_no: int) -> dict[str, Any]:
        return self.cfg[side]["lanes"][lane_no - 1]

    def get_camera_cfg(self, side: str, lane_no: int, cam_no: int) -> dict[str, Any]:
        return self.cfg[side]["lanes"][lane_no - 1]["cameras"][cam_no - 1]

    def iter_enabled_cameras(self):
        for side in ("entry", "exit"):
            side_cfg = self.get_side_cfg(side)
            if not side_cfg.get("enabled", True):
                continue
            for lane_no, lane in enumerate(side_cfg.get("lanes", []), start=1):
                if not lane.get("enabled", False):
                    continue
                for cam_no, cam in enumerate(lane.get("cameras", []), start=1):
                    if not cam.get("enabled", False):
                        continue
                    yield side, lane_no, cam_no, cam

    def ensure_camera_runtime_objects(self, camera_key: str):
        if camera_key not in self.video_sources:
            self.video_sources[camera_key] = VideoSource(self, camera_key)
        if camera_key not in self.motion_states:
            self.motion_states[camera_key] = MotionState()
        if camera_key not in self.send_managers:
            self.send_managers[camera_key] = SendManager(self, camera_key)
        if camera_key not in self.camera_state_locks:
            self.camera_state_locks[camera_key] = threading.Lock()
        if camera_key not in self.camera_states:
            self.camera_states[camera_key] = {
                "plate": "",
                "conf": 0.0,
                "ts": 0.0,
                "raw_candidates": [],
                "raw_ts": 0.0,
                "display": ["", "", ""],
                "titles": ["Folio", "Nombre", "Telefono"],
                "auth": False,
                "cat": "NONE",
                "user_type": "NONE",
                "tag": "",
                "tag_ts": 0.0,
                "tag_cat": "NONE",
                "tag_fields": ["", "", ""],
            }
        if camera_key not in self.last_auth_ts:
            self.last_auth_ts[camera_key] = 0.0
        if camera_key not in self.stable_state:
            self.stable_state[camera_key] = {"last": "", "hits": 0}
        if camera_key not in self.last_sent_value:
            self.last_sent_value[camera_key] = {"ACTIVE": "", "INACTIVE": "", "NOTFOUND": ""}
        if camera_key not in self.last_sent_ts:
            self.last_sent_ts[camera_key] = {"ACTIVE": 0.0, "INACTIVE": 0.0, "NOTFOUND": 0.0}
        if camera_key not in self.send_lock:
            self.send_lock[camera_key] = threading.Lock()

    def init_alpr(self):
        if self.alpr_ok:
            return
        if not _FAST_ALPR_OK or ALPR is None:
            self.alpr_ok = False
            self.alpr = None
            return
        try:
            self.alpr = ALPR(
                detector_model="yolo-v9-t-384-license-plate-end2end",
                ocr_model="cct-xs-v1-global-model",
            )
            self.alpr_ok = True
        except Exception:
            self.alpr = None
            self.alpr_ok = False

    def refresh_runtime_registry(self):
        seen: set[str] = set()
        for side, lane_no, cam_no, _cam in self.iter_enabled_cameras():
            ck = self.camera_key(side, lane_no, cam_no)
            seen.add(ck)
            self.ensure_camera_runtime_objects(ck)
            if ck not in self.camera_threads_started:
                self.video_sources[ck].start()
                threading.Thread(target=motion_loop, args=(self, ck), daemon=True).start()
                threading.Thread(target=alpr_loop, args=(self, ck), daemon=True).start()
                self.camera_threads_started.add(ck)


# ============================================================
# General helpers
# ============================================================

def side_label(side: str) -> str:
    return "Entrada" if side == "entry" else "Salida"


def safe_row_val(row: list[str] | None, one_based_idx: int | None) -> str:
    if one_based_idx is None:
        return ""
    i = int(one_based_idx) - 1
    if not row or i < 0 or i >= len(row):
        return ""
    return (row[i] or "").strip()


def extract_fields(row: list[str] | None, cols: list[int | None]) -> list[str]:
    cols = cols or [2, 3, 4]
    c1, c2, c3 = (cols + [None, None, None])[:3]
    return [
        safe_row_val(row, c1),
        safe_row_val(row, c2),
        safe_row_val(row, c3),
    ]


def payload_kv_from_titles(titles: list[str], values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, (t, v) in enumerate(zip(titles, values), start=1):
        key = re.sub(r"[^a-z0-9]+", "_", (t or "").strip().lower()).strip("_")
        if not key:
            key = f"campo_{i}"
        out[key] = v
    return out


def guess_has_header(rows: list[list[str]]) -> bool:
    if not rows:
        return False
    header = rows[0]
    header_join = " ".join((header or []))[:128].upper()
    if any(tok in header_join for tok in ("PLACA", "PLATE", "ESTATUS", "STATUS", "NOMBRE", "FOLIO", "TAG", "PHYSICAL", "INTERNAL")):
        return True
    return any(header)


def parse_csv_text(txt: str) -> list[list[str]]:
    try:
        f = StringIO(txt)
        return list(csv.reader(f))
    except Exception:
        return []


def gs_url(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if "http" in s:
        if ("/export?" in s) and ("format=csv" in s):
            return s
        p = s.find("/d/")
        if p >= 0:
            p += 3
            q = s.find("/", p)
            sheet_id = s[p:q] if q > p else s[p:]
            return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
        return s
    return f"https://docs.google.com/spreadsheets/d/{s}/export?format=csv"


def is_active_from_row(section: dict[str, Any], row: list[str] | None) -> bool:
    idx = int(section.get("status_col", 3)) - 1
    val = (row[idx] if (row and 0 <= idx < len(row)) else "") or ""
    v = str(val).strip().upper()
    v = re.sub(r"[^A-Z0-9ÁÉÍÓÚÑ ]+", " ", v)
    v = re.sub(r"\s+", " ", v).strip()
    if v.startswith("ACTIV") or v.startswith("ACTIVE") or v == "ACT":
        return True
    if v in ("1", "SI", "SÍ", "YES", "Y", "TRUE", "T", "ON"):
        return True
    if v.isdigit() and v == "1":
        return True
    return False


# ============================================================
# MAC -> IP / connectivity
# ============================================================

MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def resolve_ip_by_mac(rt: RuntimeContext, mac: str, ttl: float = 1.5) -> str | None:
    mac = (mac or "").upper()
    now = time.time()
    if not MAC_RE.match(mac):
        return None

    if (now - rt._ip_cache["ts"]) < ttl:
        ip = rt._ip_cache["mac2ip"].get(mac)
        if ip:
            return ip

    try:
        with open("/var/lib/misc/dnsmasq.leases", "r", encoding="utf-8") as f:
            for line in f:
                p = line.strip().split()
                if len(p) >= 3 and p[1].upper() == mac:
                    ip = p[2].strip()
                    if ip:
                        rt._ip_cache["mac2ip"][mac] = ip
                        rt._ip_cache["ts"] = now
                        return ip
    except Exception:
        pass

    try:
        with open("/proc/net/arp", "r", encoding="utf-8") as f:
            next(f)
            for ln in f:
                cols = ln.split()
                if len(cols) >= 4 and cols[3].upper() == mac:
                    ip = cols[0]
                    rt._ip_cache["mac2ip"][mac] = ip
                    rt._ip_cache["ts"] = now
                    return ip
    except Exception:
        pass

    return None


def materialize_camera_url(rt: RuntimeContext, camera_cfg: dict[str, Any]) -> tuple[str, str | None, str]:
    runtime = camera_cfg["runtime"]
    url = (runtime.get("camera_url", "") or "").strip()
    mode = (runtime.get("camera_mode", "mac") or "mac").lower()

    if mode == "mac" and "{CAM_IP}" in url:
        ip = resolve_ip_by_mac(rt, runtime.get("camera_mac", ""))
        if ip:
            return url.replace("{CAM_IP}", ip), ip, "LAN-MAC"
        return url, None, "LAN-MAC(PEND)"

    ip = None
    try:
        ip = url.split("@")[1].split(":")[0]
    except Exception:
        ip = None
    return url, ip, "MANUAL"


def ping_ip(ip: str, timeout: int = 1) -> bool:
    if not ip:
        return False
    try:
        subprocess.check_output(["ping", "-c", "1", "-W", str(timeout), ip], stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


# ============================================================
# Video source
# ============================================================

class VideoSource:
    def __init__(self, rt: RuntimeContext, camera_key: str):
        self.rt = rt
        self.camera_key = camera_key
        self.lock = threading.Lock()
        self.frame = None
        self.ts = 0.0
        self.running = False
        self.thread: threading.Thread | None = None
        self.last_ip = None

    def get(self):
        with self.lock:
            return self.frame

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _open_cv(self, url: str):
        return cv2.VideoCapture(url)

    def _loop(self):
        try:
            cv2.setNumThreads(1)
        except Exception:
            pass

        while self.running:
            side, lane_no, cam_no = self.rt.parse_camera_key(self.camera_key)
            camera_cfg = self.rt.get_camera_cfg(side, lane_no, cam_no)
            runtime = camera_cfg["runtime"]

            url, ip, _mode = materialize_camera_url(self.rt, camera_cfg)

            if "{CAM_IP}" in (url or ""):
                time.sleep(0.5)
                continue

            if ip:
                self.last_ip = ip

            if self.last_ip and not ping_ip(self.last_ip, 1):
                time.sleep(0.5)
                continue

            cap = None
            try:
                cap = self._open_cv(url)
                if not cap or not cap.isOpened():
                    time.sleep(0.6)
                    continue

                last = time.time()
                while self.running:
                    ok, fr = cap.read()
                    if not ok or fr is None:
                        break

                    try:
                        mx = int(runtime.get("resize_max_w", 1280))
                        if mx and fr.shape[1] > mx:
                            h, w = fr.shape[:2]
                            tw = mx
                            th = int(max(36, h * (tw / float(w))))
                            fr = cv2.resize(fr, (tw, th), interpolation=cv2.INTER_AREA)
                    except Exception:
                        pass

                    with self.lock:
                        self.frame = fr
                        self.ts = time.time()

                    if (time.time() - last) > 2.0:
                        last = time.time()
                        url2, ip2, _ = materialize_camera_url(self.rt, camera_cfg)
                        if ip2 and self.last_ip and ip2 != self.last_ip:
                            break
                    time.sleep(0.001)
            except Exception:
                pass
            finally:
                try:
                    if cap:
                        cap.release()
                except Exception:
                    pass
            time.sleep(0.3)


# ============================================================
# ALPR
# ============================================================

def preprocess_for_alpr(camera_cfg: dict[str, Any], frame_bgr):
    try:
        runtime = camera_cfg["runtime"]
        if not runtime.get("pp_enabled", False):
            return frame_bgr
        prof = (runtime.get("pp_profile", "none") or "none").strip().lower()
        if prof == "none" or frame_bgr is None:
            return frame_bgr

        h, w = frame_bgr.shape[:2]
        if h < 20 or w < 20:
            return frame_bgr

        if prof == "bw_hicontrast_sharp":
            clip = float(runtime.get("pp_clahe_clip", 2.0))
            sharp = float(runtime.get("pp_sharp_strength", 0.55))

            try:
                g = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
            except Exception:
                g = frame_bgr

            try:
                clahe = cv2.createCLAHE(clipLimit=float(clip), tileGridSize=(8, 8))
                g = clahe.apply(g)
            except Exception:
                pass

            try:
                if float(sharp) > 0.001:
                    blur = cv2.GaussianBlur(g, (0, 0), 1.0)
                    w1 = 1.0 + float(sharp)
                    w2 = -float(sharp)
                    g = cv2.addWeighted(g, w1, blur, w2, 0)
            except Exception:
                pass

            try:
                return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)
            except Exception:
                return frame_bgr

        return frame_bgr
    except Exception:
        return frame_bgr


def run_alpr(rt: RuntimeContext, image_bgr, resize_max_w: int, topk: int = 3):
    if not rt.alpr_ok or image_bgr is None or rt.alpr is None:
        return []

    h0, w0 = image_bgr.shape[:2]
    if w0 < 2 or h0 < 2:
        return []

    def best_conf(v):
        if v is None:
            return 0.0
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, (list, tuple)):
            vals = []
            for x in v:
                try:
                    vals.append(float(x))
                except Exception:
                    pass
            return max(vals) if vals else 0.0
        try:
            return float(v)
        except Exception:
            return 0.0

    img = image_bgr
    target_w = max(64, int(resize_max_w))
    if target_w < w0:
        scale = max(1e-6, float(target_w) / float(w0))
        try:
            img = cv2.resize(
                image_bgr,
                (max(64, int(w0 * scale)), max(36, int(h0 * scale))),
                interpolation=cv2.INTER_AREA,
            )
        except Exception:
            img = image_bgr

    try:
        res = rt.alpr.predict(img) or []
    except Exception:
        return []

    out = []
    for r in res:
        det = getattr(r, "detection", None)
        ocr = getattr(r, "ocr", None)
        if det is None or ocr is None:
            continue

        det_conf = best_conf(getattr(det, "confidence", None))
        if det_conf <= 0.0:
            det_conf = best_conf(getattr(det, "score", None))

        raw_text = getattr(ocr, "text", "")
        raw_conf = getattr(ocr, "confidence", 0.0)

        if isinstance(raw_text, (list, tuple)):
            conf_list = raw_conf if isinstance(raw_conf, (list, tuple)) else [raw_conf] * len(raw_text)
            for t, c in zip(raw_text, conf_list):
                tt = str(t or "").strip().upper()
                cc = best_conf(c)
                if tt:
                    out.append((tt, cc, det_conf))
            continue

        text = str(raw_text or "").strip().upper()
        conf = best_conf(raw_conf)
        if text:
            out.append((text, conf, det_conf))

    out.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return out[: max(1, topk)]


# ============================================================
# ROI + motion
# ============================================================

class MotionState:
    def __init__(self):
        self.baseline = None
        self.last_base_ts = 0.0
        self.active = False
        self.last_motion_ts = 0.0
        self.trigger = threading.Event()
        self.last_ratio = 0.0


def apply_roi(camera_cfg: dict[str, Any], frame):
    roi = camera_cfg["runtime"].get("roi", {})
    if not roi.get("enabled"):
        return frame
    h, w = frame.shape[:2]
    x = max(0.0, min(1.0, float(roi.get("x", 0.0))))
    y = max(0.0, min(1.0, float(roi.get("y", 0.0))))
    ww = max(0.0, min(1.0, float(roi.get("w", 1.0))))
    hh = max(0.0, min(1.0, float(roi.get("h", 1.0))))
    if ww <= 0 or hh <= 0:
        return frame

    x0 = int(round(x * w))
    y0 = int(round(y * h))
    x1 = int(round((x + ww) * w))
    y1 = int(round((y + hh) * h))

    x0 = max(0, min(w - 1, x0))
    x1 = max(1, min(w, x1))
    y0 = max(0, min(h - 1, y0))
    y1 = max(1, min(h, y1))

    if x1 - x0 < 8 or y1 - y0 < 8:
        return frame
    return frame[y0:y1, x0:x1]


def roi_gray_small(camera_cfg: dict[str, Any], frame):
    fr = apply_roi(camera_cfg, frame)
    if fr is None:
        return None
    try:
        g = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY)
    except Exception:
        g = fr
    h, w = g.shape[:2]
    if w > 0:
        tw = min(320, w)
        if tw < w:
            th = int(max(32, h * (tw / float(w))))
            g = cv2.resize(g, (tw, th), interpolation=cv2.INTER_AREA)
    return g


def build_baseline(rt: RuntimeContext, camera_key: str):
    side, lane_no, cam_no = rt.parse_camera_key(camera_key)
    camera_cfg = rt.get_camera_cfg(side, lane_no, cam_no)
    motion_cfg = camera_cfg["runtime"]["motion"]
    n = int(motion_cfg.get("autobase_samples", 3))
    dt = float(motion_cfg.get("autobase_interval_s", 1.0))
    samples = []
    vs = rt.video_sources[camera_key]
    for _ in range(max(1, n)):
        fr = vs.get()
        if fr is not None:
            g = roi_gray_small(camera_cfg, fr)
            if g is not None:
                samples.append(g)
        time.sleep(dt)
    if not samples:
        return None
    min_h = min(s.shape[0] for s in samples)
    min_w = min(s.shape[1] for s in samples)
    stack = np.stack([s[:min_h, :min_w] for s in samples], axis=0)
    base_img = np.median(stack, axis=0).astype(np.uint8)
    st = rt.motion_states[camera_key]
    st.baseline = base_img
    st.last_base_ts = time.time()
    return base_img


def motion_ratio(rt: RuntimeContext, camera_key: str, camera_cfg: dict[str, Any], gray) -> float:
    st = rt.motion_states[camera_key]
    base_img = st.baseline
    if base_img is None:
        return 0.0
    h = min(gray.shape[0], base_img.shape[0])
    w = min(gray.shape[1], base_img.shape[1])
    if h < 8 or w < 8:
        return 0.0
    a = gray[:h, :w]
    b = base_img[:h, :w]
    d = cv2.absdiff(a, b)
    thr = int(camera_cfg["runtime"]["motion"].get("intensity_delta", 25))
    _, bw = cv2.threshold(d, thr, 255, cv2.THRESH_BINARY)
    changed = int(np.count_nonzero(bw))
    total = bw.size
    return 100.0 * changed / float(total)


def motion_loop(rt: RuntimeContext, camera_key: str):
    build_baseline(rt, camera_key)
    st = rt.motion_states[camera_key]
    last_check = 0.0

    while True:
        try:
            side, lane_no, cam_no = rt.parse_camera_key(camera_key)
            camera_cfg = rt.get_camera_cfg(side, lane_no, cam_no)
            motion_cfg = camera_cfg["runtime"]["motion"]

            if not camera_cfg.get("enabled", False):
                st.active = False
                time.sleep(0.3)
                continue

            if not motion_cfg.get("enabled", True):
                st.active = True
                time.sleep(0.2)
                continue

            vs = rt.video_sources[camera_key]
            fr = vs.get()
            if fr is None:
                time.sleep(0.05)
                continue

            now = time.time()
            period_base = float(max(1, motion_cfg.get("autobase_every_min", 10))) * 60.0
            cooldown = float(max(0.2, motion_cfg.get("cooldown_s", 2.0)))

            if (st.baseline is None) or ((now - st.last_base_ts) >= period_base):
                build_baseline(rt, camera_key)
                time.sleep(0.1)
                continue

            if now - last_check < 0.05:
                time.sleep(0.02)
                continue
            last_check = now

            g = roi_gray_small(camera_cfg, fr)
            if g is None:
                time.sleep(0.02)
                continue

            ratio = motion_ratio(rt, camera_key, camera_cfg, g)
            st.last_ratio = ratio
            threshold = float(motion_cfg.get("pixel_change_pct", 2.0))
            prev_active = st.active

            if ratio >= threshold:
                st.active = True
                st.last_motion_ts = now
                if not prev_active:
                    st.trigger.set()
            else:
                if (now - st.last_motion_ts) >= cooldown:
                    st.active = False

            time.sleep(0.02)
        except Exception:
            time.sleep(0.2)


# ============================================================
# Gate serial manager
# ============================================================

class GateSerialManager:
    def __init__(self, rt: RuntimeContext):
        self.rt = rt
        self.lock = threading.Lock()
        self.ser = None
        self.device = ""
        self.baud = 115200
        self.last_ok = 0.0
        self.last_err = ""
        self.q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def pick_device(self, preferred: str = "") -> str:
        if preferred and os.path.exists(preferred):
            return preferred
        byid = sorted(glob.glob("/dev/serial/by-id/*"))
        for p in byid:
            if os.path.exists(p):
                return p
        for pat in ("/dev/ttyACM*", "/dev/ttyUSB*"):
            for p in sorted(glob.glob(pat)):
                if os.path.exists(p):
                    return p
        return ""

    def open(self, dev: str, baud: int):
        if serial is None:
            self.last_err = "pyserial no disponible"
            return False
        try:
            s = serial.Serial(dev, baudrate=baud, timeout=0.15, write_timeout=0.5)
            try:
                s.reset_input_buffer()
                s.reset_output_buffer()
            except Exception:
                pass
            with self.lock:
                if self.ser:
                    try:
                        self.ser.close()
                    except Exception:
                        pass
                self.ser = s
                self.device = dev
                self.baud = baud
                self.last_ok = time.time()
                self.last_err = ""
            return True
        except Exception as e:
            self.last_err = str(e)
            return False

    def close(self):
        with self.lock:
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None

    def status(self):
        with self.lock:
            return {
                "connected": bool(self.ser),
                "device": self.device,
                "baud": self.baud,
                "last_ok": self.last_ok,
                "last_err": self.last_err,
                "pending": self.q.qsize(),
            }

    def send_pulse(self, gate: int, ms: int, pin: int | None = None, active_low: bool | None = None):
        try:
            payload = {"cmd": "pulse", "gate": int(gate), "ms": int(ms)}
            if pin is not None:
                payload["pin"] = int(pin)
            if active_low is not None:
                payload["active_low"] = 1 if bool(active_low) else 0
            self.q.put_nowait(payload)
            return True
        except queue.Full:
            return False

    def _loop(self):
        while True:
            preferred = ""
            baud = 115200

            try:
                for _side, _lane_no, _cam_no, cam in self.rt.iter_enabled_cameras():
                    runtime = cam["runtime"]
                    dev = (runtime.get("gate_serial_device", "") or "").strip()
                    if dev:
                        preferred = dev
                        baud = int(runtime.get("gate_serial_baud", 115200))
                        break
            except Exception:
                pass

            with self.lock:
                alive = bool(self.ser)
                dev_current = self.device

            if not alive:
                dev = self.pick_device(preferred)
                if dev:
                    self.open(dev, baud)
                time.sleep(0.6)
                continue

            if preferred and preferred != dev_current and os.path.exists(preferred):
                self.open(preferred, baud)
                time.sleep(0.2)

            try:
                with self.lock:
                    s = self.ser
                if s:
                    try:
                        _ = s.read(256)
                    except Exception:
                        pass
            except Exception:
                pass

            try:
                item = self.q.get(timeout=0.25)
            except queue.Empty:
                time.sleep(0.05)
                continue

            try:
                line = (json.dumps(item, separators=(",", ":")) + "\n").encode("utf-8")
                with self.lock:
                    s = self.ser
                if not s:
                    try:
                        self.q.put_nowait(item)
                    except queue.Full:
                        pass
                    time.sleep(0.2)
                else:
                    try:
                        s.write(line)
                        self.last_ok = time.time()
                    except Exception as e:
                        self.last_err = str(e)
                        self.close()
                        try:
                            self.q.put_nowait(item)
                        except queue.Full:
                            pass
                        time.sleep(0.4)
            finally:
                try:
                    self.q.task_done()
                except Exception:
                    pass


# ============================================================
# Gate fire
# ============================================================

def camera_gate_can_fire(rt: RuntimeContext, camera_key: str, camera_cfg: dict[str, Any]) -> bool:
    state = rt.camera_states[camera_key]
    last_gate_ts = float(state.get("gate_last_ts", 0.0) or 0.0)
    antispam = max(1, int(camera_cfg["runtime"].get("gate_antispam_sec", 4)))
    return (time.time() - last_gate_ts) >= antispam


def camera_gate_mark(rt: RuntimeContext, camera_key: str):
    with rt.camera_state_locks[camera_key]:
        rt.camera_states[camera_key]["gate_last_ts"] = time.time()


def gate_fire_http(camera_cfg: dict[str, Any]) -> tuple[bool, str]:
    rt_cfg = camera_cfg["runtime"]
    base_url = rt_cfg.get("gate_url", "")
    token = (rt_cfg.get("gate_token") or "").strip()
    if not base_url or not token:
        return False, "Config incompleta gate HTTP"

    pulse_url = base_url if base_url.lower().endswith("/pulse") else (base_url + "/pulse")

    try:
        host = re.sub(r"^https?://", "", pulse_url).split("/")[0].split(":")[0]
        if host:
            socket.gethostbyname(host)
    except Exception as e:
        return False, f"No resuelve hostname (DNS/mDNS): {e}"

    params = {
        "token": token,
        "pin": int(rt_cfg.get("gate_pin", 5)),
        "active_low": 1 if rt_cfg.get("gate_active_low", False) else 0,
        "ms": int(rt_cfg.get("gate_pulse_ms", 500)),
    }

    last_err = ""
    for _ in range(2):
        try:
            r = requests.post(pulse_url, data=params, timeout=4)
            if r.status_code == 200:
                return True, "OK"
            r2 = requests.get(pulse_url, params=params, timeout=4)
            if r2.status_code == 200:
                return True, "OK"
            last_err = f"ESP32 HTTP {r.status_code}/{r2.status_code}"
        except Exception as e:
            last_err = f"ESP32 HTTP error: {e}"
        time.sleep(0.2)

    return False, last_err or "ESP32 HTTP fail"


def gate_fire(rt: RuntimeContext, camera_key: str) -> tuple[bool, str]:
    side, lane_no, cam_no = rt.parse_camera_key(camera_key)
    camera_cfg = rt.get_camera_cfg(side, lane_no, cam_no)
    rt_cfg = camera_cfg["runtime"]

    if not rt_cfg.get("gate_enabled", False):
        return False, "Gate deshabilitado"
    if not camera_gate_can_fire(rt, camera_key, camera_cfg):
        return False, f"Anti-spam {rt_cfg.get('gate_antispam_sec', 4)}s"

    mode = (rt_cfg.get("gate_mode", "serial") or "serial").lower()
    if mode == "http":
        ok, msg = gate_fire_http(camera_cfg)
        if ok:
            camera_gate_mark(rt, camera_key)
        return ok, msg

    if serial is None:
        return False, "pyserial no disponible"

    gate_num = int(rt_cfg.get("gate_serial_gate", cam_no))
    ms = int(rt_cfg.get("gate_pulse_ms", 500))
    pin = int(rt_cfg.get("gate_pin", 5))
    active_low = bool(rt_cfg.get("gate_active_low", False))
    ok = rt.gate_serial.send_pulse(gate_num, ms, pin=pin, active_low=active_low)
    if not ok:
        return False, "Cola serial llena (drop)"

    st = rt.gate_serial.status()
    if not st["connected"]:
        return False, "Serial no conectado (reintentando): " + (st.get("last_err", "") or "")

    camera_gate_mark(rt, camera_key)
    return True, "OK"


# ============================================================
# Send manager
# ============================================================

def jpeg_bytes(frame, q: int = 75):
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, q])
    if not ok:
        return None
    return bytes(buf.tobytes())


class SendManager:
    def __init__(self, rt: RuntimeContext, camera_key: str, max_q: int = 80):
        self.rt = rt
        self.camera_key = camera_key
        self.q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_q)
        self.dropped = 0
        self.sent = 0
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def put(self, item: dict[str, Any]):
        try:
            self.q.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def _send_to_endpoint(self, sess: requests.Session, url: str, payload: dict[str, Any], snap_bytes, mode: str):
        url = (url or "").strip()
        if not url:
            return False, "no-url"
        try:
            if snap_bytes is not None:
                if (mode or "multipart").lower() == "json":
                    js = dict(payload)
                    js["snapshot_b64"] = base64.b64encode(snap_bytes).decode("ascii")
                    r = sess.post(url, json=js, timeout=8)
                else:
                    files = {"snapshot": ("snapshot.jpg", snap_bytes, "image/jpeg")}
                    r = sess.post(url, data=payload, files=files, timeout=8)
            else:
                r = sess.post(url, json=payload, timeout=8)
            return (r.status_code == 200), f"HTTP {r.status_code}"
        except Exception as e:
            return False, str(e)

    def _loop(self):
        sess = requests.Session()
        while True:
            item = self.q.get()
            try:
                endpoints = item["endpoints"]
                payload = item["payload"]

                need_snap = any(bool(es) for (_, es, _) in endpoints)
                snap_jpeg = None
                if need_snap:
                    fr = self.rt.video_sources[self.camera_key].get()
                    if fr is not None:
                        snap_jpeg = jpeg_bytes(fr, 75)

                any_ok = False
                for url, send_snap, mode in endpoints:
                    if not (url or "").strip():
                        continue
                    snap = snap_jpeg if (send_snap and snap_jpeg is not None) else None
                    ok, _msg = self._send_to_endpoint(sess, url, payload, snap, mode)
                    any_ok = any_ok or ok
                if any_ok:
                    self.sent += 1
            finally:
                self.q.task_done()


def endpoints_pair(pair: dict[str, Any]):
    return [
        (pair.get("url1", ""), bool(pair.get("send_snapshot1", False)), (pair.get("snapshot_mode1", "multipart") or "multipart")),
        (pair.get("url2", ""), bool(pair.get("send_snapshot2", False)), (pair.get("snapshot_mode2", "multipart") or "multipart")),
    ]


def should_send(rt: RuntimeContext, camera_key: str, camera_cfg: dict[str, Any], cat: str, value: str) -> bool:
    key = canon_plate(value)
    if not key:
        return False

    now = time.time()
    last_key = canon_plate(rt.last_sent_value[camera_key].get(cat, ""))
    last_t = float(rt.last_sent_ts[camera_key].get(cat, 0.0))
    gap = max(0, int(camera_cfg["runtime"].get("wh_min_gap_sec", 0)))
    allow_rep = bool(camera_cfg["runtime"].get("wh_repeat_same_plate", False))

    if not allow_rep:
        return key != last_key
    if key != last_key:
        return True
    return (gap <= 0) or ((now - last_t) >= gap)


def mark_sent(rt: RuntimeContext, camera_key: str, cat: str, value: str):
    rt.last_sent_value[camera_key][cat] = canon_plate(value)
    rt.last_sent_ts[camera_key][cat] = time.time()


def base_payload(side: str, lane_no: int, cam_no: int, usuario: str, dispositivo: str, valor: str, disp_vals: list[str], titles: list[str]):
    payload = OrderedDict()
    payload["side"] = side
    payload["side_label"] = side_label(side)
    payload["lane"] = lane_no
    payload["camera"] = cam_no
    payload["usuario"] = usuario
    payload["dispositivo"] = dispositivo
    payload["valor"] = canon_plate(valor)
    d1, d2, d3 = (disp_vals + ["", "", ""])[:3]
    payload["disp_col_1"] = d1
    payload["disp_col_2"] = d2
    payload["disp_col_3"] = d3
    payload.update(payload_kv_from_titles(titles, disp_vals))
    return payload


def enqueue_webhooks(
    rt: RuntimeContext,
    camera_key: str,
    side: str,
    lane_no: int,
    cam_no: int,
    camera_cfg: dict[str, Any],
    cat: str,
    pair: dict[str, Any],
    usuario: str,
    dispositivo: str,
    valor: str,
    disp_vals: list[str],
    titles: list[str],
):
    endpoints = endpoints_pair(pair or {})
    if not any((u or "").strip() for (u, _, _) in endpoints):
        return False, "Sin webhooks"

    with rt.send_lock[camera_key]:
        if not should_send(rt, camera_key, camera_cfg, cat, valor):
            return False, "Dedup/gap"
        mark_sent(rt, camera_key, cat, valor)

    payload = base_payload(side, lane_no, cam_no, usuario, dispositivo, valor, disp_vals, titles)
    rt.send_managers[camera_key].put({"payload": dict(payload), "endpoints": endpoints})
    return True, "Encolado"


# ============================================================
# Side indexes / bases
# ============================================================

def build_side_index_from_rows(side_bases: dict[str, Any], rows: list[list[str]], kind: str) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    sec = side_bases[kind]
    sec = normalize_wl_section(sec)
    s = int(sec.get("search_start_col", 14)) - 1
    e = int(sec.get("search_end_col", 18)) - 1
    if e < s:
        e = s
    start = 1 if guess_has_header(rows) else 0

    for row in rows[start:]:
        if not row:
            continue
        for j in range(s, e + 1):
            if j < len(row):
                key = canon_plate(row[j] or "")
                if key:
                    idx[key] = row
    return idx


def build_tag_index_from_rows(side_bases: dict[str, Any], rows: list[list[str]]) -> dict[str, list[str]]:
    idx: dict[str, list[str]] = {}
    sec = normalize_wl_section(side_bases["tags"]["owners"])
    s = int(sec.get("search_start_col", 14)) - 1
    e = int(sec.get("search_end_col", 18)) - 1
    if e < s:
        e = s
    start = 1 if guess_has_header(rows) else 0

    for row in rows[start:]:
        if not row:
            continue
        for j in range(s, e + 1):
            if j < len(row):
                key = canon_plate(row[j] or "")
                if key:
                    idx[key] = row
    return idx


def download_side_wl(rt: RuntimeContext, side: str, kind: str) -> str:
    side_bases = rt.cfg[side]["bases"]
    sec = normalize_wl_section(side_bases[kind])
    url = gs_url(sec.get("sheets_input", ""))
    if not url:
        return f"❌ Configura '{side}.{kind}.sheets_input'"
    try:
        r = requests.get(url, timeout=25)
        if r.status_code != 200:
            return f"❌ HTTP {r.status_code} descargando CSV"
        rows = parse_csv_text(r.text)
    except Exception as e:
        return f"❌ Error WL: {e}"

    idx = build_side_index_from_rows(side_bases, rows, kind)
    with rt.lock:
        rt.side_indexes[side][kind] = idx
        rt.last_wl_refresh[side][kind] = time.time()
    return f"{side}.{kind}: {len(idx)} placas indexadas"


def download_side_tag_wl(rt: RuntimeContext, side: str) -> str:
    side_bases = rt.cfg[side]["bases"]
    sec = normalize_wl_section(side_bases["tags"]["owners"])
    url = gs_url(sec.get("sheets_input", ""))
    if not url:
        return f"❌ Configura '{side}.tags.owners.sheets_input'"
    try:
        r = requests.get(url, timeout=25)
        if r.status_code != 200:
            return f"❌ HTTP {r.status_code} descargando CSV"
        rows = parse_csv_text(r.text)
    except Exception as e:
        return f"❌ Error TAG WL: {e}"

    idx = build_tag_index_from_rows(side_bases, rows)
    with rt.lock:
        rt.side_indexes[side]["tags_owners"] = idx
        rt.last_wl_refresh[side]["tags"] = time.time()
    return f"{side}.tags: {len(idx)} tags indexados"


def lookup_row(rt: RuntimeContext, side: str, plate: str):
    p = canon_plate(plate)
    ro = rt.side_indexes[side]["owners"].get(p)
    if ro is not None:
        return "PROPIETARIO", ro
    rv = rt.side_indexes[side]["visitors"].get(p)
    if rv is not None:
        return "VISITA", rv
    return "NONE", None


def lookup_tag_row(rt: RuntimeContext, side: str, tag_key: str):
    p = canon_plate(tag_key)
    ro = rt.side_indexes[side]["tags_owners"].get(p)
    if ro is not None:
        return "PROPIETARIO", ro
    return "NONE", None


# ============================================================
# State helpers
# ============================================================

def update_camera_plate_state(
    rt: RuntimeContext,
    camera_key: str,
    plate: str,
    conf: float,
    cat: str,
    auth: bool,
    user_type: str,
    display: list[str],
    titles: list[str],
):
    with rt.camera_state_locks[camera_key]:
        st = rt.camera_states[camera_key]
        st["plate"] = plate
        st["conf"] = float(conf)
        st["ts"] = time.time()
        st["auth"] = bool(auth)
        st["cat"] = cat
        st["display"] = list(display)
        st["titles"] = list(titles)
        st["user_type"] = user_type


def update_camera_raw_candidates(
    rt: RuntimeContext,
    camera_key: str,
    raw_candidates: list[dict],
):
    with rt.camera_state_locks[camera_key]:
        st = rt.camera_states[camera_key]
        st["raw_candidates"] = list(raw_candidates or [])
        st["raw_ts"] = time.time()


def update_camera_tag_state(
    rt: RuntimeContext,
    camera_key: str,
    tag_val: str,
    cat: str,
    auth: bool,
    user_type: str,
    fields: list[str],
):
    with rt.camera_state_locks[camera_key]:
        st = rt.camera_states[camera_key]
        st["tag"] = tag_val
        st["tag_ts"] = time.time()
        st["tag_cat"] = cat
        st["tag_fields"] = list(fields)
        st["tag_auth"] = bool(auth)
        st["tag_user_type"] = user_type


# ============================================================
# Native ALPR loop
# ============================================================

def alpr_loop(rt: RuntimeContext, camera_key: str):
    k = 0
    while True:
        try:
            side, lane_no, cam_no = rt.parse_camera_key(camera_key)
            camera_cfg = rt.get_camera_cfg(side, lane_no, cam_no)
            runtime = camera_cfg["runtime"]

            if not camera_cfg.get("enabled", False):
                time.sleep(0.3)
                continue

            fr = rt.video_sources[camera_key].get()
            if fr is None:
                time.sleep(0.02)
                continue

            mot = rt.motion_states[camera_key]
            if runtime["motion"].get("enabled", True) and not mot.active:
                time.sleep(0.20)
                if mot.trigger.is_set():
                    mot.trigger.clear()
                else:
                    continue

            if mot.trigger.is_set():
                mot.trigger.clear()
                k = 0

            k = (k + 1) % runtime["process_every_n"]
            if k != 0:
                time.sleep(0.01)
                continue

            fr_roi = apply_roi(camera_cfg, fr)
            fr_alpr = preprocess_for_alpr(camera_cfg, fr_roi)

            results = run_alpr(rt, fr_alpr, runtime["resize_max_w"], topk=runtime["alpr_topk"])

            raw_candidates = []
            for item in results[:5]:
                try:
                    t, c, d = item
                    raw_candidates.append({
                        "text": str(t),
                        "ocr_conf": float(c),
                        "det_conf": float(d),
                    })
                except Exception:
                    pass
            update_camera_raw_candidates(rt, camera_key, raw_candidates)

            if not results:
                rt.stable_state[camera_key]["last"] = ""
                rt.stable_state[camera_key]["hits"] = 0
                time.sleep(0.01)
                continue

            text, conf, det_conf = results[0]

            if det_conf < float(runtime.get("det_min_confidence", 0.80)):
                rt.stable_state[camera_key]["last"] = ""
                rt.stable_state[camera_key]["hits"] = 0
                time.sleep(0.01)
                continue

            if conf < float(runtime.get("min_confidence", 0.90)):
                rt.stable_state[camera_key]["last"] = ""
                rt.stable_state[camera_key]["hits"] = 0
                time.sleep(0.01)
                continue

            key = canon_plate(text)
            if key == rt.stable_state[camera_key]["last"]:
                rt.stable_state[camera_key]["hits"] += 1
            else:
                rt.stable_state[camera_key]["last"] = key
                rt.stable_state[camera_key]["hits"] = 1

            needed = int(runtime.get("stable_hits_required", 2))
            user_type, row = lookup_row(rt, side, text)
            if user_type == "NONE":
                needed = int(runtime.get("notfound_stable_hits_required", 4))

            if rt.stable_state[camera_key]["hits"] < needed:
                time.sleep(0.01)
                continue

            if user_type == "NONE":
                sup = int(runtime.get("suppress_notfound_after_auth_sec", 8))
                if sup > 0 and (time.time() - rt.last_auth_ts[camera_key]) < sup:
                    time.sleep(0.01)
                    continue

            side_bases = rt.cfg[side]["bases"]
            disp_vals = ["", "", ""]
            titles = ["Folio", "Nombre", "Telefono"]
            auth = False

            if user_type == "PROPIETARIO":
                rt.last_auth_ts[camera_key] = time.time()
                sec = normalize_wl_section(side_bases["owners"])
                auth = is_active_from_row(sec, row)
                disp_vals = extract_fields(row, sec.get("disp_cols"))
                titles = sec.get("disp_titles", titles)
                pair = sec["wh_active"] if auth else sec["wh_inactive"]
                cat = "ACTIVE" if auth else "INACTIVE"
            elif user_type == "VISITA":
                rt.last_auth_ts[camera_key] = time.time()
                sec = normalize_wl_section(side_bases["visitors"])
                auth = is_active_from_row(sec, row)
                disp_vals = extract_fields(row, sec.get("disp_cols"))
                titles = sec.get("disp_titles", titles)
                pair = sec["wh_active"] if auth else sec["wh_inactive"]
                cat = "ACTIVE" if auth else "INACTIVE"
            else:
                pair = normalize_wh_pair(side_bases["wh_notfound"])
                cat = "NOTFOUND"

            if auth and runtime.get("gate_enabled", False) and runtime.get("gate_auto_on_auth", False):
                gate_fire(rt, camera_key)

            if user_type != "NONE":
                enqueue_webhooks(rt, camera_key, side, lane_no, cam_no, camera_cfg, cat, pair, user_type, "Placa", text, disp_vals, titles)
            else:
                enqueue_webhooks(rt, camera_key, side, lane_no, cam_no, camera_cfg, "NOTFOUND", pair, "NoFound", "Placa", text, ["", "", ""], ["Folio", "Nombre", "Telefono"])

            update_camera_plate_state(rt, camera_key, text, float(conf), cat, auth, user_type, disp_vals, titles)
            time.sleep(0.005)

        except Exception:
            time.sleep(0.2)


# ============================================================
# Tag event (native)
# ============================================================

def process_tag_event(rt: RuntimeContext, side: str, lane_no: int, cam_no: int, body: dict[str, Any]) -> dict[str, Any]:
    camera_key = rt.camera_key(side, lane_no, cam_no)
    camera_cfg = rt.get_camera_cfg(side, lane_no, cam_no)
    runtime = camera_cfg["runtime"]

    side_bases = rt.cfg[side]["bases"]
    tags_cfg = normalize_tag_section(side_bases["tags"])
    fmt = (tags_cfg.get("lookup_format", "physical") or "physical").lower()

    physical = (body.get("tag_physical") or "").strip().upper()
    internal = (body.get("tag_internal_hex") or "").strip().upper()
    key = internal if fmt == "internal_hex" else physical
    pkey = canon_plate(key)

    user_type = "NONE"
    row = None
    auth = False
    disp_vals = ["", "", ""]
    titles = ["Folio", "Nombre", "Telefono"]
    cat = "NOTFOUND"

    if pkey:
        user_type, row = lookup_tag_row(rt, side, pkey)

    if user_type == "PROPIETARIO":
        sec = normalize_wl_section(tags_cfg["owners"])
        auth = is_active_from_row(sec, row)
        disp_vals = extract_fields(row, sec.get("disp_cols"))
        titles = sec.get("disp_titles", titles)
        pair = sec["wh_active"] if auth else sec["wh_inactive"]
        cat = "ACTIVE" if auth else "INACTIVE"
    else:
        pair = normalize_wh_pair(tags_cfg["wh_notfound"])
        cat = "NOTFOUND"

    if auth and runtime.get("gate_enabled", False) and runtime.get("gate_auto_on_auth", False):
        gate_fire(rt, camera_key)

    tag_display = physical or internal or ""

    update_camera_tag_state(rt, camera_key, tag_display, cat, auth, user_type, disp_vals)

    if user_type == "PROPIETARIO":
        enqueue_webhooks(rt, camera_key, side, lane_no, cam_no, camera_cfg, cat, pair, user_type, "Tag", pkey, disp_vals, titles)
    else:
        enqueue_webhooks(rt, camera_key, side, lane_no, cam_no, camera_cfg, "NOTFOUND", pair, "NoFound", "Tag", pkey, ["", "", ""], ["Folio", "Nombre", "Telefono"])

    return {
        "ok": True,
        "active": bool(auth),
        "category": cat,
        "user_type": user_type,
    }


# ============================================================
# Background loops
# ============================================================

def read_cpu_times():
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            ln = f.readline()
        parts = ln.split()
        if parts[0] != "cpu":
            return None
        vals = list(map(int, parts[1:8]))
        idle = vals[3] + vals[4]
        total = sum(vals)
        return idle, total
    except Exception:
        return None


def read_temp_c():
    base_path = "/sys/class/thermal"
    try:
        zones = [os.path.join(base_path, x, "temp") for x in os.listdir(base_path) if x.startswith("thermal_zone")]
        for p in zones:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                if not raw:
                    continue
                v = float(raw)
                if v > 200:
                    v /= 1000.0
                if v < 0:
                    continue
                return round(v, 1)
            except Exception:
                continue
    except Exception:
        pass
    try:
        out = subprocess.check_output(["vcgencmd", "measure_temp"], text=True).strip()
        m = re.search(r"temp=([0-9.]+)'C", out)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return None


def sysmon_loop(rt: RuntimeContext):
    t = read_cpu_times()
    if t:
        rt.cpu_prev = t
    while True:
        t2 = read_cpu_times()
        cpu_pct = 0.0
        if t2 and rt.cpu_prev:
            idle0, tot0 = rt.cpu_prev
            idle1, tot1 = t2
            di = idle1 - idle0
            dt = tot1 - tot0
            if dt > 0:
                cpu_pct = max(0.0, min(100.0, (1.0 - (di / float(dt))) * 100.0))
            rt.cpu_prev = t2

        rt.sys_status["cpu_pct"] = round(cpu_pct, 1)
        rt.sys_status["temp_c"] = read_temp_c()
        time.sleep(1.0)


def auto_refresh_loop(rt: RuntimeContext):
    for side in ("entry", "exit"):
        download_side_wl(rt, side, "owners")
        download_side_wl(rt, side, "visitors")
        download_side_tag_wl(rt, side)

    while True:
        try:
            now = time.time()
            for side in ("entry", "exit"):
                bases = rt.cfg[side]["bases"]

                for kind in ("owners", "visitors"):
                    mins = int(bases[kind].get("auto_refresh_min", 0))
                    if mins > 0 and (now - rt.last_wl_refresh[side][kind]) >= (mins * 60):
                        download_side_wl(rt, side, kind)

                tmins = int(bases["tags"]["owners"].get("auto_refresh_min", 0))
                if tmins > 0 and (now - rt.last_wl_refresh[side]["tags"]) >= (tmins * 60):
                    download_side_tag_wl(rt, side)
        except Exception:
            pass
        time.sleep(3)


def start_runtime(rt: RuntimeContext):
    rt.init_alpr()
    rt.refresh_runtime_registry()
    threading.Thread(target=sysmon_loop, args=(rt,), daemon=True).start()
    threading.Thread(target=auto_refresh_loop, args=(rt,), daemon=True).start()
