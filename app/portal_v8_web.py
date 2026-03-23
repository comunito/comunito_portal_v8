from __future__ import annotations

import datetime
import ipaddress
import socket
import subprocess
import time
from typing import Any

import cv2
from flask import Flask, Response, jsonify, render_template_string, request

from app.portal_v8_runtime import (
    RuntimeContext,
    apply_roi,
    gate_fire,
    materialize_camera_url,
    ping_ip,
    preprocess_for_alpr,
    process_tag_event,
    resolve_ip_by_mac,
    side_label,
    download_side_wl,
    download_side_tag_wl,
)

TZ_NAME = "America/Mexico_City"


def create_app(rt: RuntimeContext) -> Flask:
    app = Flask(__name__)
    app.config["RUNTIME"] = rt

    def _check_token() -> bool:
        want = str(rt.cfg.get("api_token", "") or "").strip()
        if not want:
            return True
        got = (request.headers.get("X-API-Key") or request.args.get("api_key") or "").strip()
        return got == want

    def _json_nocache(payload: Any, status: int = 200):
        r = jsonify(payload)
        r.status_code = status
        r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        r.headers["Pragma"] = "no-cache"
        r.headers["Expires"] = "0"
        return r

    def _iso_now():
        try:
            from zoneinfo import ZoneInfo
            return datetime.datetime.now(tz=ZoneInfo(TZ_NAME)).isoformat()
        except Exception:
            return datetime.datetime.now().isoformat()

    def sh(cmd: str) -> tuple[int, str]:
        try:
            out = subprocess.check_output(cmd, shell=True, stderr=subprocess.STDOUT, text=True)
            return 0, out
        except subprocess.CalledProcessError as e:
            return e.returncode, e.output

    def _extract_host_port(rtsp_url: str):
        u = (rtsp_url or "").strip()
        if not u:
            return "", 554
        try:
            from urllib.parse import urlparse
            p = urlparse(u)
            host = (p.hostname or "").strip()
            port = int(p.port or 554)
            return host, port
        except Exception:
            pass
        try:
            x = u
            if "@" in x:
                x = x.split("@", 1)[1]
            x = x.split("/", 1)[0]
            if ":" in x:
                host, port = x.split(":", 1)
                return host.strip(), int(port.strip() or 554)
            return x.strip(), 554
        except Exception:
            return "", 554

    def _is_ip(host: str) -> bool:
        host = (host or "").strip()
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return True
        except Exception:
            return False

    def _resolve_host(host: str) -> str:
        host = (host or "").strip()
        if not host:
            return ""
        if _is_ip(host):
            return host
        try:
            return socket.gethostbyname(host)
        except Exception:
            return ""

    def _tcp_ok(ip: str, port: int, timeout: float = 0.6) -> bool:
        if not ip:
            return False
        try:
            with socket.create_connection((ip, int(port)), timeout=timeout):
                return True
        except Exception:
            return False

    def _camera_conn_status(side: str, lane_no: int, cam_no: int):
        cam_cfg = rt.get_camera_cfg(side, lane_no, cam_no)
        runtime = cam_cfg["runtime"]
        mode = (runtime.get("camera_mode", "mac") or "mac").lower()

        if mode == "mac":
            mac = (runtime.get("camera_mac", "") or "").strip()
            if not mac:
                return {
                    "ok": False,
                    "ip": "",
                    "host": "",
                    "mode": "mac",
                    "port": 554,
                    "tcp": False,
                    "source": "",
                }
            ip = resolve_ip_by_mac(rt, mac)
            tcp = _tcp_ok(ip, 554, timeout=0.6) if ip else False
            ok = tcp or (bool(ip) and ping_ip(ip, 1))
            return {
                "ok": ok,
                "ip": ip or "",
                "host": "",
                "mode": "mac",
                "port": 554,
                "tcp": tcp,
                "source": "LAN-MAC",
            }

        url = (runtime.get("camera_url", "") or "").strip()
        host, port = _extract_host_port(url)
        ip = _resolve_host(host) if host else ""
        tcp = _tcp_ok(ip, port, timeout=0.6) if ip else False
        ok = tcp or (bool(ip) and ping_ip(ip, 1))
        return {
            "ok": ok,
            "ip": ip or "",
            "host": host or "",
            "mode": "manual",
            "port": int(port or 554),
            "tcp": tcp,
            "source": "MANUAL",
        }

    def _camera_public_state(side: str, lane_no: int, cam_no: int):
        camera_key = rt.camera_key(side, lane_no, cam_no)
        rt.ensure_camera_runtime_objects(camera_key)

        cam_cfg = rt.get_camera_cfg(side, lane_no, cam_no)
        runtime = cam_cfg["runtime"]
        lock = rt.camera_state_locks[camera_key]

        with lock:
            st = dict(rt.camera_states[camera_key])

        conn = _camera_conn_status(side, lane_no, cam_no)
        mot = rt.motion_states[camera_key]
        send_mgr = rt.send_managers[camera_key]

        hold = 2.0
        now = time.time()

        if (not st.get("plate")) or ((now - float(st.get("ts") or 0)) > hold):
            st["plate"] = ""
            st["conf"] = 0.0
            st["auth"] = False
            st["display"] = ["", "", ""]
            st["user_type"] = "NONE"
            st["cat"] = "NONE"

        raw_hold = 4.0
        if (now - float(st.get("raw_ts") or 0)) > raw_hold:
            st["raw_candidates"] = []

        if (not st.get("tag")) or ((now - float(st.get("tag_ts") or 0)) > hold):
            st["tag"] = ""
            st["tag_cat"] = "NONE"
            st["tag_fields"] = ["", "", ""]

        return {
            "side": side,
            "side_label": side_label(side),
            "lane": lane_no,
            "camera": cam_no,
            "camera_name": cam_cfg.get("name", f"Cámara {cam_no}"),
            "role_label": cam_cfg.get("role_label", ""),
            "enabled": bool(cam_cfg.get("enabled", False)),
            "plate": st.get("plate", ""),
            "conf": float(st.get("conf", 0.0) or 0.0),
            "ts": float(st.get("ts", 0.0) or 0.0),
            "category": st.get("cat", "NONE"),
            "user_type": st.get("user_type", "NONE"),
            "fields": list(st.get("display", []) or []),
            "titles": list(st.get("titles", []) or []),
            "raw_candidates": list(st.get("raw_candidates", []) or []),
            "tag": st.get("tag", ""),
            "tag_ts": float(st.get("tag_ts", 0.0) or 0.0),
            "tag_cat": st.get("tag_cat", "NONE"),
            "tag_fields": list(st.get("tag_fields", []) or []),
            "hold": hold,
            "conn": conn,
            "motion": {
                "active": bool(mot.active),
                "ratio": float(mot.last_ratio),
            },
            "queue": {
                "pending": send_mgr.q.qsize(),
                "dropped": send_mgr.dropped,
                "sent": send_mgr.sent,
            },
            "snapshot_url": f"/snapshot.jpg?side={side}&lane={lane_no}&cam={cam_no}&w=640",
            "snapshot_alpr_url": f"/snapshot_alpr.jpg?side={side}&lane={lane_no}&cam={cam_no}&w=640",
            "alpr_debug_url": f"/api/alpr_debug?side={side}&lane={lane_no}&cam={cam_no}",
        }

    def _home_payload():
        _, out = sh("nmcli -t -f ACTIVE,SSID,SIGNAL dev wifi | grep '^yes' || true")
        ssid = ""
        signal = ""
        for line in out.strip().splitlines():
            parts = line.split(":")
            if parts and parts[0] == "yes":
                ssid = parts[1] if len(parts) > 1 else ""
                signal = parts[2] if len(parts) > 2 else ""
                break

        _, ipout = sh("hostname -I | awk '{print $1}' || true")
        main_ip = (ipout or "").strip()

        sides = []
        for side in ("entry", "exit"):
            side_cfg = rt.cfg[side]
            side_out = {
                "key": side,
                "label": side_label(side),
                "name": side_cfg.get("name", side_label(side)),
                "enabled": bool(side_cfg.get("enabled", True)),
                "lanes": [],
            }
            if side_out["enabled"]:
                for lane_no, lane in enumerate(side_cfg.get("lanes", []), start=1):
                    if not lane.get("enabled", False):
                        continue
                    lane_out = {
                        "lane": lane_no,
                        "name": lane.get("name", f"Carril {lane_no}"),
                        "enabled": True,
                        "cameras": [],
                    }
                    for cam_no, cam in enumerate(lane.get("cameras", []), start=1):
                        if not cam.get("enabled", False):
                            continue
                        lane_out["cameras"].append(_camera_public_state(side, lane_no, cam_no))
                    if lane_out["cameras"]:
                        side_out["lanes"].append(lane_out)
            sides.append(side_out)

        return {
            "site_name": rt.cfg.get("site_name", "Acceso Principal"),
            "generated_at": _iso_now(),
            "net": {
                "ssid": ssid,
                "signal": signal,
                "ip": main_ip,
            },
            "sys": {
                "temp_c": rt.sys_status.get("temp_c"),
                "cpu_pct": rt.sys_status.get("cpu_pct"),
            },
            "gate_serial": rt.gate_serial.status(),
            "sides": sides,
        }

    HOME_HTML = """
<style>
 body{font-family:system-ui;margin:16px;background:#fafafa}
 h1{margin:0 0 8px}
 .net{font-size:13px;color:#444;margin-bottom:10px}
 .toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
 .btn{padding:7px 11px;border:1px solid #888;border-radius:10px;background:#f5f5f5;cursor:pointer;text-decoration:none;color:#111}
 .side-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
 .card{border:1px solid #ddd;border-radius:14px;padding:12px;background:#fff}
 .lane{border:1px dashed #ccc;border-radius:12px;padding:10px;margin-top:10px;background:#fcfcfc}
 .cam{border:1px solid #e8e8e8;border-radius:12px;padding:10px;margin-top:10px;background:#fff}
 .plate{font-size:28px;font-weight:800;border:2px solid #111;border-radius:12px;padding:4px 10px;background:#fff;display:inline-block;min-width:180px;text-align:center}
 .tag{font-size:18px;font-weight:700;border:2px dashed #333;border-radius:10px;padding:4px 10px;background:#f7f7f7;display:inline-block;min-width:160px;text-align:center}
 .muted{color:#666;font-size:12px}
 .ok{color:#2e7d32;font-weight:700}
 .bad{color:#c62828;font-weight:700}
 .chip{display:inline-block;padding:2px 8px;border-radius:999px;background:#efefef;font-size:12px;margin-left:6px}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin:0 6px;vertical-align:middle}
 .on{background:#28a745}.off{background:#999}
 .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
</style>

<h1 id="site_name">Comunito v8</h1>

<div class="net">
  Wi-Fi: <b id="net_ssid">—</b> • Señal: <b id="net_sig">—</b>% • IP: <b id="net_ip">—</b>
  &nbsp; | &nbsp; Temp: <b id="sys_temp">—</b>°C • CPU: <b id="sys_cpu">—</b>%
  &nbsp; | &nbsp; Gate Serial: <b id="gs_conn">—</b> <span class="muted" id="gs_dev"></span>
</div>

<div class="toolbar">
  <a class="btn" href="/settings">⚙️ Configuración</a>
  <a class="btn" href="/settings/side/entry">Entrada</a>
  <a class="btn" href="/settings/side/exit">Salida</a>
  <a class="btn" href="/wifi">📶 Redes conocidas</a>
</div>

<div class="side-grid" id="side_grid"></div>

<script>
function clsCat(cat){
  if(cat === 'ACTIVE') return 'ok';
  if(cat === 'INACTIVE') return 'bad';
  return 'muted';
}
function timeText(ts){
  try{
    if(!ts) return '—';
    return new Date(ts * 1000).toLocaleTimeString();
  }catch(e){ return '—'; }
}
async function gateOpen(side, lane, cam){
  try{
    const r = await fetch(`/api/gate_open?side=${side}&lane=${lane}&cam=${cam}`, {method:'POST'});
    const j = await r.json();
    if(!j.ok){
      alert('Error gate: ' + (j.error || ''));
    }
  }catch(e){
    alert('Error gate: ' + e);
  }
}
function renderCam(c){
  const fields = (c.fields || []).filter(Boolean).join(' • ');
  const tagFields = (c.tag_fields || []).filter(Boolean).join(' • ');
  const wlTxt = (c.category === 'ACTIVE') ? 'EN WHITELIST (ACTIVO)' : ((c.category === 'INACTIVE') ? 'EN WHITELIST (INACTIVO)' : 'NOFOUND');
  const tagTxt = (c.tag_cat === 'ACTIVE') ? 'TAG ACTIVO' : ((c.tag_cat === 'INACTIVE') ? 'TAG INACTIVO' : 'TAG NOFOUND');
  const conn = c.conn || {};
  return `
    <div class="cam">
      <div class="row">
        <div><b>${c.camera_name}</b></div>
        ${c.role_label ? `<span class="chip">${c.role_label}</span>` : ''}
        <span class="chip">${conn.mode || '—'}</span>
        <span class="${conn.ok ? 'ok' : 'bad'}">${conn.ok ? 'Conectada' : 'Sin conexión'}</span>
      </div>

      <div class="muted" style="margin-top:4px">
        IP cámara: <b>${conn.ip || '—'}</b>
        ${conn.host ? ` • Host: <b>${conn.host}</b>` : ''}
        • Puerto: <b>${conn.port || '—'}</b>
        • TCP: <b>${conn.tcp ? 'OK' : 'NO'}</b>
      </div>

      <div style="margin-top:8px">
        <div class="plate">${c.plate || 'Sin placa'}</div>
      </div>
      <div class="muted">Conf: ${Number((c.conf || 0) * 100).toFixed(1)}% • Hora: ${timeText(c.ts)}</div>
      <div>Usuario: <b>${c.user_type || '—'}</b> • WL: <span class="${clsCat(c.category)}">${wlTxt}</span></div>
      <div class="muted">${fields || '—'}</div>
      <div class="muted" style="margin-top:4px">
        RAW: ${
          (c.raw_candidates && c.raw_candidates.length)
            ? c.raw_candidates.map(x => `${x.text} (${Number((x.ocr_conf||0)*100).toFixed(0)}% OCR / ${Number((x.det_conf||0)*100).toFixed(0)}% DET)`).join(' • ')
            : '—'
        }
      </div>

      <div style="margin-top:8px">
        <div class="tag">${c.tag || '—'}</div>
      </div>
      <div>Tag: <span class="${clsCat(c.tag_cat)}">${tagTxt}</span></div>
      <div class="muted">${tagFields || '—'}</div>

      <div class="muted" style="margin-top:6px">
        Motion <span class="dot ${c.motion?.active ? 'on' : 'off'}"></span>
        Δpix: <b>${Number(c.motion?.ratio || 0).toFixed(2)}</b>%
        • Cola: <b>${c.queue?.pending ?? '—'} pend / ${c.queue?.dropped ?? 0} drop / ${c.queue?.sent ?? 0} sent</b>
      </div>

      <div class="row" style="margin-top:8px">
        <a class="btn" href="${c.snapshot_url}" target="_blank">📸 Snapshot</a>
        <a class="btn" href="${c.snapshot_alpr_url}" target="_blank">🧪 ALPR</a>
        <a class="btn" href="/roi?side=${c.side}&lane=${c.lane}&cam=${c.camera}" target="_blank">✂ ROI</a>
        <button class="btn" onclick="gateOpen('${c.side}', ${c.lane}, ${c.camera})">🟩 Gate</button>
      </div>
    </div>
  `;
}
function renderLane(l){
  return `
    <div class="lane">
      <div><b>${l.name}</b></div>
      ${(l.cameras || []).map(renderCam).join('')}
    </div>
  `;
}
function renderSide(s){
  return `
    <div class="card">
      <h3 style="margin:0 0 6px">${s.name}</h3>
      ${!(s.enabled) ? '<div class="muted">Deshabilitado.</div>' : ''}
      ${(s.lanes || []).length ? (s.lanes || []).map(renderLane).join('') : '<div class="muted">Sin carriles/cámaras habilitados.</div>'}
    </div>
  `;
}
async function poll(){
  try{
    const j = await (await fetch('/api/home_status?ts=' + Date.now(), {cache:'no-store'})).json();

    document.getElementById('site_name').textContent = j.site_name || 'Comunito v8';
    document.title = (j.site_name || 'Comunito v8');

    document.getElementById('net_ssid').textContent = j.net?.ssid || '—';
    document.getElementById('net_sig').textContent = j.net?.signal || '—';
    document.getElementById('net_ip').textContent = j.net?.ip || '—';

    document.getElementById('sys_temp').textContent = (j.sys?.temp_c == null ? '—' : Number(j.sys.temp_c).toFixed(1));
    document.getElementById('sys_cpu').textContent = (j.sys?.cpu_pct == null ? '—' : Number(j.sys.cpu_pct).toFixed(1));

    document.getElementById('gs_conn').textContent = (j.gate_serial?.connected ? 'Conectado' : 'No');
    document.getElementById('gs_conn').className = j.gate_serial?.connected ? 'ok' : 'bad';
    document.getElementById('gs_dev').textContent = j.gate_serial?.device ? '(' + j.gate_serial.device + ')' : '';

    document.getElementById('side_grid').innerHTML = (j.sides || []).map(renderSide).join('');
  }catch(e){}
}
setInterval(poll, 2500);
poll();
</script>
"""

    @app.route("/")
    def home():
        return render_template_string(HOME_HTML)

    @app.route("/api/home_status")
    def api_home_status():
        return _json_nocache(_home_payload())

    @app.route("/api/net")
    def api_net():
        _, out = sh("nmcli -t -f ACTIVE,SSID,SIGNAL dev wifi | grep '^yes' || true")
        ssid = ""
        signal = ""
        for line in out.strip().splitlines():
            parts = line.split(":")
            if parts and parts[0] == "yes":
                ssid = parts[1] if len(parts) > 1 else ""
                signal = parts[2] if len(parts) > 2 else ""
                break
        _, ipout = sh("hostname -I | awk '{print $1}' || true")
        return _json_nocache({"ssid": ssid, "signal": signal, "ip": (ipout or "").strip()})

    @app.route("/api/sys")
    def api_sys():
        return _json_nocache({
            "temp_c": rt.sys_status.get("temp_c"),
            "cpu_pct": rt.sys_status.get("cpu_pct"),
        })

    @app.route("/api/gate_serial_status")
    def api_gate_serial_status():
        return _json_nocache(rt.gate_serial.status())

    @app.route("/api/status")
    def api_status():
        side = (request.args.get("side") or "entry").strip().lower()
        lane = int(request.args.get("lane", "1") or "1")
        cam = int(request.args.get("cam", "1") or "1")

        if side not in ("entry", "exit"):
            return _json_nocache({"error": "side inválido"}, 400)
        if lane < 1 or lane > 3:
            return _json_nocache({"error": "lane inválido"}, 400)
        if cam < 1 or cam > 2:
            return _json_nocache({"error": "cam inválida"}, 400)

        return _json_nocache(_camera_public_state(side, lane, cam))

    @app.route("/api/lan")
    def api_lan():
        out = {}
        for side in ("entry", "exit"):
            side_cfg = rt.cfg[side]
            for lane_no, lane in enumerate(side_cfg.get("lanes", []), start=1):
                for cam_no, cam in enumerate(lane.get("cameras", []), start=1):
                    key = f"{side}:{lane_no}:{cam_no}"
                    if not cam.get("enabled", False):
                        out[key] = {
                            "ok": False,
                            "ip": "",
                            "host": "",
                            "mode": "",
                            "port": 554,
                            "tcp": False,
                        }
                    else:
                        out[key] = _camera_conn_status(side, lane_no, cam_no)
        return _json_nocache(out)

    @app.route("/snapshot.jpg")
    def snapshot():
        side = (request.args.get("side") or "entry").strip().lower()
        lane = int(request.args.get("lane", "1") or "1")
        cam = int(request.args.get("cam", "1") or "1")
        if side not in ("entry", "exit"):
            return ("side inválido", 400, {"Content-Type": "text/plain"})
        if lane < 1 or lane > 3 or cam < 1 or cam > 2:
            return ("lane/cam inválido", 400, {"Content-Type": "text/plain"})

        camera_key = rt.camera_key(side, lane, cam)
        rt.ensure_camera_runtime_objects(camera_key)
        fr = rt.video_sources[camera_key].get()
        if fr is None:
            return ("No frame", 503, {"Content-Type": "text/plain"})

        try:
            w = int(request.args.get("w", "0"))
        except Exception:
            w = 0

        fr2 = fr
        if w > 32:
            h, wi = fr2.shape[:2]
            tw = min(w, wi)
            th = int(h * (tw / float(wi)))
            fr2 = cv2.resize(fr2, (tw, th), interpolation=cv2.INTER_AREA)

        ok, buf = cv2.imencode(".jpg", fr2, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok:
            return ("Encode error", 500, {"Content-Type": "text/plain"})

        r = Response(buf.tobytes(), mimetype="image/jpeg")
        r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, no-transform"
        return r

    @app.route("/snapshot_alpr.jpg")
    def snapshot_alpr():
        side = (request.args.get("side") or "entry").strip().lower()
        lane = int(request.args.get("lane", "1") or "1")
        cam = int(request.args.get("cam", "1") or "1")
        if side not in ("entry", "exit"):
            return ("side inválido", 400, {"Content-Type": "text/plain"})
        if lane < 1 or lane > 3 or cam < 1 or cam > 2:
            return ("lane/cam inválido", 400, {"Content-Type": "text/plain"})

        camera_key = rt.camera_key(side, lane, cam)
        rt.ensure_camera_runtime_objects(camera_key)
        cam_cfg = rt.get_camera_cfg(side, lane, cam)

        fr = rt.video_sources[camera_key].get()
        if fr is None:
            return ("No frame", 503, {"Content-Type": "text/plain"})

        try:
            fr_roi = apply_roi(cam_cfg, fr)
        except Exception:
            fr_roi = fr

        try:
            fr_alpr = preprocess_for_alpr(cam_cfg, fr_roi)
        except Exception:
            fr_alpr = fr_roi

        try:
            w = int(request.args.get("w", "0"))
        except Exception:
            w = 0

        fr2 = fr_alpr
        if w and w > 32:
            h, wi = fr2.shape[:2]
            tw = min(w, wi)
            th = int(max(24, h * (tw / float(wi))))
            try:
                fr2 = cv2.resize(fr2, (tw, th), interpolation=cv2.INTER_AREA)
            except Exception:
                pass

        ok, buf = cv2.imencode(".jpg", fr2, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok:
            return ("Encode error", 500, {"Content-Type": "text/plain"})

        r = Response(buf.tobytes(), mimetype="image/jpeg")
        r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0, no-transform"
        return r

    @app.route("/api/alpr_debug")
    def api_alpr_debug():
        side = (request.args.get("side") or "entry").strip().lower()
        lane = int(request.args.get("lane", "1") or "1")
        cam = int(request.args.get("cam", "1") or "1")
        if side not in ("entry", "exit"):
            return _json_nocache({"ok": False, "error": "side inválido"}, 400)
        if lane < 1 or lane > 3 or cam < 1 or cam > 2:
            return _json_nocache({"ok": False, "error": "lane/cam inválido"}, 400)

        from portal_v8_runtime import run_alpr

        camera_key = rt.camera_key(side, lane, cam)
        rt.ensure_camera_runtime_objects(camera_key)
        cam_cfg = rt.get_camera_cfg(side, lane, cam)

        fr = rt.video_sources[camera_key].get()
        if fr is None:
            return _json_nocache({"ok": False, "error": "no-frame"}, 503)

        try:
            fr_roi = apply_roi(cam_cfg, fr)
        except Exception as e:
            return _json_nocache({"ok": False, "error": f"roi-error: {e}"}, 500)

        try:
            fr_alpr = preprocess_for_alpr(cam_cfg, fr_roi)
        except Exception as e:
            return _json_nocache({"ok": False, "error": f"preprocess-error: {e}"}, 500)

        try:
            results = run_alpr(rt, fr_alpr, cam_cfg["runtime"]["resize_max_w"], topk=cam_cfg["runtime"]["alpr_topk"])
        except Exception as e:
            return _json_nocache({"ok": False, "error": f"run_alpr-error: {e}"}, 500)

        out = []
        for item in results:
            try:
                txt, conf, det_conf = item
                out.append({"text": str(txt), "conf": float(conf), "det_conf": float(det_conf)})
            except Exception:
                pass

        return _json_nocache({
            "ok": True,
            "side": side,
            "lane": lane,
            "cam": cam,
            "frame_shape": (list(fr.shape) if fr is not None else None),
            "roi_shape": (list(fr_roi.shape) if fr_roi is not None else None),
            "alpr_shape": (list(fr_alpr.shape) if fr_alpr is not None else None),
            "count": len(out),
            "results": out,
            "cfg": {
                "camera_mode": cam_cfg["runtime"].get("camera_mode"),
                "resize_max_w": cam_cfg["runtime"].get("resize_max_w"),
                "min_confidence": cam_cfg["runtime"].get("min_confidence"),
                "roi_enabled": cam_cfg["runtime"].get("roi", {}).get("enabled"),
                "motion_enabled": cam_cfg["runtime"].get("motion", {}).get("enabled"),
                "pp_enabled": cam_cfg["runtime"].get("pp_enabled"),
            },
        })

    @app.route("/api/gate_open", methods=["POST"])
    def api_gate_open():
        if not _check_token():
            return _json_nocache({"ok": False, "error": "unauthorized"}, 401)

        side = (request.args.get("side") or "entry").strip().lower()
        lane = int(request.args.get("lane", "1") or "1")
        cam = int(request.args.get("cam", "1") or "1")

        if side not in ("entry", "exit"):
            return _json_nocache({"ok": False, "error": "side inválido"}, 400)
        if lane < 1 or lane > 3:
            return _json_nocache({"ok": False, "error": "lane inválido"}, 400)
        if cam < 1 or cam > 2:
            return _json_nocache({"ok": False, "error": "cam inválida"}, 400)

        camera_key = rt.camera_key(side, lane, cam)
        ok, msg = gate_fire(rt, camera_key)
        return _json_nocache({"ok": ok, "error": (None if ok else msg)}, 200 if ok else 500)

    @app.route("/api/wl_refresh", methods=["POST"])
    def api_wl_refresh():
        if not _check_token():
            return _json_nocache({"ok": False, "error": "unauthorized"}, 401)
        side = (request.args.get("side") or "entry").strip().lower()
        kind = (request.args.get("kind") or "owners").strip().lower()
        if side not in ("entry", "exit"):
            return _json_nocache({"ok": False, "error": "side inválido"}, 400)
        if kind not in ("owners", "visitors"):
            return _json_nocache({"ok": False, "error": "kind inválido"}, 400)

        msg = download_side_wl(rt, side, kind)
        return _json_nocache({"ok": True, "message": msg})

    @app.route("/api/tag_wl_refresh", methods=["POST"])
    def api_tag_wl_refresh():
        if not _check_token():
            return _json_nocache({"ok": False, "error": "unauthorized"}, 401)
        side = (request.args.get("side") or "entry").strip().lower()
        if side not in ("entry", "exit"):
            return _json_nocache({"ok": False, "error": "side inválido"}, 400)

        msg = download_side_tag_wl(rt, side)
        return _json_nocache({"ok": True, "message": msg})

    @app.route("/api/tag_event", methods=["POST"])
    def api_tag_event():
        body = request.get_json(force=True, silent=True) or {}
        side = (body.get("side") or "entry").strip().lower()
        lane = int(body.get("lane", 1) or 1)
        cam = int(body.get("cam", 1) or 1)

        if side not in ("entry", "exit"):
            return _json_nocache({"ok": False, "error": "side inválido"}, 400)
        if lane < 1 or lane > 3:
            return _json_nocache({"ok": False, "error": "lane inválido"}, 400)
        if cam < 1 or cam > 2:
            return _json_nocache({"ok": False, "error": "cam inválida"}, 400)

        res = process_tag_event(rt, side, lane, cam, body)
        return _json_nocache(res)


    ROI_HTML = """<!doctype html><meta charset="utf-8"><title>ROI</title>
<style>
 body{font-family:system-ui;margin:16px;background:#fafafa}
 .row{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
 .card{border:1px solid #ddd;border-radius:12px;padding:12px;background:#fff}
 .btn{padding:8px 12px;border:1px solid #888;border-radius:10px;background:#f5f5f5;cursor:pointer;text-decoration:none;color:#111}
 .muted{color:#666;font-size:12px}
 canvas{max-width:100%; height:auto; border:1px solid #aaa; border-radius:8px}
 img.preview{max-width:100%; height:auto; border:1px solid #aaa; border-radius:8px}
 .col{min-width:320px; flex:1}
 .title{font-weight:700;margin-bottom:8px}
</style>
<h2>Definir ROI — {{side_label}} / Carril {{lane}} / Cámara {{cam}}</h2>

<div class="row">
  <div class="card col">
    <div class="title">1) Dibuja ROI sobre imagen normal</div>
    <img id="raw" src="" crossorigin="anonymous" style="display:none"/>
    <canvas id="cnv"></canvas>

    <div style="margin-top:8px">
      <label><input type="checkbox" id="enabled"> Habilitar ROI</label>
      <button class="btn" id="saveBtn">💾 Guardar ROI</button>
      <button class="btn" id="clearBtn">🧹 Limpiar</button>
      <a class="btn" href="/">⬅ Volver</a>
      <span class="muted" id="msg"></span>
    </div>

    <div class="muted" style="margin-top:6px">
      Arrastra para dibujar el rectángulo. Se guarda normalizado.
    </div>
  </div>

  <div class="card col">
    <div class="title">2) Vista ALPR en vivo (ROI + preprocesado)</div>
    <img id="proc" class="preview" src="" alt="Vista ALPR"/>
    <div class="muted" style="margin-top:6px">
      Esta vista muestra recorte ROI + preprocesado.
    </div>
    <div class="muted" style="margin-top:6px"><b>ROI actual</b><pre id="cur"></pre></div>
  </div>
</div>

<script>
const side="{{side}}";
const lane={{lane}};
const cam={{cam}};
const img=document.getElementById('raw');
const cnv=document.getElementById('cnv');
const ctx=cnv.getContext('2d');
const proc=document.getElementById('proc');

let roi={x:0,y:0,w:1,h:1,enabled:false};
let dragging=false, sx=0, sy=0, ex=0, ey=0;

function draw(){
  if(!img.naturalWidth){return;}
  cnv.width = img.naturalWidth; cnv.height = img.naturalHeight;
  ctx.drawImage(img,0,0);
  if(roi.w>0 && roi.h>0){
    ctx.lineWidth=2; ctx.strokeStyle='rgba(0,200,0,0.9)';
    ctx.setLineDash([6,4]);
    ctx.strokeRect(roi.x*cnv.width, roi.y*cnv.height, roi.w*cnv.width, roi.h*cnv.height);
    ctx.setLineDash([]);
  }
  if(dragging){
    const x=Math.min(sx,ex), y=Math.min(sy,ey);
    const w=Math.abs(ex-sx), h=Math.abs(ey-sy);
    ctx.lineWidth=2; ctx.strokeStyle='rgba(255,140,0,0.9)';
    ctx.strokeRect(x,y,w,h);
  }
}

function toCanvasXY(e){
  const r = cnv.getBoundingClientRect();
  const cx = (e.clientX - r.left);
  const cy = (e.clientY - r.top);
  const sx2 = (cnv.width  / Math.max(1, r.width));
  const sy2 = (cnv.height / Math.max(1, r.height));
  return {x: cx*sx2, y: cy*sy2};
}

function startDrag(e){
  e.preventDefault();
  const p=toCanvasXY(e);
  sx=p.x; sy=p.y; ex=sx; ey=sy; dragging=true; draw();
}
function moveDrag(e){
  if(!dragging) return;
  e.preventDefault();
  const p=toCanvasXY(e);
  ex=p.x; ey=p.y; draw();
}
function endDrag(e){
  if(!dragging) return;
  e.preventDefault();
  dragging=false;
  const x=Math.max(0,Math.min(sx,ex))/cnv.width;
  const y=Math.max(0,Math.min(sy,ey))/cnv.height;
  const w=Math.abs(ex-sx)/cnv.width;
  const h=Math.abs(ey-sy)/cnv.height;
  if(w>0.01 && h>0.01){ roi.x=x; roi.y=y; roi.w=w; roi.h=h; }
  draw();
}

cnv.addEventListener('mousedown', startDrag);
cnv.addEventListener('mousemove', moveDrag);
window.addEventListener('mouseup', endDrag);

cnv.addEventListener('touchstart', (ev)=>{ if(ev.touches && ev.touches[0]) startDrag(ev.touches[0]); }, {passive:false});
cnv.addEventListener('touchmove',  (ev)=>{ if(ev.touches && ev.touches[0]) moveDrag(ev.touches[0]);  }, {passive:false});
window.addEventListener('touchend', (ev)=>{ endDrag(ev.changedTouches && ev.changedTouches[0] ? ev.changedTouches[0] : ev); }, {passive:false});

async function loadCur(){
  const r=await fetch(`/api/roi_get?side=${side}&lane=${lane}&cam=${cam}`);
  const j=await r.json();
  roi=j.roi||roi;
  document.getElementById('enabled').checked=!!roi.enabled;
  document.getElementById('cur').textContent=JSON.stringify(roi, null, 2);
  draw();
}

document.getElementById('saveBtn').onclick=async ()=>{
  const enabled=document.getElementById('enabled').checked;
  const body={x:roi.x,y:roi.y,w:roi.w,h:roi.h,enabled};
  const r=await fetch(`/api/roi_save?side=${side}&lane=${lane}&cam=${cam}`,{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)
  });
  const j=await r.json();
  document.getElementById('msg').textContent=(j.error||j.message||'');
  await loadCur();
  refreshProcessed();
};

document.getElementById('clearBtn').onclick=async ()=>{
  const r=await fetch(`/api/roi_clear?side=${side}&lane=${lane}&cam=${cam}`,{method:'POST'});
  const j=await r.json();
  document.getElementById('msg').textContent=(j.error||j.message||'');
  await loadCur();
  refreshProcessed();
};

function refreshSnapshot(){ img.src=`/snapshot.jpg?side=${side}&lane=${lane}&cam=${cam}&w=960&ts=${Date.now()}`; }
function refreshProcessed(){ proc.src=`/snapshot_alpr.jpg?side=${side}&lane=${lane}&cam=${cam}&w=960&ts=${Date.now()}`; }

img.onload=()=>{ draw(); };
img.onerror=()=>{ setTimeout(refreshSnapshot, 800); };

window.onload=async ()=>{
  await loadCur();
  refreshSnapshot();
  refreshProcessed();
  setInterval(refreshSnapshot, 4000);
  setInterval(refreshProcessed, 1200);
}
</script>
"""

    @app.route("/roi")
    def roi_page():
        side = (request.args.get("side") or "entry").strip().lower()
        try:
            lane = int(request.args.get("lane", "1"))
        except Exception:
            lane = 1
        try:
            cam = int(request.args.get("cam", "1"))
        except Exception:
            cam = 1

        if side not in ("entry", "exit"):
            side = "entry"
        if lane < 1 or lane > 3:
            lane = 1
        if cam < 1 or cam > 2:
            cam = 1

        return render_template_string(ROI_HTML, side=side, side_label=side_label(side), lane=lane, cam=cam)

    @app.route("/api/roi_get")
    def api_roi_get():
        side = (request.args.get("side") or "entry").strip().lower()
        try:
            lane = int(request.args.get("lane", "1"))
        except Exception:
            lane = 1
        try:
            cam = int(request.args.get("cam", "1"))
        except Exception:
            cam = 1

        if side not in ("entry", "exit"):
            return _json_nocache({"error":"side inválido"}, 400)
        if lane < 1 or lane > 3 or cam < 1 or cam > 2:
            return _json_nocache({"error":"lane/cam inválido"}, 400)

        roi = rt.get_camera_cfg(side, lane, cam)["runtime"].get("roi", {"enabled":False,"x":0.0,"y":0.0,"w":1.0,"h":1.0})
        return _json_nocache({"side":side,"lane":lane,"cam":cam,"roi":roi})

    @app.route("/api/roi_save", methods=["POST"])
    def api_roi_save():
        if not _check_token():
            return _json_nocache({"error":"unauthorized"}, 401)

        side = (request.args.get("side") or "entry").strip().lower()
        try:
            lane = int(request.args.get("lane", "1"))
        except Exception:
            lane = 1
        try:
            cam = int(request.args.get("cam", "1"))
        except Exception:
            cam = 1

        if side not in ("entry", "exit"):
            return _json_nocache({"error":"side inválido"}, 400)
        if lane < 1 or lane > 3 or cam < 1 or cam > 2:
            return _json_nocache({"error":"lane/cam inválido"}, 400)

        body = request.get_json(force=True, silent=True) or {}
        x = max(0.0, min(1.0, float(body.get("x", 0.0))))
        y = max(0.0, min(1.0, float(body.get("y", 0.0))))
        w = max(0.0, min(1.0, float(body.get("w", 1.0))))
        h = max(0.0, min(1.0, float(body.get("h", 1.0))))
        en = bool(body.get("enabled", True))

        if x + w > 1.0:
            w = 1.0 - x
        if y + h > 1.0:
            h = 1.0 - y

        rt.get_camera_cfg(side, lane, cam)["runtime"]["roi"] = {
            "enabled": en,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
        }

        try:
            from portal_v8_app import save_runtime_cfg
            save_runtime_cfg(rt.cfg)
        except Exception:
            pass

        camera_key = rt.camera_key(side, lane, cam)
        rt.ensure_camera_runtime_objects(camera_key)
        rt.motion_states[camera_key].baseline = None

        return _json_nocache({
            "ok": True,
            "message": "ROI guardado",
            "roi": rt.get_camera_cfg(side, lane, cam)["runtime"]["roi"]
        })

    @app.route("/api/roi_clear", methods=["POST"])
    def api_roi_clear():
        if not _check_token():
            return _json_nocache({"error":"unauthorized"}, 401)

        side = (request.args.get("side") or "entry").strip().lower()
        try:
            lane = int(request.args.get("lane", "1"))
        except Exception:
            lane = 1
        try:
            cam = int(request.args.get("cam", "1"))
        except Exception:
            cam = 1

        if side not in ("entry", "exit"):
            return _json_nocache({"error":"side inválido"}, 400)
        if lane < 1 or lane > 3 or cam < 1 or cam > 2:
            return _json_nocache({"error":"lane/cam inválido"}, 400)

        rt.get_camera_cfg(side, lane, cam)["runtime"]["roi"] = {
            "enabled": False,
            "x": 0.0,
            "y": 0.0,
            "w": 1.0,
            "h": 1.0,
        }

        try:
            from portal_v8_app import save_runtime_cfg
            save_runtime_cfg(rt.cfg)
        except Exception:
            pass

        camera_key = rt.camera_key(side, lane, cam)
        rt.ensure_camera_runtime_objects(camera_key)
        rt.motion_states[camera_key].baseline = None

        return _json_nocache({"ok": True, "message": "ROI limpiado y deshabilitado"})


    @app.route("/healthz")
    def healthz():
        oks = []
        for side in ("entry", "exit"):
            side_cfg = rt.cfg[side]
            for lane_no, lane in enumerate(side_cfg.get("lanes", []), start=1):
                if not lane.get("enabled", False):
                    continue
                for cam_no, cam in enumerate(lane.get("cameras", []), start=1):
                    if not cam.get("enabled", False):
                        continue
                    ck = rt.camera_key(side, lane_no, cam_no)
                    rt.ensure_camera_runtime_objects(ck)
                    oks.append(rt.video_sources[ck].get() is not None)
        any_ok = any(oks) if oks else False
        return (f"V8:{'OK' if any_ok else 'NO'} CAMS:{len(oks)}", (200 if any_ok else 503))

    return app
