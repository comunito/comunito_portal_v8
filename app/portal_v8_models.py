from __future__ import annotations

import copy
import json
import os
import re
from typing import Any

APP_VERSION = "8.0.0"
DEFAULT_CFG_FILE = "config_v8.json"
LEGACY_CFG_FILE = "config_full.json"


# ============================================================
# Helpers
# ============================================================

def clampi(v: Any, lo: int, hi: int, fb: int) -> int:
    try:
        v = int(float(v))
    except Exception:
        return fb
    return max(lo, min(hi, v))


def clampf(v: Any, lo: float, hi: float, fb: float) -> float:
    try:
        v = float(v)
    except Exception:
        return fb
    return max(lo, min(hi, v))


def parse_bool(v: Any, fb: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return fb
    s = str(v).strip().lower()
    if s in ("1", "true", "t", "yes", "y", "on", "si", "sí", "checked"):
        return True
    if s in ("0", "false", "f", "no", "n", "off"):
        return False
    return fb


def canon_plate(s: str) -> str:
    return "".join([c for c in str(s or "").upper() if c.isalnum()])


def col_to_idx(x: Any, fb: int | None = None) -> int | None:
    if x is None:
        return fb
    s = str(x).strip()
    if s == "":
        return fb
    if re.fullmatch(r"\d+", s):
        return int(s)
    if re.fullmatch(r"[A-Za-z]+", s):
        s = s.upper()
        n = 0
        for ch in s:
            n = n * 26 + (ord(ch) - 64)
        return n
    return fb


def norm_cols_any(v: Any, n: int = 3) -> list[int | None]:
    out: list[int | None] = []
    try:
        for x in list(v)[:n]:
            if x is None or str(x).strip() == "":
                out.append(None)
            else:
                out.append(col_to_idx(x, None))
    except Exception:
        base = [2, 3, 4]
        return base[:n]
    while len(out) < n:
        out.append(None)
    return out


def norm_url_base(u: str) -> str:
    """
    Normaliza URL base para gate HTTP.
    """
    u = (u or "").strip()
    if not u:
        return ""
    if not (u.startswith("http://") or u.startswith("https://")):
        u = "http://" + u

    while u.endswith("/") and len(u) > len("http://x"):
        u = u[:-1]

    if u.lower().endswith("/pulse"):
        u = u[:-len("/pulse")]
        while u.endswith("/") and len(u) > len("http://x"):
            u = u[:-1]
    return u


# ============================================================
# Defaults v8
# ============================================================

WH_PAIR_DEF = {
    "url1": "",
    "send_snapshot1": False,
    "snapshot_mode1": "multipart",
    "url2": "",
    "send_snapshot2": False,
    "snapshot_mode2": "multipart",
}

WL_DEF = {
    "sheets_input": "",
    "search_start_col": 14,
    "search_end_col": 18,
    "status_col": 3,
    "disp_cols": [2, 3, 4],
    "disp_titles": ["Folio", "Nombre", "Telefono"],
    "auto_refresh_min": 0,
    "wh_active": copy.deepcopy(WH_PAIR_DEF),
    "wh_inactive": copy.deepcopy(WH_PAIR_DEF),
}

TAG_DEF = {
    "lookup_format": "physical",
    "owners": copy.deepcopy(WL_DEF),
    "wh_notfound": copy.deepcopy(WH_PAIR_DEF),
}

SIDE_BASES_DEF = {
    "owners": copy.deepcopy(WL_DEF),
    "visitors": copy.deepcopy(WL_DEF),
    "tags": copy.deepcopy(TAG_DEF),
    "wh_notfound": copy.deepcopy(WH_PAIR_DEF),
}

ROI_DEF = {
    "enabled": False,
    "x": 0.0,
    "y": 0.0,
    "w": 1.0,
    "h": 1.0,
}

MOTION_DEF = {
    "enabled": True,
    "pixel_change_pct": 2.0,
    "intensity_delta": 25,
    "autobase_every_min": 10,
    "autobase_samples": 3,
    "autobase_interval_s": 1.0,
    "cooldown_s": 2.0,
}

CAMERA_RUNTIME_DEF = {
    "camera_mode": "mac",
    "camera_mac": "",
    "camera_url": "rtsp://usuario:pass@{CAM_IP}:554/Streaming/Channels/102",
    "process_every_n": 2,
    "resize_max_w": 1280,
    "alpr_topk": 3,
    "min_confidence": 0.90,
    "idle_clear_sec": 1.5,
    "det_min_confidence": 0.80,
    "stable_hits_required": 2,
    "notfound_stable_hits_required": 4,
    "suppress_notfound_after_auth_sec": 8,
    "latch_hold_sec": 30.0,
    "pp_enabled": False,
    "pp_profile": "none",
    "pp_clahe_clip": 2.0,
    "pp_sharp_strength": 0.55,
    "roi": copy.deepcopy(ROI_DEF),
    "motion": copy.deepcopy(MOTION_DEF),
    "gate_enabled": False,
    "gate_auto_on_auth": False,
    "gate_antispam_sec": 4,
    "gate_pulse_ms": 500,
    "gate_mode": "serial",
    "gate_url": "",
    "gate_token": "12345",
    "gate_pin": 5,
    "gate_active_low": False,
    "gate_serial_device": "",
    "gate_serial_baud": 115200,
    "gate_serial_gate": 1,
    "wh_repeat_same_plate": False,
    "wh_min_gap_sec": 0,
}

CAMERA_DEF = {
    "enabled": False,
    "name": "Cámara",
    "role_label": "",
    "runtime": copy.deepcopy(CAMERA_RUNTIME_DEF),
}

LANE_DEF = {
    "enabled": False,
    "name": "Carril",
    "cameras": [copy.deepcopy(CAMERA_DEF), copy.deepcopy(CAMERA_DEF)],
}

SIDE_DEF = {
    "enabled": True,
    "name": "Entrada",
    "bases": copy.deepcopy(SIDE_BASES_DEF),
    "lanes": [copy.deepcopy(LANE_DEF), copy.deepcopy(LANE_DEF), copy.deepcopy(LANE_DEF)],
}

SITE_DEF = {
    "version": APP_VERSION,
    "site_name": "Acceso Principal",
    "heartbeat": {
        "enabled": False,
        "url": "",
        "period_min": 0,
    },
    "entry": copy.deepcopy(SIDE_DEF),
    "exit": copy.deepcopy(SIDE_DEF),
}

SITE_DEF["entry"]["name"] = "Entrada"
SITE_DEF["exit"]["name"] = "Salida"


def default_camera(cam_no: int = 1) -> dict[str, Any]:
    d = copy.deepcopy(CAMERA_DEF)
    d["enabled"] = (cam_no == 1)
    d["name"] = f"Cámara {cam_no}"
    d["runtime"]["gate_serial_gate"] = cam_no
    return d


def default_lane(lane_no: int = 1) -> dict[str, Any]:
    d = copy.deepcopy(LANE_DEF)
    d["enabled"] = (lane_no == 1)
    d["name"] = f"Carril {lane_no}"
    d["cameras"] = [default_camera(1), default_camera(2)]
    d["cameras"][1]["enabled"] = False
    return d


def default_side(name: str) -> dict[str, Any]:
    d = copy.deepcopy(SIDE_DEF)
    d["name"] = name
    d["lanes"] = [default_lane(1), default_lane(2), default_lane(3)]
    d["lanes"][1]["enabled"] = False
    d["lanes"][2]["enabled"] = False
    return d


def default_site() -> dict[str, Any]:
    d = copy.deepcopy(SITE_DEF)
    d["entry"] = default_side("Entrada")
    d["exit"] = default_side("Salida")
    return d


def normalize_wh_pair(pair: dict[str, Any] | None) -> dict[str, Any]:
    src = pair or {}
    out = copy.deepcopy(WH_PAIR_DEF)
    out.update({k: v for k, v in src.items() if k in out})
    out["url1"] = (out.get("url1") or "").strip()
    out["url2"] = (out.get("url2") or "").strip()
    out["send_snapshot1"] = parse_bool(out.get("send_snapshot1"), False)
    out["send_snapshot2"] = parse_bool(out.get("send_snapshot2"), False)
    out["snapshot_mode1"] = "json" if str(out.get("snapshot_mode1", "multipart")).strip().lower() == "json" else "multipart"
    out["snapshot_mode2"] = "json" if str(out.get("snapshot_mode2", "multipart")).strip().lower() == "json" else "multipart"
    return out


def normalize_wl_section(sec: dict[str, Any] | None) -> dict[str, Any]:
    src = sec or {}
    out = copy.deepcopy(WL_DEF)
    out.update({k: v for k, v in src.items() if k not in ("wh_active", "wh_inactive")})
    out["wh_active"] = normalize_wh_pair(src.get("wh_active"))
    out["wh_inactive"] = normalize_wh_pair(src.get("wh_inactive"))
    out["sheets_input"] = (out.get("sheets_input") or "").strip()
    out["search_start_col"] = col_to_idx(out.get("search_start_col", 14), 14)
    out["search_end_col"] = col_to_idx(out.get("search_end_col", 18), 18)
    if out["search_end_col"] < out["search_start_col"]:
        out["search_end_col"] = out["search_start_col"]
    out["status_col"] = col_to_idx(out.get("status_col", 3), 3)
    out["auto_refresh_min"] = clampi(out.get("auto_refresh_min", 0), 0, 1440, 0)
    out["disp_cols"] = norm_cols_any(out.get("disp_cols", [2, 3, 4]), 3)
    disp_titles = out.get("disp_titles", ["Folio", "Nombre", "Telefono"])
    if not isinstance(disp_titles, list):
        disp_titles = ["Folio", "Nombre", "Telefono"]
    disp_titles = list(disp_titles[:3]) + [""] * (3 - len(list(disp_titles[:3])))
    out["disp_titles"] = disp_titles
    return out


def normalize_tag_section(sec: dict[str, Any] | None) -> dict[str, Any]:
    src = sec or {}
    out = copy.deepcopy(TAG_DEF)
    out["lookup_format"] = "internal_hex" if str(src.get("lookup_format", "physical")).strip().lower() == "internal_hex" else "physical"
    out["owners"] = normalize_wl_section(src.get("owners"))
    out["wh_notfound"] = normalize_wh_pair(src.get("wh_notfound"))
    return out


def normalize_side_bases(bases: dict[str, Any] | None) -> dict[str, Any]:
    src = bases or {}
    out = copy.deepcopy(SIDE_BASES_DEF)
    out["owners"] = normalize_wl_section(src.get("owners"))
    out["visitors"] = normalize_wl_section(src.get("visitors"))
    out["tags"] = normalize_tag_section(src.get("tags"))
    out["wh_notfound"] = normalize_wh_pair(src.get("wh_notfound"))
    return out


def normalize_runtime(rt: dict[str, Any] | None, cam_no: int = 1) -> dict[str, Any]:
    src = rt or {}
    out = copy.deepcopy(CAMERA_RUNTIME_DEF)
    out.update({k: v for k, v in src.items() if k not in ("roi", "motion")})
    out["camera_mode"] = "manual" if str(out.get("camera_mode", "mac")).strip().lower() == "manual" else "mac"
    out["camera_mac"] = (out.get("camera_mac") or "").upper().replace("-", ":")
    out["camera_url"] = (out.get("camera_url") or "").strip()
    out["process_every_n"] = clampi(out.get("process_every_n", 2), 1, 30, 2)
    out["resize_max_w"] = clampi(out.get("resize_max_w", 1280), 64, 4096, 1280)
    out["alpr_topk"] = clampi(out.get("alpr_topk", 3), 1, 5, 3)
    out["min_confidence"] = clampf(out.get("min_confidence", 0.90), 0.0, 1.0, 0.90)
    out["idle_clear_sec"] = max(0.5, float(out.get("idle_clear_sec", 1.5)))
    out["det_min_confidence"] = clampf(out.get("det_min_confidence", 0.80), 0.0, 1.0, 0.80)
    out["stable_hits_required"] = clampi(out.get("stable_hits_required", 2), 1, 5, 2)
    out["notfound_stable_hits_required"] = clampi(out.get("notfound_stable_hits_required", 4), 1, 10, 4)
    out["suppress_notfound_after_auth_sec"] = clampi(out.get("suppress_notfound_after_auth_sec", 8), 0, 60, 8)
    out["latch_hold_sec"] = max(1.0, float(out.get("latch_hold_sec", 30.0)))
    out["pp_enabled"] = parse_bool(out.get("pp_enabled"), False)
    out["pp_profile"] = str(out.get("pp_profile", "none")).strip().lower()
    if out["pp_profile"] not in ("none", "bw_hicontrast_sharp"):
        out["pp_profile"] = "none"
    out["pp_clahe_clip"] = clampf(out.get("pp_clahe_clip", 2.0), 1.0, 4.0, 2.0)
    out["pp_sharp_strength"] = clampf(out.get("pp_sharp_strength", 0.55), 0.0, 1.2, 0.55)

    roi = copy.deepcopy(ROI_DEF)
    roi.update(src.get("roi") or {})
    roi["enabled"] = parse_bool(roi.get("enabled"), False)
    roi["x"] = clampf(roi.get("x", 0.0), 0.0, 1.0, 0.0)
    roi["y"] = clampf(roi.get("y", 0.0), 0.0, 1.0, 0.0)
    roi["w"] = clampf(roi.get("w", 1.0), 0.0, 1.0, 1.0)
    roi["h"] = clampf(roi.get("h", 1.0), 0.0, 1.0, 1.0)
    if roi["x"] + roi["w"] > 1.0:
        roi["w"] = max(0.0, 1.0 - roi["x"])
    if roi["y"] + roi["h"] > 1.0:
        roi["h"] = max(0.0, 1.0 - roi["y"])
    out["roi"] = roi

    motion = copy.deepcopy(MOTION_DEF)
    motion.update(src.get("motion") or {})
    motion["enabled"] = parse_bool(motion.get("enabled"), True)
    motion["pixel_change_pct"] = float(motion.get("pixel_change_pct", 2.0))
    motion["intensity_delta"] = clampi(motion.get("intensity_delta", 25), 1, 255, 25)
    motion["autobase_every_min"] = clampi(motion.get("autobase_every_min", 10), 1, 1440, 10)
    motion["autobase_samples"] = clampi(motion.get("autobase_samples", 3), 1, 5, 3)
    motion["autobase_interval_s"] = max(0.2, float(motion.get("autobase_interval_s", 1.0)))
    motion["cooldown_s"] = max(0.2, float(motion.get("cooldown_s", 2.0)))
    out["motion"] = motion

    out["gate_enabled"] = parse_bool(out.get("gate_enabled"), False)
    out["gate_auto_on_auth"] = parse_bool(out.get("gate_auto_on_auth"), False)
    out["gate_antispam_sec"] = clampi(out.get("gate_antispam_sec", 4), 1, 600, 4)
    out["gate_pulse_ms"] = clampi(out.get("gate_pulse_ms", 500), 20, 10000, 500)
    out["gate_mode"] = "http" if str(out.get("gate_mode", "serial")).strip().lower() == "http" else "serial"
    out["gate_url"] = norm_url_base(out.get("gate_url", ""))
    out["gate_token"] = (out.get("gate_token") or "").strip()
    out["gate_pin"] = clampi(out.get("gate_pin", 5), 1, 39, 5)
    out["gate_active_low"] = parse_bool(out.get("gate_active_low"), False)
    out["gate_serial_device"] = (out.get("gate_serial_device") or "").strip()
    out["gate_serial_baud"] = clampi(out.get("gate_serial_baud", 115200), 1200, 921600, 115200)
    out["gate_serial_gate"] = clampi(out.get("gate_serial_gate", cam_no), 1, 8, cam_no)
    out["wh_repeat_same_plate"] = parse_bool(out.get("wh_repeat_same_plate"), False)
    out["wh_min_gap_sec"] = clampi(out.get("wh_min_gap_sec", 0), 0, 3600, 0)
    return out


def normalize_camera(cam: dict[str, Any] | None, cam_no: int = 1) -> dict[str, Any]:
    src = cam or {}
    out = default_camera(cam_no)
    out["enabled"] = parse_bool(src.get("enabled"), out["enabled"])
    out["name"] = (src.get("name") or out["name"]).strip()
    out["role_label"] = (src.get("role_label") or "").strip()
    out["runtime"] = normalize_runtime(src.get("runtime"), cam_no)
    return out


def normalize_lane(lane: dict[str, Any] | None, lane_no: int = 1) -> dict[str, Any]:
    src = lane or {}
    out = default_lane(lane_no)
    out["enabled"] = parse_bool(src.get("enabled"), out["enabled"])
    out["name"] = (src.get("name") or out["name"]).strip()
    cams = src.get("cameras") if isinstance(src.get("cameras"), list) else []
    norm_cams = []
    for i in range(2):
        norm_cams.append(normalize_camera(cams[i] if i < len(cams) else None, i + 1))
    out["cameras"] = norm_cams
    return out


def normalize_side(side: dict[str, Any] | None, name: str) -> dict[str, Any]:
    src = side or {}
    out = default_side(name)
    out["enabled"] = parse_bool(src.get("enabled"), True)
    out["name"] = (src.get("name") or name).strip()
    out["bases"] = normalize_side_bases(src.get("bases"))
    lanes = src.get("lanes") if isinstance(src.get("lanes"), list) else []
    norm_lanes = []
    for i in range(3):
        norm_lanes.append(normalize_lane(lanes[i] if i < len(lanes) else None, i + 1))
    out["lanes"] = norm_lanes
    return out


def normalize_site(cfg: dict[str, Any] | None) -> dict[str, Any]:
    src = cfg or {}
    out = default_site()
    out["version"] = APP_VERSION
    out["site_name"] = (src.get("site_name") or out["site_name"]).strip()
    hb = src.get("heartbeat") or {}
    out["heartbeat"]["enabled"] = parse_bool(hb.get("enabled"), False)
    out["heartbeat"]["url"] = (hb.get("url") or "").strip()
    out["heartbeat"]["period_min"] = clampi(hb.get("period_min", 0), 0, 1440, 0)
    out["entry"] = normalize_side(src.get("entry"), "Entrada")
    out["exit"] = normalize_side(src.get("exit"), "Salida")
    return out


def migrate_legacy_cfg(legacy_cfg: dict[str, Any] | None) -> dict[str, Any]:
    site = default_site()
    if not isinstance(legacy_cfg, dict):
        return site

    if legacy_cfg.get("community_name"):
        site["site_name"] = str(legacy_cfg.get("community_name")).strip()

    if legacy_cfg.get("monitor_enabled") is not None:
        site["heartbeat"]["enabled"] = parse_bool(legacy_cfg.get("monitor_enabled"), False)
    if legacy_cfg.get("monitor_url"):
        site["heartbeat"]["url"] = str(legacy_cfg.get("monitor_url")).strip()
    if legacy_cfg.get("monitor_period_min") is not None:
        site["heartbeat"]["period_min"] = clampi(legacy_cfg.get("monitor_period_min", 0), 0, 1440, 0)

    cams = legacy_cfg.get("cameras") if isinstance(legacy_cfg.get("cameras"), list) else []
    mappings = [("entry", 0, 0), ("exit", 0, 0)]

    for legacy_idx in range(min(2, len(cams))):
        side_key, lane_idx, cam_idx = mappings[legacy_idx]
        lc = cams[legacy_idx] or {}
        side = site[side_key]
        lane = side["lanes"][lane_idx]
        cam = lane["cameras"][cam_idx]

        lane["enabled"] = True
        cam["enabled"] = True
        cam["name"] = f"Cámara {cam_idx + 1}"
        cam["runtime"] = normalize_runtime(lc, cam_idx + 1)

        side["bases"]["owners"] = normalize_wl_section(lc.get("owners"))
        side["bases"]["visitors"] = normalize_wl_section(lc.get("visitors"))
        side["bases"]["tags"] = normalize_tag_section(lc.get("tags"))
        side["bases"]["wh_notfound"] = normalize_wh_pair(lc.get("wh_notfound"))

    return site


def read_json_file(path: str) -> dict[str, Any] | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_cfg(cfg: dict[str, Any], cfg_file: str = DEFAULT_CFG_FILE) -> None:
    with open(cfg_file, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def load_cfg(cfg_file: str = DEFAULT_CFG_FILE, legacy_file: str = LEGACY_CFG_FILE) -> dict[str, Any]:
    v8 = read_json_file(cfg_file)
    if isinstance(v8, dict):
        cfg = normalize_site(v8)
        return cfg

    legacy = read_json_file(legacy_file)
    if isinstance(legacy, dict):
        cfg = migrate_legacy_cfg(legacy)
        cfg = normalize_site(cfg)
        return cfg

    return normalize_site(default_site())
