from __future__ import annotations

import json
import os
from typing import Any


CONFIG_PATH = os.path.expanduser("~/comunito_portal/config_v8.json")


# ============================================================
# DEFAULT CONFIG (V8)
# ============================================================

def default_config() -> dict[str, Any]:
    def cam_default(idx: int) -> dict[str, Any]:
        return {
            "name": f"Cámara {idx}",
            "role_label": "",
            "enabled": False,
            "runtime": {
                # Fuente de video
                "camera_mode": "mac",  # mac | manual
                "camera_url": "rtsp://user:pass@{CAM_IP}:554/stream1",
                "camera_mac": "",

                # ALPR
                "resize_max_w": 1280,
                "alpr_topk": 3,
                "min_confidence": 0.90,
                "det_min_confidence": 0.80,
                "stable_hits_required": 2,
                "notfound_stable_hits_required": 4,
                "process_every_n": 1,
                "suppress_notfound_after_auth_sec": 8,

                # ROI
                "roi": {
                    "enabled": False,
                    "x": 0.0,
                    "y": 0.0,
                    "w": 1.0,
                    "h": 1.0,
                },

                # Motion
                "motion": {
                    "enabled": True,
                    "pixel_change_pct": 2.0,
                    "intensity_delta": 25,
                    "cooldown_s": 2.0,
                    "autobase_samples": 3,
                    "autobase_interval_s": 1.0,
                    "autobase_every_min": 10,
                },

                # Preprocesado
                "pp_enabled": False,
                "pp_profile": "none",
                "pp_clahe_clip": 2.0,
                "pp_sharp_strength": 0.55,

                # Gate
                "gate_enabled": False,
                "gate_mode": "serial",  # serial | http
                "gate_auto_on_auth": True,
                "gate_antispam_sec": 4,

                # Serial
                "gate_serial_device": "",
                "gate_serial_baud": 115200,
                "gate_serial_gate": idx,

                # HTTP
                "gate_url": "",
                "gate_token": "",
                "gate_pin": 5,
                "gate_active_low": False,
                "gate_pulse_ms": 500,

                # Webhooks
                "wh_min_gap_sec": 0,
                "wh_repeat_same_plate": False,

                # UI
                "latch_hold_sec": 30.0,
            },
        }

    def lane_default() -> dict[str, Any]:
        return {
            "name": "Carril",
            "enabled": False,
            "cameras": [
                cam_default(1),
                cam_default(2),
            ],
        }

    def bases_default() -> dict[str, Any]:
        def wl_section():
            return {
                "sheets_input": "",
                "search_start_col": 14,
                "search_end_col": 18,
                "status_col": 3,
                "disp_cols": [2, 3, 4],
                "disp_titles": ["Folio", "Nombre", "Telefono"],
                "auto_refresh_min": 0,
                "wh_active": {
                    "url1": "",
                    "url2": "",
                    "send_snapshot1": False,
                    "send_snapshot2": False,
                    "snapshot_mode1": "multipart",
                    "snapshot_mode2": "multipart",
                },
                "wh_inactive": {
                    "url1": "",
                    "url2": "",
                    "send_snapshot1": False,
                    "send_snapshot2": False,
                    "snapshot_mode1": "multipart",
                    "snapshot_mode2": "multipart",
                },
            }

        def tag_section():
            return {
                "lookup_format": "physical",  # physical | internal_hex
                "owners": wl_section(),
                "wh_notfound": {
                    "url1": "",
                    "url2": "",
                    "send_snapshot1": False,
                    "send_snapshot2": False,
                    "snapshot_mode1": "multipart",
                    "snapshot_mode2": "multipart",
                },
            }

        return {
            "owners": wl_section(),
            "visitors": wl_section(),
            "tags": tag_section(),
            "wh_notfound": {
                "url1": "",
                "url2": "",
                "send_snapshot1": False,
                "send_snapshot2": False,
                "snapshot_mode1": "multipart",
                "snapshot_mode2": "multipart",
            },
        }

    return {
        "site_name": "Acceso Principal",
        "api_token": "",
        "entry": {
            "name": "Entrada",
            "enabled": True,
            "lanes": [
                lane_default(),
                lane_default(),
                lane_default(),
            ],
            "bases": bases_default(),
        },
        "exit": {
            "name": "Salida",
            "enabled": True,
            "lanes": [
                lane_default(),
                lane_default(),
                lane_default(),
            ],
            "bases": bases_default(),
        },
    }


# ============================================================
# LOAD / SAVE
# ============================================================

def load_config() -> dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        cfg = default_config()
        save_config(cfg)
        return cfg

    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = default_config()
        save_config(cfg)
        return cfg

    # merge básico por si faltan llaves nuevas
    return merge_dict(default_config(), cfg)


def save_config(cfg: dict[str, Any]):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


# ============================================================
# MERGE UTIL
# ============================================================

def merge_dict(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = merge_dict(out[k], v)
        else:
            out[k] = v
    return out


# ============================================================
# HELPERS PARA SETTINGS (OPCIONAL FUTURO UI)
# ============================================================

def get_camera(cfg: dict, side: str, lane: int, cam: int) -> dict:
    return cfg[side]["lanes"][lane - 1]["cameras"][cam - 1]


def set_camera(cfg: dict, side: str, lane: int, cam: int, data: dict):
    cfg[side]["lanes"][lane - 1]["cameras"][cam - 1] = data


def enable_lane(cfg: dict, side: str, lane: int, enabled: bool):
    cfg[side]["lanes"][lane - 1]["enabled"] = bool(enabled)


def enable_camera(cfg: dict, side: str, lane: int, cam: int, enabled: bool):
    cfg[side]["lanes"][lane - 1]["cameras"][cam - 1]["enabled"] = bool(enabled)


def set_site_name(cfg: dict, name: str):
    cfg["site_name"] = str(name or "").strip() or "Acceso"


def set_api_token(cfg: dict, token: str):
    cfg["api_token"] = str(token or "").strip()
