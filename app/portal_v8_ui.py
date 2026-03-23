from __future__ import annotations

import copy
import datetime

from flask import redirect, render_template_string, request, url_for

from app.portal_v8_models import (
    clampf,
    clampi,
    norm_cols_any,
    normalize_runtime,
    normalize_side_bases,
    normalize_tag_section,
    normalize_wl_section,
    parse_bool,
    col_to_idx,
)
from app.portal_v8_runtime import (
    RuntimeContext,
    download_side_tag_wl,
    download_side_wl,
    gate_fire,
    materialize_camera_url,
    ping_ip,
    side_label,
)

TZ_NAME = "America/Mexico_City"


def register_ui_routes(app, rt: RuntimeContext, save_cfg_fn):
    def _fmt_ts(ts):
        try:
            from zoneinfo import ZoneInfo
            ts = float(ts or 0.0)
            if ts <= 0:
                return "—"
            return datetime.datetime.fromtimestamp(ts, tz=ZoneInfo(TZ_NAME)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            try:
                ts = float(ts or 0.0)
                if ts <= 0:
                    return "—"
                return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                return "—"

    def _pair_from_form(prefix: str):
        return {
            "url1": (request.form.get(prefix + "_url1") or "").strip(),
            "send_snapshot1": parse_bool(request.form.get(prefix + "_send_snapshot1"), False),
            "snapshot_mode1": (request.form.get(prefix + "_snapshot_mode1") or "multipart").strip(),
            "url2": (request.form.get(prefix + "_url2") or "").strip(),
            "send_snapshot2": parse_bool(request.form.get(prefix + "_send_snapshot2"), False),
            "snapshot_mode2": (request.form.get(prefix + "_snapshot_mode2") or "multipart").strip(),
        }

    def _pair_block(prefix, pair):
        u1 = pair.get("url1", "")
        u2 = pair.get("url2", "")
        s1 = bool(pair.get("send_snapshot1", False))
        s2 = bool(pair.get("send_snapshot2", False))
        m1 = pair.get("snapshot_mode1", "multipart") or "multipart"
        m2 = pair.get("snapshot_mode2", "multipart") or "multipart"
        return f"""
        <div class="grid3">
          <label>URL #1<br><input type="text" name="{prefix}_url1" value="{u1}" placeholder="https://..."></label>
          <label>Snapshot #1<br>
            <select name="{prefix}_send_snapshot1">
              <option value="0" {"selected" if not s1 else ""}>OFF</option>
              <option value="1" {"selected" if s1 else ""}>ON</option>
            </select>
          </label>
          <label>Modo #1<br>
            <select name="{prefix}_snapshot_mode1">
              <option value="multipart" {"selected" if m1=="multipart" else ""}>multipart</option>
              <option value="json" {"selected" if m1=="json" else ""}>json</option>
            </select>
          </label>
        </div>
        <div class="grid3">
          <label>URL #2<br><input type="text" name="{prefix}_url2" value="{u2}" placeholder="https://..."></label>
          <label>Snapshot #2<br>
            <select name="{prefix}_send_snapshot2">
              <option value="0" {"selected" if not s2 else ""}>OFF</option>
              <option value="1" {"selected" if s2 else ""}>ON</option>
            </select>
          </label>
          <label>Modo #2<br>
            <select name="{prefix}_snapshot_mode2">
              <option value="multipart" {"selected" if m2=="multipart" else ""}>multipart</option>
              <option value="json" {"selected" if m2=="json" else ""}>json</option>
            </select>
          </label>
        </div>
        """

    def _section_from_form(prefix: str, sec: dict):
        sec["sheets_input"] = (request.form.get(prefix + "_sheets_input") or "").strip()
        sec["auto_refresh_min"] = clampi(
            request.form.get(prefix + "_auto_refresh_min", sec.get("auto_refresh_min", 0)),
            0, 1440, sec.get("auto_refresh_min", 0)
        )
        sec["search_start_col"] = col_to_idx(
            request.form.get(prefix + "_search_start_col", sec.get("search_start_col", 14)),
            sec.get("search_start_col", 14)
        )
        sec["search_end_col"] = col_to_idx(
            request.form.get(prefix + "_search_end_col", sec.get("search_end_col", 18)),
            sec.get("search_end_col", 18)
        )
        if sec["search_end_col"] < sec["search_start_col"]:
            sec["search_end_col"] = sec["search_start_col"]
        sec["status_col"] = col_to_idx(
            request.form.get(prefix + "_status_col", sec.get("status_col", 3)),
            sec.get("status_col", 3)
        )

        c1 = request.form.get(prefix + "_disp_col_1", sec.get("disp_cols", [2, 3, 4])[0])
        c2 = request.form.get(prefix + "_disp_col_2", sec.get("disp_cols", [2, 3, 4])[1])
        c3 = request.form.get(prefix + "_disp_col_3", sec.get("disp_cols", [2, 3, 4])[2])
        sec["disp_cols"] = norm_cols_any([c1, c2, c3], 3)

        t1 = (request.form.get(prefix + "_disp_title_1") or sec.get("disp_titles", ["", "", ""])[0] or "Campo 1")
        t2 = (request.form.get(prefix + "_disp_title_2") or sec.get("disp_titles", ["", "", ""])[1] or "Campo 2")
        t3 = (request.form.get(prefix + "_disp_title_3") or sec.get("disp_titles", ["", "", ""])[2] or "Campo 3")
        sec["disp_titles"] = [t1, t2, t3]

    SETTINGS_MAIN_HTML = """
    <style>
     body{font-family:system-ui;margin:18px;background:#fafafa}
     .card{border:1px solid #ddd;border-radius:12px;padding:14px;background:#fff;margin-bottom:12px}
     .btn{padding:8px 12px;border:1px solid #888;border-radius:10px;background:#f5f5f5;cursor:pointer;text-decoration:none;color:#111}
     input[type="text"],input[type="number"]{padding:6px 8px;border-radius:8px;border:1px solid #bbb;min-width:220px}
     .muted{color:#666;font-size:12px}
    </style>

    <h2>Configuración del acceso</h2>

    <div class="card">
      <form method="post">
        <label><b>Nombre del acceso</b><br><input type="text" name="site_name" value="{{site_name}}"></label>

        <h3 style="margin-top:18px">Heartbeat / Monitor</h3>
        <label><input type="checkbox" name="heartbeat_enabled" {{'checked' if heartbeat_enabled else ''}}> Enviar heartbeat</label><br><br>
        <label>Monitor URL<br><input type="text" name="heartbeat_url" value="{{heartbeat_url}}" placeholder="https://..."></label><br><br>
        <label>Periodo (min)<br><input type="number" step="1" name="heartbeat_period_min" value="{{heartbeat_period_min}}"></label>

        <p style="margin-top:10px">
          <button class="btn" name="action" value="heartbeat_test">📡 Probar heartbeat ahora</button>
          <span class="muted">{{hb_msg}}</span>
        </p>
        <p class="muted">
          Último OK: <b>{{hb_last_ok}}</b> • Último intento: <b>{{hb_last_try}}</b> • Code: <b>{{hb_last_code}}</b> • Error: <b>{{hb_last_err}}</b>
        </p>

        <h3 style="margin-top:18px">Configuración por lado</h3>
        <p>
          <a class="btn" href="/settings/side/entry">Entrada</a>
          <a class="btn" href="/settings/side/exit">Salida</a>
          <a class="btn" href="/">Regresar</a>
        </p>

        <p style="margin-top:14px">
          <button class="btn" type="submit" name="action" value="save">Guardar</button>
        </p>
        <p class="muted">{{msg}}</p>
      </form>
    </div>
    """

    SETTINGS_SIDE_HTML = """
    <style>
     body{font-family:system-ui;margin:18px;background:#fafafa}
     .card{border:1px solid #ddd;border-radius:12px;padding:14px;background:#fff;margin-bottom:12px}
     .btn{padding:8px 12px;border:1px solid #888;border-radius:10px;background:#f5f5f5;cursor:pointer;text-decoration:none;color:#111}
     input[type="text"],input[type="number"],select{padding:6px 8px;border-radius:8px;border:1px solid #bbb;min-width:220px}
     .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
     .grid3{display:grid;grid-template-columns:repeat(3,minmax(220px,1fr));gap:10px 16px}
     .grid4{display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:10px 16px}
     .lane{border:1px dashed #bbb;border-radius:10px;padding:10px;margin-top:8px;background:#fcfcfc}
     .cam{border:1px solid #e8e8e8;border-radius:10px;padding:10px;margin-top:8px;background:#fff}
     .subsec{border:1px dashed #aaa;padding:10px;border-radius:10px;margin:8px 0;background:#fcfcfc}
     .tabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
     .muted{color:#666;font-size:12px}
     .ok{color:#2e7d32;font-weight:700}
     .bad{color:#c62828;font-weight:700}
    </style>

    <h2>{{side_label}} — {{side.name}}</h2>

    <div class="tabs">
      <a class="btn" href="/settings">← Configuración del acceso</a>
      <a class="btn" href="/settings/side/{{side_key}}?tab=cameras">Carriles y cámaras</a>
      <a class="btn" href="/settings/side/{{side_key}}?tab=bases">Bases</a>
      <a class="btn" href="/">Monitor</a>
    </div>

    {% if tab == 'cameras' %}
    <div class="card">
      <form method="post">
        <input type="hidden" name="tab" value="cameras">
        <label><input type="checkbox" name="{{side_key}}_enabled" {{'checked' if side.enabled else ''}}> Habilitar {{side_label}}</label><br><br>
        <label>Nombre visible<br><input type="text" name="{{side_key}}_name" value="{{side.name}}"></label>

        {% for lane in lanes %}
        <div class="lane">
          <label><input type="checkbox" name="lane_{{lane.index}}_enabled" {{'checked' if lane.enabled else ''}}> Habilitar {{lane.name}}</label><br><br>
          <label>Nombre del carril<br><input type="text" name="lane_{{lane.index}}_name" value="{{lane.name}}"></label>

          {% for cam in lane.cameras %}
          <div class="cam">
            <label><input type="checkbox" name="lane_{{lane.index}}_cam_{{cam.index}}_enabled" {{'checked' if cam.enabled else ''}}> Habilitar {{cam.name}}</label><br><br>
            <label>Nombre de cámara<br><input type="text" name="lane_{{lane.index}}_cam_{{cam.index}}_name" value="{{cam.name}}"></label><br><br>
            <label>Etiqueta libre (ej. Frontal / Trasera)<br><input type="text" name="lane_{{lane.index}}_cam_{{cam.index}}_role_label" value="{{cam.role_label}}"></label>

            <div class="subsec">
              <b>Estado / conectividad</b>
              <div class="muted" style="margin-top:6px">
                URL materializada: <b>{{cam.materialized_url}}</b><br>
                IP: <b>{{cam.ip}}</b> • Estado:
                <span class="{{'ok' if cam.conn_ok else 'bad'}}">{{'Conectada' if cam.conn_ok else 'Sin conexión'}}</span>
              </div>
              <p style="margin-top:8px">
                <a class="btn" href="/settings/camera/{{side_key}}/{{lane.index}}/{{cam.index}}">Configurar esta cámara</a>
                <a class="btn" href="/snapshot.jpg?side={{side_key}}&lane={{lane.index}}&cam={{cam.index}}&w=640" target="_blank">Snapshot</a>
                <a class="btn" href="/snapshot_alpr.jpg?side={{side_key}}&lane={{lane.index}}&cam={{cam.index}}&w=640" target="_blank">ALPR</a>
                <a class="btn" href="/api/alpr_debug?side={{side_key}}&lane={{lane.index}}&cam={{cam.index}}" target="_blank">Debug</a>
              </p>
            </div>
          </div>
          {% endfor %}
        </div>
        {% endfor %}

        <p style="margin-top:14px">
          <button class="btn" type="submit" name="action" value="save">Guardar carriles y cámaras</button>
        </p>
        <p class="muted">{{msg}}</p>
      </form>
    </div>
    {% else %}
    <div class="card">
      <form method="post">
        <input type="hidden" name="tab" value="bases">

        <label><input type="checkbox" name="{{side_key}}_enabled" {{'checked' if side.enabled else ''}}> Habilitar {{side_label}}</label><br><br>
        <label>Nombre visible<br><input type="text" name="{{side_key}}_name" value="{{side.name}}"></label>

        <h3 style="margin-top:16px">Owners</h3>
        <div class="grid2">
          <label>Sheets (ID o URL)<br><input type="text" name="owners_sheets_input" value="{{owners.sheets_input}}"></label>
          <label>Auto refresh (min)<br><input type="number" name="owners_auto_refresh_min" value="{{owners.auto_refresh_min}}"></label>
          <label>Buscar desde col<br><input type="text" name="owners_search_start_col" value="{{owners.search_start_col}}"></label>
          <label>Hasta col<br><input type="text" name="owners_search_end_col" value="{{owners.search_end_col}}"></label>
          <label>Status col<br><input type="text" name="owners_status_col" value="{{owners.status_col}}"></label>
          <label>Disp col 1<br><input type="text" name="owners_disp_col_1" value="{{owners.disp_cols[0]}}"></label>
          <label>Disp col 2<br><input type="text" name="owners_disp_col_2" value="{{owners.disp_cols[1]}}"></label>
          <label>Disp col 3<br><input type="text" name="owners_disp_col_3" value="{{owners.disp_cols[2]}}"></label>
          <label>Título 1<br><input type="text" name="owners_disp_title_1" value="{{owners.disp_titles[0]}}"></label>
          <label>Título 2<br><input type="text" name="owners_disp_title_2" value="{{owners.disp_titles[1]}}"></label>
          <label>Título 3<br><input type="text" name="owners_disp_title_3" value="{{owners.disp_titles[2]}}"></label>
        </div>
        <div class="subsec"><b>Owners — ACTIVO</b>{{owners_wh_active|safe}}</div>
        <div class="subsec"><b>Owners — INACTIVO</b>{{owners_wh_inactive|safe}}</div>

        <h3 style="margin-top:16px">Visitors</h3>
        <div class="grid2">
          <label>Sheets (ID o URL)<br><input type="text" name="visitors_sheets_input" value="{{visitors.sheets_input}}"></label>
          <label>Auto refresh (min)<br><input type="number" name="visitors_auto_refresh_min" value="{{visitors.auto_refresh_min}}"></label>
          <label>Buscar desde col<br><input type="text" name="visitors_search_start_col" value="{{visitors.search_start_col}}"></label>
          <label>Hasta col<br><input type="text" name="visitors_search_end_col" value="{{visitors.search_end_col}}"></label>
          <label>Status col<br><input type="text" name="visitors_status_col" value="{{visitors.status_col}}"></label>
          <label>Disp col 1<br><input type="text" name="visitors_disp_col_1" value="{{visitors.disp_cols[0]}}"></label>
          <label>Disp col 2<br><input type="text" name="visitors_disp_col_2" value="{{visitors.disp_cols[1]}}"></label>
          <label>Disp col 3<br><input type="text" name="visitors_disp_col_3" value="{{visitors.disp_cols[2]}}"></label>
          <label>Título 1<br><input type="text" name="visitors_disp_title_1" value="{{visitors.disp_titles[0]}}"></label>
          <label>Título 2<br><input type="text" name="visitors_disp_title_2" value="{{visitors.disp_titles[1]}}"></label>
          <label>Título 3<br><input type="text" name="visitors_disp_title_3" value="{{visitors.disp_titles[2]}}"></label>
        </div>
        <div class="subsec"><b>Visitors — ACTIVO</b>{{visitors_wh_active|safe}}</div>
        <div class="subsec"><b>Visitors — INACTIVO</b>{{visitors_wh_inactive|safe}}</div>

        <h3 style="margin-top:16px">NoFound placas</h3>
        <div class="subsec">{{plates_notfound|safe}}</div>

        <h3 style="margin-top:16px">Tags</h3>
        <label>Formato lookup<br>
          <select name="tags_lookup_format">
            <option value="physical" {{'selected' if tags_lookup_format=='physical' else ''}}>physical</option>
            <option value="internal_hex" {{'selected' if tags_lookup_format=='internal_hex' else ''}}>internal_hex</option>
          </select>
        </label>

        <div class="grid2" style="margin-top:8px">
          <label>Sheets (ID o URL)<br><input type="text" name="tags_owners_sheets_input" value="{{tags_owners.sheets_input}}"></label>
          <label>Auto refresh (min)<br><input type="number" name="tags_owners_auto_refresh_min" value="{{tags_owners.auto_refresh_min}}"></label>
          <label>Buscar desde col<br><input type="text" name="tags_owners_search_start_col" value="{{tags_owners.search_start_col}}"></label>
          <label>Hasta col<br><input type="text" name="tags_owners_search_end_col" value="{{tags_owners.search_end_col}}"></label>
          <label>Status col<br><input type="text" name="tags_owners_status_col" value="{{tags_owners.status_col}}"></label>
          <label>Disp col 1<br><input type="text" name="tags_owners_disp_col_1" value="{{tags_owners.disp_cols[0]}}"></label>
          <label>Disp col 2<br><input type="text" name="tags_owners_disp_col_2" value="{{tags_owners.disp_cols[1]}}"></label>
          <label>Disp col 3<br><input type="text" name="tags_owners_disp_col_3" value="{{tags_owners.disp_cols[2]}}"></label>
          <label>Título 1<br><input type="text" name="tags_owners_disp_title_1" value="{{tags_owners.disp_titles[0]}}"></label>
          <label>Título 2<br><input type="text" name="tags_owners_disp_title_2" value="{{tags_owners.disp_titles[1]}}"></label>
          <label>Título 3<br><input type="text" name="tags_owners_disp_title_3" value="{{tags_owners.disp_titles[2]}}"></label>
        </div>

        <div class="subsec"><b>Tags Owners — ACTIVO</b>{{tags_owners_wh_active|safe}}</div>
        <div class="subsec"><b>Tags Owners — INACTIVO</b>{{tags_owners_wh_inactive|safe}}</div>

        <h3 style="margin-top:16px">NoFound tags</h3>
        <div class="subsec">{{tags_notfound|safe}}</div>

        <p style="margin-top:10px">
          <button class="btn" name="action" value="save_bases">Guardar bases</button>
          <button class="btn" name="action" value="refresh_owners">Refresh Owners</button>
          <button class="btn" name="action" value="refresh_visitors">Refresh Visitors</button>
          <button class="btn" name="action" value="refresh_tags">Refresh Tags</button>
        </p>

        <p class="muted">{{msg}}</p>
      </form>
    </div>
    {% endif %}
    """

    SETTINGS_CAMERA_HTML = """
    <style>
     body{font-family:system-ui;margin:18px;background:#fafafa}
     .card{border:1px solid #ddd;border-radius:12px;padding:14px;background:#fff;margin-bottom:12px}
     .btn{padding:8px 12px;border:1px solid #888;border-radius:10px;background:#f5f5f5;cursor:pointer;text-decoration:none;color:#111}
     input[type="text"],input[type="number"],select{padding:6px 8px;border-radius:8px;border:1px solid #bbb;min-width:220px}
     .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
     .grid4{display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:10px 16px}
     .muted{color:#666;font-size:12px}
     .subsec{border:1px dashed #aaa;padding:10px;border-radius:10px;margin:10px 0;background:#fcfcfc}
    </style>

    <h2>{{side_label}} / {{lane_name}} / {{cam_name}}</h2>

    <div class="card">
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <a class="btn" href="/settings/side/{{side_key}}?tab=cameras">← Regresar a {{side_label}}</a>
        <a class="btn" href="/">Monitor</a>
        <a class="btn" href="/snapshot.jpg?side={{side_key}}&lane={{lane_no}}&cam={{cam_no}}&w=640" target="_blank">Snapshot</a>
        <a class="btn" href="/snapshot_alpr.jpg?side={{side_key}}&lane={{lane_no}}&cam={{cam_no}}&w=640" target="_blank">ALPR</a>
        <a class="btn" href="/api/alpr_debug?side={{side_key}}&lane={{lane_no}}&cam={{cam_no}}" target="_blank">Debug</a>
      </div>
      <p class="muted" style="margin-top:10px">
        URL materializada: <b>{{materialized_url}}</b><br>
        IP resuelta: <b>{{resolved_ip}}</b> • Origen: <b>{{url_source}}</b>
      </p>
    </div>

    <div class="card">
      <form method="post">
        <h3>Cámara</h3>
        <div class="grid2">
          <label>Nombre visible<br><input type="text" name="cam_name" value="{{cam_name}}"></label>
          <label>Etiqueta libre<br><input type="text" name="role_label" value="{{role_label}}"></label>
          <label>Modo<br>
            <select name="camera_mode">
              <option value="mac" {{'selected' if c.camera_mode=='mac' else ''}}>Por MAC ({CAM_IP})</option>
              <option value="manual" {{'selected' if c.camera_mode=='manual' else ''}}>URL manual</option>
            </select>
          </label>
          <label>camera_mac<br><input type="text" name="camera_mac" value="{{c.camera_mac}}"></label>
          <label style="grid-column:1/-1">RTSP URL<br><input type="text" name="camera_url" value="{{c.camera_url}}" style="min-width:95%"></label>
        </div>

        <h3>Procesamiento ALPR</h3>
        <div class="grid4">
          <label>Procesar cada N frames<br><input type="number" name="process_every_n" value="{{c.process_every_n}}"></label>
          <label>Max ancho (px)<br><input type="number" name="resize_max_w" value="{{c.resize_max_w}}"></label>
          <label>Top-K<br><input type="number" name="alpr_topk" value="{{c.alpr_topk}}"></label>
          <label>Umbral conf (%)<br><input type="number" name="min_conf_pct" value="{{(c.min_confidence*100)|round(0,'floor')}}"></label>
          <label>Det conf (%)<br><input type="number" name="det_min_conf_pct" value="{{(c.det_min_confidence*100)|round(0,'floor')}}"></label>
          <label>Stable hits<br><input type="number" name="stable_hits_required" value="{{c.stable_hits_required}}"></label>
          <label>NoFound hits<br><input type="number" name="notfound_stable_hits_required" value="{{c.notfound_stable_hits_required}}"></label>
          <label>Suprimir NoFound (s)<br><input type="number" name="suppress_notfound_after_auth_sec" value="{{c.suppress_notfound_after_auth_sec}}"></label>
          <label>Latch hold (s)<br><input type="number" step="0.1" name="latch_hold_sec" value="{{c.latch_hold_sec}}"></label>
          <label>Idle clear (s)<br><input type="number" step="0.1" name="idle_clear_sec" value="{{c.idle_clear_sec}}"></label>
        </div>

        <div class="subsec">
          <b>Pre-procesado</b>
          <div class="grid4">
            <label><input type="checkbox" name="pp_enabled" {{'checked' if c.pp_enabled else ''}}> Habilitar</label>
            <label>Perfil<br>
              <select name="pp_profile">
                <option value="none" {{'selected' if c.pp_profile=='none' else ''}}>none</option>
                <option value="bw_hicontrast_sharp" {{'selected' if c.pp_profile=='bw_hicontrast_sharp' else ''}}>B/N alto contraste + nitidez</option>
              </select>
            </label>
            <label>CLAHE clip<br><input type="number" step="0.1" name="pp_clahe_clip" value="{{c.pp_clahe_clip}}"></label>
            <label>Nitidez<br><input type="number" step="0.05" name="pp_sharp_strength" value="{{c.pp_sharp_strength}}"></label>
          </div>
        </div>

        <div class="subsec">
          <b>Motion gating</b>
          <div class="grid4">
            <label><input type="checkbox" name="motion_enabled" {{'checked' if c.motion.enabled else ''}}> Habilitar</label>
            <label>Umbral cambio pix (%)<br><input type="number" step="0.1" name="motion_pixel_change_pct" value="{{c.motion.pixel_change_pct}}"></label>
            <label>Δ intensidad<br><input type="number" name="motion_intensity_delta" value="{{c.motion.intensity_delta}}"></label>
            <label>Recalibrar (min)<br><input type="number" name="motion_autobase_every_min" value="{{c.motion.autobase_every_min}}"></label>
            <label>Muestras baseline<br><input type="number" name="motion_autobase_samples" value="{{c.motion.autobase_samples}}"></label>
            <label>Intervalo muestras (s)<br><input type="number" step="0.1" name="motion_autobase_interval_s" value="{{c.motion.autobase_interval_s}}"></label>
            <label>Cooldown (s)<br><input type="number" step="0.1" name="motion_cooldown_s" value="{{c.motion.cooldown_s}}"></label>
          </div>
        </div>

        <div class="subsec">
          <b>ROI</b>
          <div class="grid4">
            <label><input type="checkbox" name="roi_enabled" {{'checked' if c.roi.enabled else ''}}> Habilitar ROI</label>
            <label>X<br><input type="number" step="0.001" name="roi_x" value="{{c.roi.x}}"></label>
            <label>Y<br><input type="number" step="0.001" name="roi_y" value="{{c.roi.y}}"></label>
            <label>W<br><input type="number" step="0.001" name="roi_w" value="{{c.roi.w}}"></label>
            <label>H<br><input type="number" step="0.001" name="roi_h" value="{{c.roi.h}}"></label>
          </div>
        </div>

        <div class="subsec">
          <b>Gate</b>
          <div class="grid4">
            <label><input type="checkbox" name="gate_enabled" {{'checked' if c.gate_enabled else ''}}> Habilitar Gate</label>
            <label><input type="checkbox" name="gate_auto_on_auth" {{'checked' if c.gate_auto_on_auth else ''}}> Auto abrir si ACTIVO</label>
            <label>Modo<br>
              <select name="gate_mode">
                <option value="serial" {{'selected' if c.gate_mode=='serial' else ''}}>SERIAL/USB</option>
                <option value="http" {{'selected' if c.gate_mode=='http' else ''}}>HTTP/IP</option>
              </select>
            </label>
            <label>Anti-spam (s)<br><input type="number" name="gate_antispam_sec" value="{{c.gate_antispam_sec}}"></label>
            <label>Pulso (ms)<br><input type="number" name="gate_pulse_ms" value="{{c.gate_pulse_ms}}"></label>
            <label>Gate URL<br><input type="text" name="gate_url" value="{{c.gate_url}}"></label>
            <label>Token<br><input type="text" name="gate_token" value="{{c.gate_token}}"></label>
            <label>GPIO pin<br><input type="number" name="gate_pin" value="{{c.gate_pin}}"></label>
            <label>Active low<br>
              <select name="gate_active_low">
                <option value="0" {{'selected' if not c.gate_active_low else ''}}>NO</option>
                <option value="1" {{'selected' if c.gate_active_low else ''}}>SI</option>
              </select>
            </label>
            <label>Serial device<br><input type="text" name="gate_serial_device" value="{{c.gate_serial_device}}"></label>
            <label>Serial baud<br><input type="number" name="gate_serial_baud" value="{{c.gate_serial_baud}}"></label>
            <label>Serial gate #<br><input type="number" name="gate_serial_gate" value="{{c.gate_serial_gate}}"></label>
          </div>
        </div>

        <div class="subsec">
          <b>Dedup / Gap</b>
          <div class="grid4">
            <label><input type="checkbox" name="wh_repeat_same_plate" {{'checked' if c.wh_repeat_same_plate else ''}}> Permitir repetir misma placa/tag</label>
            <label>Min gap (s)<br><input type="number" name="wh_min_gap_sec" value="{{c.wh_min_gap_sec}}"></label>
          </div>
        </div>

        <p style="margin-top:14px">
          <button class="btn" type="submit" name="action" value="save">Guardar cámara</button>
          <button class="btn" type="submit" name="action" value="test_gate">Probar gate</button>
          <a class="btn" href="/settings/side/{{side_key}}?tab=cameras">Regresar</a>
        </p>
        <p class="muted">{{msg}}</p>
      </form>
    </div>
    """

    @app.route("/settings", methods=["GET", "POST"])
    def settings_main():
        msg = ""
        hb_msg = ""

        if request.method == "POST":
            action = (request.form.get("action") or "save").strip()

            rt.cfg["site_name"] = (request.form.get("site_name") or "Acceso Principal").strip() or "Acceso Principal"
            rt.cfg["heartbeat"]["enabled"] = parse_bool(request.form.get("heartbeat_enabled"), False)
            rt.cfg["heartbeat"]["url"] = (request.form.get("heartbeat_url") or "").strip()
            rt.cfg["heartbeat"]["period_min"] = clampi(
                request.form.get("heartbeat_period_min", rt.cfg["heartbeat"].get("period_min", 0)),
                0, 1440, rt.cfg["heartbeat"].get("period_min", 0)
            )
            save_cfg_fn(rt.cfg)

            if action == "heartbeat_test":
                try:
                    if hasattr(rt, "heartbeat_mgr") and rt.heartbeat_mgr:
                        rt.heartbeat_mgr.enqueue("manual_test")
                        hb_msg = "Encolado (manual_test)."
                    else:
                        hb_msg = "Heartbeat manager aún no disponible."
                except Exception as e:
                    hb_msg = f"Error encolando: {e}"
            else:
                msg = "Guardado."

        return render_template_string(
            SETTINGS_MAIN_HTML,
            site_name=rt.cfg.get("site_name", "Acceso Principal"),
            heartbeat_enabled=rt.cfg["heartbeat"].get("enabled", False),
            heartbeat_url=rt.cfg["heartbeat"].get("url", ""),
            heartbeat_period_min=rt.cfg["heartbeat"].get("period_min", 0),
            hb_msg=hb_msg,
            hb_last_ok=_fmt_ts(rt.hb_status.get("last_ok_ts", 0.0)),
            hb_last_try=_fmt_ts(rt.hb_status.get("last_try_ts", 0.0)),
            hb_last_code=(rt.hb_status.get("last_code", None) if rt.hb_status.get("last_code", None) is not None else "—"),
            hb_last_err=(rt.hb_status.get("last_err", "") or "—"),
            msg=msg,
        )

    @app.route("/settings/side/<side_key>", methods=["GET", "POST"])
    def settings_side(side_key: str):
        side_key = (side_key or "").strip().lower()
        if side_key not in ("entry", "exit"):
            return redirect(url_for("settings_main"))

        side = rt.cfg[side_key]
        side_lbl = side_label(side_key)
        tab = (request.args.get("tab") or request.form.get("tab") or "cameras").strip().lower()
        if tab not in ("cameras", "bases"):
            tab = "cameras"

        msg = ""

        if request.method == "POST":
            action = (request.form.get("action") or "save").strip()
            side["enabled"] = parse_bool(request.form.get(f"{side_key}_enabled"), side.get("enabled", True))
            side["name"] = (request.form.get(f"{side_key}_name") or side_lbl).strip() or side_lbl

            if tab == "cameras":
                for lane_idx, lane in enumerate(side.get("lanes", []), start=1):
                    lane["enabled"] = parse_bool(request.form.get(f"lane_{lane_idx}_enabled"), lane.get("enabled", False))
                    lane["name"] = (request.form.get(f"lane_{lane_idx}_name") or f"Carril {lane_idx}").strip() or f"Carril {lane_idx}"

                    for cam_idx, cam in enumerate(lane.get("cameras", []), start=1):
                        cam["enabled"] = parse_bool(request.form.get(f"lane_{lane_idx}_cam_{cam_idx}_enabled"), cam.get("enabled", False))
                        cam["name"] = (request.form.get(f"lane_{lane_idx}_cam_{cam_idx}_name") or f"Cámara {cam_idx}").strip() or f"Cámara {cam_idx}"
                        cam["role_label"] = (request.form.get(f"lane_{lane_idx}_cam_{cam_idx}_role_label") or "").strip()

                save_cfg_fn(rt.cfg)
                rt.refresh_runtime_registry()
                msg = f"{side_lbl}: carriles y cámaras guardados."

            elif tab == "bases":
                bases = side["bases"]

                _section_from_form("owners", bases["owners"])
                bases["owners"]["wh_active"] = _pair_from_form("owners_wh_active")
                bases["owners"]["wh_inactive"] = _pair_from_form("owners_wh_inactive")

                _section_from_form("visitors", bases["visitors"])
                bases["visitors"]["wh_active"] = _pair_from_form("visitors_wh_active")
                bases["visitors"]["wh_inactive"] = _pair_from_form("visitors_wh_inactive")

                bases["wh_notfound"] = _pair_from_form("plates_notfound")

                bases["tags"]["lookup_format"] = (request.form.get("tags_lookup_format", bases["tags"].get("lookup_format", "physical")) or "physical").strip()
                _section_from_form("tags_owners", bases["tags"]["owners"])
                bases["tags"]["owners"]["wh_active"] = _pair_from_form("tags_owners_wh_active")
                bases["tags"]["owners"]["wh_inactive"] = _pair_from_form("tags_owners_wh_inactive")
                bases["tags"]["wh_notfound"] = _pair_from_form("tags_notfound")

                bases["owners"] = normalize_wl_section(bases["owners"])
                bases["visitors"] = normalize_wl_section(bases["visitors"])
                bases["tags"] = normalize_tag_section(bases["tags"])
                bases["wh_notfound"] = normalize_side_bases({"wh_notfound": bases["wh_notfound"]})["wh_notfound"]

                save_cfg_fn(rt.cfg)

                if action == "refresh_owners":
                    msg = download_side_wl(rt, side_key, "owners")
                elif action == "refresh_visitors":
                    msg = download_side_wl(rt, side_key, "visitors")
                elif action == "refresh_tags":
                    msg = download_side_tag_wl(rt, side_key)
                else:
                    msg = f"{side_lbl}: bases guardadas."

        lanes = []
        for lane_idx, lane in enumerate(side.get("lanes", []), start=1):
            lane_view = copy.deepcopy(lane)
            lane_view["index"] = lane_idx
            cams = []
            for cam_idx, cam in enumerate(lane_view.get("cameras", []), start=1):
                cam["index"] = cam_idx
                if cam.get("enabled", False):
                    try:
                        materialized_url, ip, _source = materialize_camera_url(rt, cam)
                    except Exception:
                        materialized_url, ip, _source = "", None, ""
                    cam["materialized_url"] = materialized_url or "—"
                    cam["ip"] = ip or "—"
                    try:
                        cam["conn_ok"] = bool(ip) and bool(ping_ip(ip, 1))
                    except Exception:
                        cam["conn_ok"] = bool(ip)
                else:
                    cam["materialized_url"] = "—"
                    cam["ip"] = "—"
                    cam["conn_ok"] = False
                cams.append(cam)
            lane_view["cameras"] = cams
            lanes.append(lane_view)

        bases = side["bases"]
        owners = copy.deepcopy(bases["owners"])
        visitors = copy.deepcopy(bases["visitors"])
        tags_owners = copy.deepcopy(bases["tags"]["owners"])

        return render_template_string(
            SETTINGS_SIDE_HTML,
            side_key=side_key,
            side_label=side_lbl,
            side=side,
            lanes=lanes,
            tab=tab,
            msg=msg,
            owners=owners,
            visitors=visitors,
            owners_wh_active=_pair_block("owners_wh_active", owners.get("wh_active", {})),
            owners_wh_inactive=_pair_block("owners_wh_inactive", owners.get("wh_inactive", {})),
            visitors_wh_active=_pair_block("visitors_wh_active", visitors.get("wh_active", {})),
            visitors_wh_inactive=_pair_block("visitors_wh_inactive", visitors.get("wh_inactive", {})),
            plates_notfound=_pair_block("plates_notfound", bases.get("wh_notfound", {})),
            tags_lookup_format=bases["tags"].get("lookup_format", "physical"),
            tags_owners=tags_owners,
            tags_owners_wh_active=_pair_block("tags_owners_wh_active", tags_owners.get("wh_active", {})),
            tags_owners_wh_inactive=_pair_block("tags_owners_wh_inactive", tags_owners.get("wh_inactive", {})),
            tags_notfound=_pair_block("tags_notfound", bases["tags"].get("wh_notfound", {})),
        )

    @app.route("/settings/camera/<side_key>/<int:lane_no>/<int:cam_no>", methods=["GET", "POST"])
    def settings_camera(side_key: str, lane_no: int, cam_no: int):
        side_key = (side_key or "").strip().lower()
        if side_key not in ("entry", "exit"):
            return redirect(url_for("settings_main"))
        if lane_no < 1 or lane_no > 3 or cam_no < 1 or cam_no > 2:
            return redirect(url_for("settings_side", side_key=side_key))

        cam = rt.get_camera_cfg(side_key, lane_no, cam_no)
        runtime = cam["runtime"]
        side_lbl = side_label(side_key)
        lane = rt.get_lane_cfg(side_key, lane_no)
        msg = ""

        if request.method == "POST":
            action = (request.form.get("action") or "save").strip()

            cam["name"] = (request.form.get("cam_name") or cam.get("name", f"Cámara {cam_no}")).strip() or f"Cámara {cam_no}"
            cam["role_label"] = (request.form.get("role_label") or "").strip()

            runtime["camera_mode"] = (request.form.get("camera_mode") or runtime.get("camera_mode", "mac")).strip().lower()
            runtime["camera_mac"] = (request.form.get("camera_mac") or runtime.get("camera_mac", "")).upper().replace("-", ":")
            runtime["camera_url"] = (request.form.get("camera_url") or runtime.get("camera_url", "")).strip()

            runtime["process_every_n"] = clampi(request.form.get("process_every_n", runtime.get("process_every_n", 2)), 1, 30, runtime.get("process_every_n", 2))
            runtime["resize_max_w"] = clampi(request.form.get("resize_max_w", runtime.get("resize_max_w", 1280)), 64, 4096, runtime.get("resize_max_w", 1280))
            runtime["alpr_topk"] = clampi(request.form.get("alpr_topk", runtime.get("alpr_topk", 3)), 1, 5, runtime.get("alpr_topk", 3))
            runtime["min_confidence"] = clampf(float(request.form.get("min_conf_pct", runtime.get("min_confidence", 0.90) * 100.0)) / 100.0, 0.0, 1.0, runtime.get("min_confidence", 0.90))
            runtime["det_min_confidence"] = clampf(float(request.form.get("det_min_conf_pct", runtime.get("det_min_confidence", 0.80) * 100.0)) / 100.0, 0.0, 1.0, runtime.get("det_min_confidence", 0.80))
            runtime["stable_hits_required"] = clampi(request.form.get("stable_hits_required", runtime.get("stable_hits_required", 2)), 1, 5, runtime.get("stable_hits_required", 2))
            runtime["notfound_stable_hits_required"] = clampi(request.form.get("notfound_stable_hits_required", runtime.get("notfound_stable_hits_required", 4)), 1, 10, runtime.get("notfound_stable_hits_required", 4))
            runtime["suppress_notfound_after_auth_sec"] = clampi(request.form.get("suppress_notfound_after_auth_sec", runtime.get("suppress_notfound_after_auth_sec", 8)), 0, 60, runtime.get("suppress_notfound_after_auth_sec", 8))
            runtime["latch_hold_sec"] = max(1.0, float(request.form.get("latch_hold_sec", runtime.get("latch_hold_sec", 30.0))))
            runtime["idle_clear_sec"] = max(0.5, float(request.form.get("idle_clear_sec", runtime.get("idle_clear_sec", 1.5))))

            runtime["pp_enabled"] = parse_bool(request.form.get("pp_enabled"), False)
            runtime["pp_profile"] = (request.form.get("pp_profile") or runtime.get("pp_profile", "none")).strip().lower()
            runtime["pp_clahe_clip"] = clampf(request.form.get("pp_clahe_clip", runtime.get("pp_clahe_clip", 2.0)), 1.0, 4.0, runtime.get("pp_clahe_clip", 2.0))
            runtime["pp_sharp_strength"] = clampf(request.form.get("pp_sharp_strength", runtime.get("pp_sharp_strength", 0.55)), 0.0, 1.2, runtime.get("pp_sharp_strength", 0.55))

            runtime["motion"]["enabled"] = parse_bool(request.form.get("motion_enabled"), True)
            runtime["motion"]["pixel_change_pct"] = float(request.form.get("motion_pixel_change_pct", runtime["motion"].get("pixel_change_pct", 2.0)))
            runtime["motion"]["intensity_delta"] = clampi(request.form.get("motion_intensity_delta", runtime["motion"].get("intensity_delta", 25)), 1, 255, runtime["motion"].get("intensity_delta", 25))
            runtime["motion"]["autobase_every_min"] = clampi(request.form.get("motion_autobase_every_min", runtime["motion"].get("autobase_every_min", 10)), 1, 1440, runtime["motion"].get("autobase_every_min", 10))
            runtime["motion"]["autobase_samples"] = clampi(request.form.get("motion_autobase_samples", runtime["motion"].get("autobase_samples", 3)), 1, 5, runtime["motion"].get("autobase_samples", 3))
            runtime["motion"]["autobase_interval_s"] = max(0.2, float(request.form.get("motion_autobase_interval_s", runtime["motion"].get("autobase_interval_s", 1.0))))
            runtime["motion"]["cooldown_s"] = max(0.2, float(request.form.get("motion_cooldown_s", runtime["motion"].get("cooldown_s", 2.0))))

            runtime["roi"]["enabled"] = parse_bool(request.form.get("roi_enabled"), False)
            runtime["roi"]["x"] = clampf(request.form.get("roi_x", runtime["roi"].get("x", 0.0)), 0.0, 1.0, runtime["roi"].get("x", 0.0))
            runtime["roi"]["y"] = clampf(request.form.get("roi_y", runtime["roi"].get("y", 0.0)), 0.0, 1.0, runtime["roi"].get("y", 0.0))
            runtime["roi"]["w"] = clampf(request.form.get("roi_w", runtime["roi"].get("w", 1.0)), 0.0, 1.0, runtime["roi"].get("w", 1.0))
            runtime["roi"]["h"] = clampf(request.form.get("roi_h", runtime["roi"].get("h", 1.0)), 0.0, 1.0, runtime["roi"].get("h", 1.0))

            runtime["gate_enabled"] = parse_bool(request.form.get("gate_enabled"), False)
            runtime["gate_auto_on_auth"] = parse_bool(request.form.get("gate_auto_on_auth"), False)
            runtime["gate_mode"] = (request.form.get("gate_mode") or runtime.get("gate_mode", "serial")).strip().lower()
            runtime["gate_antispam_sec"] = clampi(request.form.get("gate_antispam_sec", runtime.get("gate_antispam_sec", 4)), 1, 600, runtime.get("gate_antispam_sec", 4))
            runtime["gate_pulse_ms"] = clampi(request.form.get("gate_pulse_ms", runtime.get("gate_pulse_ms", 500)), 20, 10000, runtime.get("gate_pulse_ms", 500))
            runtime["gate_url"] = (request.form.get("gate_url") or runtime.get("gate_url", "")).strip()
            runtime["gate_token"] = (request.form.get("gate_token") or runtime.get("gate_token", "")).strip()
            runtime["gate_pin"] = clampi(request.form.get("gate_pin", runtime.get("gate_pin", 5)), 1, 39, runtime.get("gate_pin", 5))
            runtime["gate_active_low"] = parse_bool(request.form.get("gate_active_low"), False)
            runtime["gate_serial_device"] = (request.form.get("gate_serial_device") or runtime.get("gate_serial_device", "")).strip()
            runtime["gate_serial_baud"] = clampi(request.form.get("gate_serial_baud", runtime.get("gate_serial_baud", 115200)), 1200, 921600, runtime.get("gate_serial_baud", 115200))
            runtime["gate_serial_gate"] = clampi(request.form.get("gate_serial_gate", runtime.get("gate_serial_gate", cam_no)), 1, 8, runtime.get("gate_serial_gate", cam_no))

            runtime["wh_repeat_same_plate"] = parse_bool(request.form.get("wh_repeat_same_plate"), False)
            runtime["wh_min_gap_sec"] = clampi(request.form.get("wh_min_gap_sec", runtime.get("wh_min_gap_sec", 0)), 0, 3600, runtime.get("wh_min_gap_sec", 0))

            cam["runtime"] = normalize_runtime(runtime, cam_no)
            save_cfg_fn(rt.cfg)
            rt.refresh_runtime_registry()

            if action == "test_gate":
                ok, gate_msg = gate_fire(rt, rt.camera_key(side_key, lane_no, cam_no))
                msg = "Gate OK" if ok else f"Gate error: {gate_msg}"
            else:
                msg = "Configuración de cámara guardada."

        materialized_url, resolved_ip, url_source = materialize_camera_url(rt, cam)

        return render_template_string(
            SETTINGS_CAMERA_HTML,
            side_key=side_key,
            side_label=side_lbl,
            lane_name=lane.get("name", f"Carril {lane_no}"),
            lane_no=lane_no,
            cam_no=cam_no,
            cam_name=cam.get("name", f"Cámara {cam_no}"),
            role_label=cam.get("role_label", ""),
            c=cam["runtime"],
            materialized_url=materialized_url or "—",
            resolved_ip=resolved_ip or "—",
            url_source=url_source or "—",
            msg=msg,
        )
