from __future__ import annotations

import datetime
import os
import queue
import socket
import threading
import time
from collections import OrderedDict

import requests

from app.portal_v8_models import DEFAULT_CFG_FILE, LEGACY_CFG_FILE, load_cfg, save_cfg
from app.portal_v8_runtime import RuntimeContext, start_runtime, side_label
from app.portal_v8_web import create_app
from app.portal_v8_ui import register_ui_routes

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/Mexico_City")
except Exception:
    TZ = None


APP_TITLE = "Comunito Portal v8.0"


def save_runtime_cfg(cfg: dict) -> None:
    save_cfg(cfg, DEFAULT_CFG_FILE)


def iso_now() -> str:
    try:
        if TZ is not None:
            return datetime.datetime.now(tz=TZ).isoformat()
        return datetime.datetime.now().isoformat()
    except Exception:
        return datetime.datetime.now().isoformat()


def safe_hostname() -> str:
    try:
        return os.uname().nodename
    except Exception:
        try:
            return socket.gethostname()
        except Exception:
            return ""


def shell_out(cmd: str) -> str:
    try:
        import subprocess
        out = subprocess.check_output(cmd, shell=True, stderr=subprocess.DEVNULL, text=True)
        return out.strip()
    except Exception:
        return ""


def camera_heartbeat_payload(rt: RuntimeContext, side: str, lane_no: int, cam_no: int):
    camera_key = rt.camera_key(side, lane_no, cam_no)
    rt.ensure_camera_runtime_objects(camera_key)

    cam_cfg = rt.get_camera_cfg(side, lane_no, cam_no)
    runtime = cam_cfg["runtime"]

    try:
        with rt.camera_state_locks[camera_key]:
            st = dict(rt.camera_states[camera_key])
    except Exception:
        st = {}

    try:
        mot = rt.motion_states[camera_key]
        motion_active = bool(mot.active)
        motion_ratio = float(mot.last_ratio)
    except Exception:
        motion_active = False
        motion_ratio = 0.0

    try:
        send_mgr = rt.send_managers[camera_key]
        queue_pending = int(send_mgr.q.qsize())
        queue_dropped = int(send_mgr.dropped)
        queue_sent = int(send_mgr.sent)
    except Exception:
        queue_pending = 0
        queue_dropped = 0
        queue_sent = 0

    from app.portal_v8_runtime import materialize_camera_url, ping_ip, resolve_ip_by_mac

    try:
        url, ip, url_mode = materialize_camera_url(rt, cam_cfg)
    except Exception:
        url, ip, url_mode = "", None, ""

    lan_ok = False
    if ip:
        try:
            lan_ok = bool(ping_ip(ip, 1))
        except Exception:
            lan_ok = False
    elif runtime.get("camera_mode", "mac") == "mac" and runtime.get("camera_mac"):
        try:
            ip2 = resolve_ip_by_mac(rt, runtime.get("camera_mac", ""))
            if ip2:
                ip = ip2
                lan_ok = bool(ping_ip(ip2, 1))
        except Exception:
            pass

    return OrderedDict({
        "side": side,
        "side_label": side_label(side),
        "lane": lane_no,
        "camera": cam_no,
        "camera_name": cam_cfg.get("name", f"Cámara {cam_no}"),
        "role_label": cam_cfg.get("role_label", ""),
        "enabled": bool(cam_cfg.get("enabled", False)),
        "camera_mode": runtime.get("camera_mode", "mac"),
        "camera_mac": runtime.get("camera_mac", ""),
        "camera_url_materialized": url or "",
        "lan_ip": ip or "",
        "lan_ok": bool(lan_ok),
        "url_mode": url_mode,
        "motion_active": motion_active,
        "motion_ratio": motion_ratio,
        "queue_pending": queue_pending,
        "queue_dropped": queue_dropped,
        "queue_sent": queue_sent,
        "plate": st.get("plate", ""),
        "plate_cat": st.get("cat", ""),
        "plate_user_type": st.get("user_type", ""),
        "plate_ts": st.get("ts", 0.0),
        "tag": st.get("tag", ""),
        "tag_cat": st.get("tag_cat", ""),
        "tag_ts": st.get("tag_ts", 0.0),
        "gate_enabled": bool(runtime.get("gate_enabled", False)),
        "gate_mode": runtime.get("gate_mode", "serial"),
    })


def heartbeat_payload(rt: RuntimeContext):
    payload = OrderedDict()
    payload["ts"] = iso_now()
    payload["app"] = APP_TITLE
    payload["host"] = safe_hostname()
    payload["site_name"] = rt.cfg.get("site_name", "Acceso Principal")
    payload["ip"] = shell_out("hostname -I | awk '{print $1}' || true")
    payload["temp_c"] = rt.sys_status.get("temp_c")
    payload["cpu_pct"] = rt.sys_status.get("cpu_pct")

    try:
        payload["gate_serial"] = rt.gate_serial.status()
    except Exception:
        payload["gate_serial"] = {}

    sides = []
    for side in ("entry", "exit"):
        side_cfg = rt.cfg.get(side, {})
        side_out = OrderedDict()
        side_out["side"] = side
        side_out["side_label"] = side_label(side)
        side_out["name"] = side_cfg.get("name", side_label(side))
        side_out["enabled"] = bool(side_cfg.get("enabled", True))
        side_out["cameras"] = []

        if side_out["enabled"]:
            for lane_no, lane in enumerate(side_cfg.get("lanes", []), start=1):
                if not lane.get("enabled", False):
                    continue
                for cam_no, cam in enumerate(lane.get("cameras", []), start=1):
                    if not cam.get("enabled", False):
                        continue
                    side_out["cameras"].append(camera_heartbeat_payload(rt, side, lane_no, cam_no))

        sides.append(side_out)

    payload["sides"] = sides
    return payload


class HeartbeatManager:
    def __init__(self, rt: RuntimeContext, max_q: int = 20):
        self.rt = rt
        self.q = queue.Queue(maxsize=max_q)
        self.t = threading.Thread(target=self._loop, daemon=True)
        self.t.start()

    def enqueue(self, reason: str = "periodic"):
        try:
            self.q.put_nowait({"reason": reason, "ts": time.time()})
        except queue.Full:
            self.rt.hb_status["dropped"] = int(self.rt.hb_status.get("dropped", 0)) + 1

    def _post_with_retries(self, sess: requests.Session, url: str, js: dict):
        last_err = ""
        last_code = None
        for slp in (0.0, 0.5, 1.0, 2.0):
            if slp > 0:
                time.sleep(slp)
            try:
                r = sess.post(url, json=js, timeout=8)
                last_code = r.status_code
                if r.status_code == 200:
                    return True, r.status_code, ""
                last_err = f"HTTP {r.status_code}"
            except Exception as e:
                last_err = str(e)
        return False, last_code, last_err

    def _loop(self):
        sess = requests.Session()
        while True:
            try:
                item = self.q.get()
                try:
                    self.rt.hb_status["pending"] = int(self.q.qsize())

                    hb_cfg = self.rt.cfg.get("heartbeat", {})
                    enabled = bool(hb_cfg.get("enabled", False))
                    url = (hb_cfg.get("url", "") or "").strip()
                    period = int(hb_cfg.get("period_min", 0) or 0)
                    reason = (item.get("reason") or "periodic")

                    if (not enabled) or (not url):
                        self.rt.hb_status["last_err"] = "Heartbeat deshabilitado o sin URL"
                        continue

                    if reason == "periodic" and period <= 0:
                        self.rt.hb_status["last_err"] = "Periodo=0 (off)"
                        continue

                    self.rt.hb_status["last_try_ts"] = time.time()
                    payload = heartbeat_payload(self.rt)
                    payload["reason"] = reason

                    ok, code, err = self._post_with_retries(sess, url, payload)
                    self.rt.hb_status["last_code"] = code

                    if ok:
                        self.rt.hb_status["last_ok_ts"] = time.time()
                        self.rt.hb_status["last_err"] = ""
                        self.rt.hb_status["sent"] = int(self.rt.hb_status.get("sent", 0)) + 1
                    else:
                        self.rt.hb_status["last_err"] = err or "fail"
                        self.rt.hb_status["fail"] = int(self.rt.hb_status.get("fail", 0)) + 1
                finally:
                    try:
                        self.q.task_done()
                    except Exception:
                        pass
            except Exception:
                time.sleep(0.2)


def heartbeat_scheduler_loop(rt: RuntimeContext):
    last_sent = 0.0
    while True:
        try:
            hb_cfg = rt.cfg.get("heartbeat", {})
            enabled = bool(hb_cfg.get("enabled", False))
            url = (hb_cfg.get("url", "") or "").strip()
            period = int(hb_cfg.get("period_min", 0) or 0)

            if enabled and url and period > 0:
                now = time.time()
                if (now - last_sent) >= (period * 60.0):
                    if hasattr(rt, "heartbeat_mgr") and rt.heartbeat_mgr:
                        rt.heartbeat_mgr.enqueue("periodic")
                    last_sent = now
        except Exception:
            pass
        time.sleep(1.0)


def build_app():
    cfg = load_cfg(DEFAULT_CFG_FILE, LEGACY_CFG_FILE)
    rt = RuntimeContext(cfg)
    rt.heartbeat_mgr = HeartbeatManager(rt)

    start_runtime(rt)

    app = create_app(rt)
    register_ui_routes(app, rt, save_runtime_cfg)

    threading.Thread(target=heartbeat_scheduler_loop, args=(rt,), daemon=True).start()

    app.config["RUNTIME"] = rt
    return app, rt


app, runtime = build_app()


if __name__ == "__main__":
    os.environ["TZ"] = "America/Mexico_City"
    os.environ["OMP_NUM_THREADS"] = "2"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"

    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=5000, threads=12)
    except Exception:
        app.run(host="0.0.0.0", port=5000, threaded=True)
