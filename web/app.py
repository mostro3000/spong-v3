#!/usr/bin/env python3
"""SPONG web interface - Flask application.

Run with:
    python3 /usr/local/spong/web/app.py
    # or via gunicorn:
    gunicorn -w 4 -b 0.0.0.0:8080 app:app
"""

import sys
import time
import os
import re
import html
import secrets
import threading
from collections import OrderedDict
from functools import wraps

sys.path.insert(0, "/usr/local/spong")

try:
    from flask import Flask, render_template, redirect, url_for, request, jsonify, Response, g, make_response
except ImportError:
    print("Flask not installed. Run: pip3 install flask", file=sys.stderr)
    sys.exit(1)

from spong import config, database, __version__
from spong.models import worst_color
from auth_utils import check_basic_auth
import sgt_link

_COLOR_ORDER = {"red": 0, "yellow": 1, "purple": 2, "blue": 3, "clear": 4, "green": 5}
from spong.status_sender import send_ack, send_ack_del

config.load_all()

app = Flask(__name__, template_folder="templates")

from config_admin import config_bp, config_permission_available
app.register_blueprint(config_bp)

# Support reverse-proxy with path prefix (e.g. Apache ProxyPass /spong → localhost:8090)
# Apache must set:  RequestHeader set X-Forwarded-Prefix /spong
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_host=1, x_prefix=1)


_DASHBOARD_CACHE_TTL = 5.0
_dashboard_cache = {"ts": 0.0, "group_data": None, "sidebar": None}
_dashboard_cache_lock = threading.Lock()
app.config["SPONG_DASHBOARD_CACHE"] = _dashboard_cache
app.config["SPONG_DASHBOARD_CACHE_LOCK"] = _dashboard_cache_lock


def _invalidate_dashboard_cache():
    with _dashboard_cache_lock:
        _dashboard_cache["ts"] = 0.0
        _dashboard_cache["group_data"] = None
        _dashboard_cache["sidebar"] = None


_GRAPH_CACHE_TTL = max(5, int(config.get("web.graph_cache_seconds", 60) or 60))
_GRAPH_CACHE_MAX_ENTRIES = max(64, int(config.get("web.graph_cache_entries", 512) or 512))
_graph_cache = OrderedDict()
_graph_cache_lock = threading.Lock()
app.config["SPONG_GRAPH_CACHE"] = _graph_cache
app.config["SPONG_GRAPH_CACHE_LOCK"] = _graph_cache_lock

_CHECK_COOLDOWN_SECONDS = max(5, int(config.get("web.check_cooldown_seconds", 15) or 15))
_check_state = {}
_check_state_lock = threading.Lock()
_CLIENT_PLUGIN_DIR = "/usr/local/spong/spong/plugins/client"
_client_plugin_services_cache = None


def _graph_cache_get(key):
    now = time.time()
    with _graph_cache_lock:
        entry = _graph_cache.get(key)
        if entry is None:
            return None
        expires_at, status, data = entry
        if expires_at <= now:
            _graph_cache.pop(key, None)
            return None
        _graph_cache.move_to_end(key)
        return status, data


def _graph_cache_put(key, status, data):
    expires_at = time.time() + _GRAPH_CACHE_TTL
    with _graph_cache_lock:
        _graph_cache[key] = (expires_at, status, data)
        _graph_cache.move_to_end(key)
        while len(_graph_cache) > _GRAPH_CACHE_MAX_ENTRIES:
            _graph_cache.popitem(last=False)


def _read_rrd_name_map(hostname, service_prefix):
    map_path = os.path.join('/usr/local/spong/var/rrd', hostname, f'{service_prefix}-name-map')
    entries = {}
    try:
        with open(map_path, 'r') as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or ':' not in line:
                    continue
                rrd_name, mountpoint = line.split(':', 1)
                if rrd_name and mountpoint:
                    entries[mountpoint] = rrd_name
    except OSError:
        return {}
    return entries


def _disk_mountpoints_from_message(message):
    pattern = re.compile(r'\d+\s+\d+\s+\d+\s+\d+%\s+(/\S*)')
    mountpoints = []
    seen = set()
    for line in (message or '').splitlines():
        match = pattern.search(line)
        if not match:
            continue
        mountpoint = match.group(1)
        if mountpoint in seen:
            continue
        seen.add(mountpoint)
        mountpoints.append(mountpoint)
    return mountpoints


def _disk_graph_targets(hostname, service, message=''):
    if service not in ('disk', 'diski'):
        return []

    service_map = _read_rrd_name_map(hostname, service)
    if not service_map:
        return []

    rrd_dir = os.path.join('/usr/local/spong/var/rrd', hostname)
    targets = []
    seen = set()

    def add_target(mountpoint):
        rrd_name = service_map.get(mountpoint)
        if not rrd_name or rrd_name in seen:
            return
        rrd_path = os.path.join(rrd_dir, f'{service}-{rrd_name}.rrd')
        if not os.path.isfile(rrd_path):
            return
        seen.add(rrd_name)
        targets.append({
            'service': f'{service}-{rrd_name}',
            'mountpoint': mountpoint,
            'rrd_name': rrd_name,
        })

    for mountpoint in _disk_mountpoints_from_message(message):
        add_target(mountpoint)

    if targets:
        return targets

    for mountpoint in sorted(service_map, key=lambda mp: (mp != '/', mp)):
        add_target(mountpoint)

    return targets


def _client_plugin_services():
    global _client_plugin_services_cache
    if _client_plugin_services_cache is not None:
        return _client_plugin_services_cache
    names = set()
    try:
        for filename in os.listdir(_CLIENT_PLUGIN_DIR):
            if not filename.endswith(".py"):
                continue
            name = filename[:-3]
            if name == "__init__" or name.startswith(("_", ".")):
                continue
            names.add(name)
    except OSError:
        pass
    _client_plugin_services_cache = names
    return names


def _visible_service_names(hostname):
    configured = {svc_name for svc_name, _ in config.host_services(hostname)}
    client_services = set(config.get_checks()) | _client_plugin_services()
    return configured | client_services


def _is_visible_service(hostname, service):
    return service in _visible_service_names(hostname)


def _load_visible_services(hostname):
    allowed = _visible_service_names(hostname)
    return {
        svc_name: svc
        for svc_name, svc in database.load_all_services(hostname).items()
        if svc_name in allowed
    }


def _service_status_payload(hostname, service, svc=None):
    if not _is_visible_service(hostname, service):
        return None
    if svc is None:
        svc = database.load_service(hostname, service)
    if not svc:
        return None
    acks = database.load_acks(hostname)
    color = svc.color
    if color not in ("green", "blue") and any(ack.covers(service) for ack in acks):
        color = "blue"
    if color in ("red", "yellow") and config.is_suppressed(hostname, service):
        color = "clear"
    return {
        "color": color,
        "summary": svc.summary,
        "message": svc.message,
        "report_time": svc.report_time,
        "duration": svc.duration,
    }


def _check_begin(hostname, service):
    key = (hostname, service)
    now = time.time()
    with _check_state_lock:
        entry = _check_state.get(key)
        if entry:
            if entry.get("running"):
                return False, "running"
            if now - entry.get("finished_at", 0.0) < _CHECK_COOLDOWN_SECONDS:
                return False, "cooldown"
        _check_state[key] = {
            "running": True,
            "finished_at": entry.get("finished_at", 0.0) if entry else 0.0,
        }
        return True, None


def _check_end(hostname, service):
    key = (hostname, service)
    now = time.time()
    with _check_state_lock:
        entry = _check_state.get(key)
        if entry is None:
            _check_state[key] = {"running": False, "finished_at": now}
        else:
            entry["running"] = False
            entry["finished_at"] = now
        stale_before = now - (_CHECK_COOLDOWN_SECONDS * 4)
        stale_keys = [
            old_key for old_key, old_entry in _check_state.items()
            if not old_entry.get("running") and old_entry.get("finished_at", 0.0) < stale_before
        ]
        for old_key in stale_keys:
            _check_state.pop(old_key, None)


def _build_dashboard_snapshot():
    groups = config.get_groups()
    group_data = []
    sidebar = []
    for gname, gdata in groups.items():
        if not gdata.get("display", True):
            continue
        members = gdata.get("members", [])
        host_statuses = {}
        red_items = []
        for host in members:
            services = _load_visible_services(host)
            acks = database.load_acks(host)
            _apply_ack_colors(services, acks)
            _apply_schedule_suppression(host, services)
            host_color = worst_color([
                "green" if s.color == "blue" else s.color
                for s in services.values()
            ]) if services else "green"
            host_statuses[host] = {"color": host_color, "services": services}
            red_svcs = [svc_name for svc_name, svc in services.items() if svc.color == "red"]
            if len(red_svcs) == 1:
                red_items.append({"host": host, "service": red_svcs[0]})
            elif len(red_svcs) > 1:
                red_items.append({"host": host, "service": "multiple"})

        if red_items:
            sidebar.append({
                "name": gdata.get("name", gname),
                "key": gname,
                "problems": red_items,
                "count": len(red_items),
            })

        group_color = worst_color([
            "green" if v["color"] == "blue" else v["color"]
            for v in host_statuses.values()
        ]) if host_statuses else "green"

        seen_cols = {}
        for host in members:
            for svc_name, _ in config.host_services(host):
                if svc_name not in seen_cols:
                    seen_cols[svc_name] = None
        for data in host_statuses.values():
            for svc_name in data["services"]:
                if svc_name not in seen_cols:
                    seen_cols[svc_name] = None

        pairs = {"http": "https", "ssh": "telnet"}
        cols = list(seen_cols.keys())
        service_cols = []
        skip = set()
        for svc in cols:
            if svc in skip:
                continue
            service_cols.append(svc)
            partner = pairs.get(svc)
            if partner and partner in seen_cols:
                service_cols.append(partner)
                skip.add(partner)

        group_data.append({
            "name": gdata.get("name", gname),
            "key": gname,
            "color": group_color,
            "hosts": host_statuses,
            "compress": gdata.get("compress", False),
            "service_cols": service_cols,
        })

    return group_data, sidebar


def _get_dashboard_snapshot():
    now = time.time()
    with _dashboard_cache_lock:
        if now - _dashboard_cache["ts"] < _DASHBOARD_CACHE_TTL and _dashboard_cache["group_data"] is not None:
            return _dashboard_cache["group_data"], _dashboard_cache["sidebar"]

    group_data, sidebar = _build_dashboard_snapshot()

    with _dashboard_cache_lock:
        _dashboard_cache["ts"] = now
        _dashboard_cache["group_data"] = group_data
        _dashboard_cache["sidebar"] = sidebar

    return group_data, sidebar




_SPONG_ROLES = ("admin", "editor", "add", "read", "view")
_SPONG_ROLE_ALIASES = {
    "owner":     "admin",
    "write":     "editor",
    "readonly":  "read",
    "read-only": "read",
    "viewer":    "read",
    "add-only":  "add",
    "add_only":  "add",
}
_SPONG_ACK_ROLES = frozenset({"admin", "editor"})
_SPONG_REALM = "SPONG"
_SPONG_LOGGED_OUT_REALM = "SPONG signed out"


def _normalize_spong_role(role):
    role = (role or "view").strip().lower()
    return _SPONG_ROLE_ALIASES.get(role, role if role in _SPONG_ROLES else "view")


def _spong_user_entries():
    """Return {username: {password, password_hash, role}} for spong UI auth.

    Reads multi-user `web.users` and falls back to legacy single-user
    `web.auth_user` / `web.auth_password` (treated as admin).
    """
    entries = {}
    users_cfg = config.get("web.users", {})
    if isinstance(users_cfg, dict):
        for username, entry in users_cfg.items():
            username = str(username or "").strip()
            if not username or not isinstance(entry, dict):
                continue
            entries[username] = {
                "password": entry.get("password", ""),
                "password_hash": entry.get("password_hash", ""),
                "role": _normalize_spong_role(entry.get("role")),
            }

    legacy_user = config.get("web.auth_user", "")
    if legacy_user and legacy_user not in entries:
        entries[legacy_user] = {
            "password": config.get("web.auth_password", ""),
            "password_hash": config.get("web.auth_password_hash", ""),
            "role": "admin",
        }
    return entries


def _authenticate_spong_user(username, password):
    for expected_user, entry in _spong_user_entries().items():
        if check_basic_auth(username, password, expected_user, entry["password"], entry["password_hash"]):
            return expected_user, entry["role"]
    return None


def _spong_no_store(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _spong_auth_page(title, message, *, icon="🔒"):
    """Render a self-contained styled HTML page for 401 responses.

    No template inheritance: avoids leaking sidebar / nav to unauthenticated users
    and works regardless of session state.
    """
    theme = request.cookies.get("theme", "dark")
    if theme not in ("light", "dark"):
        theme = "dark"
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    safe_icon = html.escape(icon)
    return (
        '<!DOCTYPE html>\n'
        f'<html lang="es" class="{theme}"><head>'
        '<meta charset="UTF-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">'
        f'<title>SPONG — {safe_title}</title>'
        '<style>'
        ':root{--bg:#f0f2f5;--surface:#fff;--surface2:#f8f9fb;--border:#e0e4ea;'
        '--text:#1a1a2e;--text-h:#1e2d40;--text-m:#546e7a;'
        '--accent:#4a6fa5;--accent-h:#2e4d7a;'
        '--icon-bg:#fff2f2;--icon-fg:#b32626;--icon-brd:#efb5b5;}'
        'html.dark{--bg:#0d1b2a;--surface:#132232;--surface2:#192d3e;--border:#2a4258;'
        '--text:#cdd8e3;--text-h:#e0eaf4;--text-m:#8bafc7;'
        '--accent:#5b9bd5;--accent-h:#7aabdb;'
        '--icon-bg:#4a1515;--icon-fg:#f08080;--icon-brd:#6a2020;}'
        '*{box-sizing:border-box;margin:0;padding:0;}'
        'body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;'
        'font-size:13px;background:var(--bg);color:var(--text);'
        'min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}'
        '.auth-card{background:var(--surface);border:1px solid var(--border);'
        'border-radius:10px;padding:36px 40px;max-width:480px;width:100%;'
        'box-shadow:0 6px 24px rgba(0,0,0,0.08);text-align:center;}'
        'html.dark .auth-card{box-shadow:0 6px 24px rgba(0,0,0,0.35);}'
        '.auth-icon{width:64px;height:64px;border-radius:50%;'
        'display:inline-flex;align-items:center;justify-content:center;'
        'font-size:30px;margin-bottom:20px;'
        'background:var(--icon-bg);color:var(--icon-fg);border:1px solid var(--icon-brd);}'
        '.auth-title{font-size:20px;font-weight:600;color:var(--text-h);margin-bottom:10px;}'
        '.auth-body{font-size:14px;color:var(--text-m);margin-bottom:24px;line-height:1.5;}'
        '.auth-btn{display:inline-block;padding:9px 22px;background:var(--accent);'
        'color:#fff;border-radius:5px;font-size:13px;font-weight:600;'
        'text-decoration:none;border:0;cursor:pointer;transition:background 0.15s;}'
        '.auth-btn:hover{background:var(--accent-h);text-decoration:none;}'
        '.auth-brand{margin-top:18px;font-size:11px;color:var(--text-m);opacity:0.6;'
        'letter-spacing:0.5px;text-transform:uppercase;}'
        '</style></head><body>'
        '<div class="auth-card">'
        f'<div class="auth-icon">{safe_icon}</div>'
        f'<div class="auth-title">{safe_title}</div>'
        f'<div class="auth-body">{safe_message}</div>'
        '<a href="" onclick="location.reload();return false;" class="auth-btn">Reintentar</a>'
        '<div class="auth-brand">● SPONG</div>'
        '</div></body></html>'
    )


@app.before_request
def require_auth():
    # /config/ tiene su propio auth gestionado por el Blueprint.
    if request.path.startswith('/config'):
        return

    g.spong_user = ""
    g.spong_role = ""

    # /logout debe ser alcanzable sin credenciales para poder cerrar la sesión Basic.
    if request.endpoint == "logout":
        return

    entries = _spong_user_entries()
    if not entries:
        # auth deshabilitada → todos pueden hacer todo
        g.spong_role = "admin"
        return

    auth = request.authorization
    identity = _authenticate_spong_user(auth.username, auth.password) if auth else None

    logged_out_token = request.cookies.get("spong_logged_out")
    reauth_token = request.cookies.get("spong_reauth")
    if logged_out_token:
        realm = f"{_SPONG_LOGGED_OUT_REALM} {logged_out_token}"
        if reauth_token != logged_out_token:
            body = _spong_auth_page(
                "Sesión cerrada",
                "Volvé a autenticarte para entrar al monitor.",
            )
            resp = Response(
                body,
                401,
                {"WWW-Authenticate": f'Basic realm="{realm}"'},
                mimetype="text/html",
            )
            resp.set_cookie("spong_reauth", logged_out_token, max_age=5 * 60, samesite="Lax")
            return _spong_no_store(resp)
        if not identity:
            body = _spong_auth_page(
                "Sesión cerrada",
                "Volvé a autenticarte para entrar al monitor.",
            )
            return _spong_no_store(Response(
                body,
                401,
                {"WWW-Authenticate": f'Basic realm="{realm}"'},
                mimetype="text/html",
            ))
        g.spong_user, g.spong_role = identity
        # se limpian las cookies en after_request via flag
        g.spong_clear_logout = True
        return

    if not identity:
        body = _spong_auth_page(
            "Acceso restringido",
            "Ingresá usuario y contraseña para entrar al monitor.",
        )
        return _spong_no_store(Response(
            body,
            401,
            {"WWW-Authenticate": f'Basic realm="{_SPONG_REALM}"'},
            mimetype="text/html",
        ))
    g.spong_user, g.spong_role = identity


def _spong_role_denied(message: str) -> Response:
    html = render_template(
        "error.html",
        title="Permiso insuficiente",
        message=message,
        back_url=request.referrer or url_for("index"),
        back_label="Volver",
    )
    return Response(html, status=403, mimetype="text/html")


def _require_spong_admin():
    """Return a 403 Response if the current request lacks admin rights, else None."""
    if getattr(g, "spong_role", "") != "admin":
        return _spong_role_denied("Esta acción requiere un usuario con rol de administrador.")
    return None


def _require_spong_ack():
    """Return a 403 Response if the current request can't ack/unack, else None."""
    if getattr(g, "spong_role", "") not in _SPONG_ACK_ROLES:
        return _spong_role_denied("Esta acción requiere un usuario con rol administrador o editor.")
    return None


def require_spong_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        denied = _require_spong_admin()
        if denied is not None:
            return denied
        return f(*args, **kwargs)
    return decorated


def require_spong_ack(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        denied = _require_spong_ack()
        if denied is not None:
            return denied
        return f(*args, **kwargs)
    return decorated


@app.route("/logout")
def logout():
    logout_token = secrets.token_urlsafe(8)
    resp = redirect(url_for("index"))
    resp.set_cookie("spong_logged_out", logout_token, max_age=30 * 60, samesite="Lax")
    resp.delete_cookie("spong_reauth")
    return _spong_no_store(resp)

# ---- Color helpers ----
COLORS_CSS = {
    "red": "#cc0000",
    "yellow": "#ffff00",
    "green": "#339900",
    "purple": "#990099",
    "blue": "#0000ff",
    "clear": "#ffffff",
}


def color_style(color: str) -> str:
    return f"background-color:{COLORS_CSS.get(color, '#ffffff')};color:{'#fff' if color in ('red','purple','blue') else '#000'}"


app.jinja_env.globals["color_style"] = color_style
app.jinja_env.globals["worst_color"] = worst_color
app.jinja_env.globals["time"] = time
app.jinja_env.globals["spong_version"] = __version__

# ---------------------------------------------------------------------------
# i18n — simple dict-based translation, no external deps
# ---------------------------------------------------------------------------
_SUPPORTED_LANGS = ("es", "en", "fr", "de", "pt", "zh", "ru")

_LANG_META = {
    "es": ("🇪🇸", "Español"),
    "en": ("🇬🇧", "English"),
    "fr": ("🇫🇷", "Français"),
    "de": ("🇩🇪", "Deutsch"),
    "pt": ("🇧🇷", "Português"),
    "zh": ("🇨🇳", "中文"),
    "ru": ("🇷🇺", "Русский"),
}

_TRANSLATIONS: dict = {
    "en": {
        "Grupos": "Groups", "Problemas": "Problems", "Reconocidos": "Acknowledged",
        "Con problemas": "With problems", "Sin problemas": "No problems", "grupos": "groups",
        "Actualizar ahora": "Refresh now", "clic para cerrar": "click to close",
        "creado por mt": "created by mt",
        "Disponibilidad": "Availability", "Disponibilidad histórica": "Historical availability",
        "Historial": "History", "Historial general": "Global history",
        "Grupos de Hosts": "Host Groups", "Sin datos.": "No data.",
        "Servicio": "Service", "Servicios": "Services", "Estado": "Status",
        "Resumen": "Summary", "Contacto": "Contact", "Hasta": "Until",
        "Mensaje": "Message", "Borrar": "Delete", "Reconocer": "Acknowledge",
        "En este estado": "In this state", "Último reporte": "Last report",
        "Graf.": "Graph", "Sin vencimiento": "Never", "desde": "since",
        "Reconocimientos activos": "Active acknowledgements",
        "Historial (últimos 7 días)": "History (last 7 days)",
        "Fecha": "Date", "Tipo": "Type",
        "Problemas actuales": "Current problems",
        "Todo OK — sin problemas activos": "All OK — no active problems",
        "Detalles del servicio": "Service details", "Última actualización": "Last update",
        "Reconocido": "Acknowledged", "Detalle": "Detail", "Gráficos": "Graphs",
        "1 hora": "1 hour", "24 horas": "24 hours", "7 días": "7 days",
        "30 días": "30 days", "1 año": "1 year",
        "No hay datos para este servicio.": "No data for this service.",
        "No hay reconocimientos activos.": "No active acknowledgements.",
        "Sin cambios de estado en los últimos 30 días": "No status changes in the last 30 days.",
        "Patrón": "Pattern", "Estado actual": "Current status",
        "restantes": "remaining", "+ Nuevo reconocimiento": "+ New acknowledgement",
        "Nuevo reconocimiento": "New acknowledgement", "Duración": "Duration", "Cancelar": "Cancel",
    },
    "fr": {
        "Grupos": "Groupes", "Problemas": "Problèmes", "Reconocidos": "Acquittements",
        "Con problemas": "Avec problèmes", "Sin problemas": "Sans problèmes", "grupos": "groupes",
        "Actualizar ahora": "Actualiser", "clic para cerrar": "cliquer pour fermer",
        "creado por mt": "créé par mt",
        "Disponibilidad": "Disponibilité", "Disponibilidad histórica": "Disponibilité historique",
        "Historial": "Historique", "Historial general": "Historique global",
        "Grupos de Hosts": "Groupes d'hôtes", "Sin datos.": "Pas de données.",
        "Servicio": "Service", "Servicios": "Services", "Estado": "État",
        "Resumen": "Résumé", "Contacto": "Contact", "Hasta": "Jusqu'à",
        "Mensaje": "Message", "Borrar": "Supprimer", "Reconocer": "Acquitter",
        "En este estado": "Dans cet état", "Último reporte": "Dernier rapport",
        "Graf.": "Graph", "Sin vencimiento": "Jamais", "desde": "depuis",
        "Reconocimientos activos": "Acquittements actifs",
        "Historial (últimos 7 días)": "Historique (7 derniers jours)",
        "Fecha": "Date", "Tipo": "Type",
        "Problemas actuales": "Problèmes actuels",
        "Todo OK — sin problemas activos": "Tout OK — aucun problème actif",
        "Detalles del servicio": "Détails du service", "Última actualización": "Dernière mise à jour",
        "Reconocido": "Acquitté", "Detalle": "Détail", "Gráficos": "Graphiques",
        "1 hora": "1 heure", "24 horas": "24 heures", "7 días": "7 jours",
        "30 días": "30 jours", "1 año": "1 an",
        "No hay datos para este servicio.": "Pas de données pour ce service.",
        "No hay reconocimientos activos.": "Aucun acquittement actif.",
        "Sin cambios de estado en los últimos 30 días": "Aucun changement d'état sur les 30 derniers jours.",
        "Patrón": "Modèle", "Estado actual": "État actuel",
        "restantes": "restants", "+ Nuevo reconocimiento": "+ Nouvel acquittement",
        "Nuevo reconocimiento": "Nouvel acquittement", "Duración": "Durée", "Cancelar": "Annuler",
    },
    "de": {
        "Grupos": "Gruppen", "Problemas": "Probleme", "Reconocidos": "Bestätigt",
        "Con problemas": "Mit Problemen", "Sin problemas": "Keine Probleme", "grupos": "Gruppen",
        "Actualizar ahora": "Jetzt aktualisieren", "clic para cerrar": "klicken zum Schließen",
        "creado por mt": "erstellt von mt",
        "Disponibilidad": "Verfügbarkeit", "Disponibilidad histórica": "Historische Verfügbarkeit",
        "Historial": "Verlauf", "Historial general": "Globaler Verlauf",
        "Grupos de Hosts": "Host-Gruppen", "Sin datos.": "Keine Daten.",
        "Servicio": "Dienst", "Servicios": "Dienste", "Estado": "Status",
        "Resumen": "Zusammenfassung", "Contacto": "Kontakt", "Hasta": "Bis",
        "Mensaje": "Nachricht", "Borrar": "Löschen", "Reconocer": "Bestätigen",
        "En este estado": "In diesem Zustand", "Último reporte": "Letzter Bericht",
        "Graf.": "Graph", "Sin vencimiento": "Nie", "desde": "seit",
        "Reconocimientos activos": "Aktive Bestätigungen",
        "Historial (últimos 7 días)": "Verlauf (letzte 7 Tage)",
        "Fecha": "Datum", "Tipo": "Typ",
        "Problemas actuales": "Aktuelle Probleme",
        "Todo OK — sin problemas activos": "Alles OK — keine aktiven Probleme",
        "Detalles del servicio": "Dienstdetails", "Última actualización": "Letzte Aktualisierung",
        "Reconocido": "Bestätigt", "Detalle": "Detail", "Gráficos": "Graphen",
        "1 hora": "1 Stunde", "24 horas": "24 Stunden", "7 días": "7 Tage",
        "30 días": "30 Tage", "1 año": "1 Jahr",
        "No hay datos para este servicio.": "Keine Daten für diesen Dienst.",
        "No hay reconocimientos activos.": "Keine aktiven Bestätigungen.",
        "Sin cambios de estado en los últimos 30 días": "Keine Statusänderungen in den letzten 30 Tagen.",
        "Patrón": "Muster", "Estado actual": "Aktueller Status",
        "restantes": "verbleibend", "+ Nuevo reconocimiento": "+ Neue Bestätigung",
        "Nuevo reconocimiento": "Neue Bestätigung", "Duración": "Dauer", "Cancelar": "Abbrechen",
    },
    "pt": {
        "Grupos": "Grupos", "Problemas": "Problemas", "Reconocidos": "Reconhecidos",
        "Con problemas": "Com problemas", "Sin problemas": "Sem problemas", "grupos": "grupos",
        "Actualizar ahora": "Atualizar agora", "clic para cerrar": "clique para fechar",
        "creado por mt": "criado por mt",
        "Disponibilidad": "Disponibilidade", "Disponibilidad histórica": "Disponibilidade histórica",
        "Historial": "Histórico", "Historial general": "Histórico geral",
        "Grupos de Hosts": "Grupos de Hosts", "Sin datos.": "Sem dados.",
        "Servicio": "Serviço", "Servicios": "Serviços", "Estado": "Estado",
        "Resumen": "Resumo", "Contacto": "Contato", "Hasta": "Até",
        "Mensaje": "Mensagem", "Borrar": "Excluir", "Reconocer": "Reconhecer",
        "En este estado": "Neste estado", "Último reporte": "Último relatório",
        "Graf.": "Gráfico", "Sin vencimiento": "Nunca", "desde": "desde",
        "Reconocimientos activos": "Reconhecimentos ativos",
        "Historial (últimos 7 días)": "Histórico (últimos 7 dias)",
        "Fecha": "Data", "Tipo": "Tipo",
        "Problemas actuales": "Problemas atuais",
        "Todo OK — sin problemas activos": "Tudo OK — sem problemas ativos",
        "Detalles del servicio": "Detalhes do serviço", "Última actualización": "Última atualização",
        "Reconocido": "Reconhecido", "Detalle": "Detalhe", "Gráficos": "Gráficos",
        "1 hora": "1 hora", "24 horas": "24 horas", "7 días": "7 dias",
        "30 días": "30 dias", "1 año": "1 ano",
        "No hay datos para este servicio.": "Sem dados para este serviço.",
        "No hay reconocimientos activos.": "Nenhum reconhecimento ativo.",
        "Sin cambios de estado en los últimos 30 días": "Sem mudanças de estado nos últimos 30 dias.",
        "Patrón": "Padrão", "Estado actual": "Estado atual",
        "restantes": "restantes", "+ Nuevo reconocimiento": "+ Novo reconhecimento",
        "Nuevo reconocimiento": "Novo reconhecimento", "Duración": "Duração", "Cancelar": "Cancelar",
    },
    "zh": {
        "Grupos": "组", "Problemas": "问题", "Reconocidos": "已确认",
        "Con problemas": "有问题", "Sin problemas": "无问题", "grupos": "组",
        "Actualizar ahora": "立即刷新", "clic para cerrar": "点击关闭",
        "creado por mt": "由 mt 创建",
        "Disponibilidad": "可用性", "Disponibilidad histórica": "历史可用性",
        "Historial": "历史", "Historial general": "全局历史",
        "Grupos de Hosts": "主机组", "Sin datos.": "无数据。",
        "Servicio": "服务", "Servicios": "服务", "Estado": "状态",
        "Resumen": "摘要", "Contacto": "联系人", "Hasta": "截止",
        "Mensaje": "消息", "Borrar": "删除", "Reconocer": "确认",
        "En este estado": "在此状态", "Último reporte": "最后报告",
        "Graf.": "图表", "Sin vencimiento": "永不", "desde": "自",
        "Reconocimientos activos": "活动确认",
        "Historial (últimos 7 días)": "历史（最近7天）",
        "Fecha": "日期", "Tipo": "类型",
        "Problemas actuales": "当前问题",
        "Todo OK — sin problemas activos": "一切正常——无活动问题",
        "Detalles del servicio": "服务详情", "Última actualización": "最后更新",
        "Reconocido": "已确认", "Detalle": "详情", "Gráficos": "图表",
        "1 hora": "1小时", "24 horas": "24小时", "7 días": "7天",
        "30 días": "30天", "1 año": "1年",
        "No hay datos para este servicio.": "此服务无数据。",
        "No hay reconocimientos activos.": "无活动确认。",
        "Sin cambios de estado en los últimos 30 días": "最近 30 天没有状态变化。",
        "Patrón": "模式", "Estado actual": "当前状态",
        "restantes": "剩余", "+ Nuevo reconocimiento": "+ 新确认",
        "Nuevo reconocimiento": "新确认", "Duración": "持续时间", "Cancelar": "取消",
    },
    "ru": {
        "Grupos": "Группы", "Problemas": "Проблемы", "Reconocidos": "Подтверждено",
        "Con problemas": "С проблемами", "Sin problemas": "Нет проблем", "grupos": "групп",
        "Actualizar ahora": "Обновить", "clic para cerrar": "нажмите для закрытия",
        "creado por mt": "создано mt",
        "Disponibilidad": "Доступность", "Disponibilidad histórica": "Историческая доступность",
        "Historial": "История", "Historial general": "Общая история",
        "Grupos de Hosts": "Группы хостов", "Sin datos.": "Нет данных.",
        "Servicio": "Сервис", "Servicios": "Сервисы", "Estado": "Статус",
        "Resumen": "Сводка", "Contacto": "Контакт", "Hasta": "До",
        "Mensaje": "Сообщение", "Borrar": "Удалить", "Reconocer": "Подтвердить",
        "En este estado": "В этом состоянии", "Último reporte": "Последний отчёт",
        "Graf.": "График", "Sin vencimiento": "Никогда", "desde": "с",
        "Reconocimientos activos": "Активные подтверждения",
        "Historial (últimos 7 días)": "История (последние 7 дней)",
        "Fecha": "Дата", "Tipo": "Тип",
        "Problemas actuales": "Текущие проблемы",
        "Todo OK — sin problemas activos": "Всё OK — нет активных проблем",
        "Detalles del servicio": "Детали сервиса", "Última actualización": "Последнее обновление",
        "Reconocido": "Подтверждено", "Detalle": "Детали", "Gráficos": "Графики",
        "1 hora": "1 час", "24 horas": "24 часа", "7 días": "7 дней",
        "30 días": "30 дней", "1 año": "1 год",
        "No hay datos para este servicio.": "Нет данных для этого сервиса.",
        "No hay reconocimientos activos.": "Нет активных подтверждений.",
        "Sin cambios de estado en los últimos 30 días": "Нет изменений состояния за последние 30 дней.",
        "Patrón": "Шаблон", "Estado actual": "Текущий статус",
        "restantes": "осталось", "+ Nuevo reconocimiento": "+ Новое подтверждение",
        "Nuevo reconocimiento": "Новое подтверждение", "Duración": "Длительность", "Cancelar": "Отмена",
    },
}

_EXTRA_TRANSLATIONS = {
    "en": {
        "Acción": "Action", "Agregar el primero": "Add the first one",
        "Agregar grupo": "Add group", "Agregar horario": "Add schedule", "Agregar host": "Add host",
        "Agregar usuario": "Add user",
        "Ampliar grupo": "Expand group",
        "Archivo": "File", "Backup": "Backup", "Backup previo": "Pre-restore backup",
        "Blue": "Blue", "Borrar reconocimiento": "Delete acknowledgement",
        "Buscar host...": "Search host...", "Básico": "Basic", "Clave": "Key",
        "Clave interna": "Internal key", "Clear": "Clear",
        "Clic para verificar ahora": "Click to check now", "Comparar períodos": "Compare periods",
        "Configuración": "Configuration", "Configurada": "Configured",
        "Contenido completo del historial": "Full history content",
        "D": "S", "Datos del grupo": "Group data", "Datos del host": "Host data",
        "Descripción": "Description", "Desde": "From",
        "Diferencias respecto al estado actual": "Differences from current state",
        "Dirección IP": "IP address",
        "Durante estos horarios, si el servicio está en rojo, se mostrará en blanco en el dashboard (no dispara alerta).":
            "During these schedules, if the service is red, it will be shown as white on the dashboard (no alert is triggered).",
        "Días": "Days", "Editado": "Edited", "Editar": "Edit", "Editar grupo": "Edit group",
        "Editar grupo:": "Edit group:", "Editar host:": "Edit host:",
        "Editar servicios": "Edit services",
        "El estado actual se guardará como backup automático antes de restaurar.":
            "The current state will be saved as an automatic backup before restoring.",
        "El historial es idéntico al estado actual - no hay diferencias.":
            "The history entry is identical to the current state - there are no differences.",
        "El historial siempre guarda el estado real.": "History always stores the real state.",
        "El nombre no se puede cambiar. Para renombrar, eliminá y volvé a crear.":
            "The name cannot be changed. To rename it, delete it and create it again.",
        "Eliminado": "Deleted", "Fecha y hora": "Date and time",
        "Gestionar grupos": "Manage groups", "Gestionar hosts": "Manage hosts",
        "Green": "Green", "Guardar": "Save", "Historial de cambios": "Change history",
        "Horarios de supresión de alertas": "Alert suppression schedules",
        "Hosts": "Hosts", "Hosts en este grupo": "Hosts in this group",
        "IP / Dirección": "IP / Address",
        "Identificador único, sin espacios ni caracteres especiales.":
            "Unique identifier, without spaces or special characters.",
        "J": "T", "L": "M", "Los próximos cambios que hagas desde aquí quedarán registrados automáticamente.":
            "Future changes made here will be recorded automatically.",
        "M": "T", "Marcá los hosts que pertenecen a este grupo.":
            "Select the hosts that belong to this group.",
        "Marcá los servicios que querés verificar en este host.":
            "Select the services you want to check on this host.",
        "Menú": "Menu", "Miembros": "Members",
        "Cambiar modo de visualización de grupos": "Change group display mode",
        "Minimizar grupo": "Collapse group", "Modo claro": "Light mode",
        "rojo expandido": "red expanded",
        "problemas expandidos": "problems expanded",
        "todos expandidos": "all expanded",
        "todos minimizados": "all collapsed",
        "Modo oscuro": "Dark mode", "No hay cambios registrados aún.": "No changes recorded yet.",
        "No hay grupos configurados.": "No groups configured.",
        "No hay hosts configurados.": "No hosts configured.",
        "Nombre": "Name", "Nombre del host": "Host name", "Nombre para mostrar": "Display name",
        "Nuevo": "New", "Nuevo grupo": "New group", "Nuevo host": "New host",
        "Ocultar gráficos": "Hide graphs", "Opciones": "Options",
        "Ordenar por clave": "Sort by key",
        "Ordenar por host": "Sort by host",
        "Ordenar por IP": "Sort by IP",
        "Ordenar por nombre": "Sort by name",
        "Plugins que no aparecen arriba, separados por espacio.":
            "Plugins not shown above, separated by spaces.",
        "Purple": "Purple", "Red": "Red", "Restaurado": "Restored",
        "Restaurar": "Restore", "Restaurar esta versión": "Restore this version",
        "S": "S", "Salir": "Log out", "Cerrar sesión": "Sign out",
        "admin": "admin", "view": "view-only",
        "Servicios a monitorear": "Services to monitor",
        "Servicios adicionales": "Additional services", "Si ping falla,": "If ping fails,",
        "Sin datos de disponibilidad": "No availability data",
        "Tiene horarios de supresión": "Has suppression schedules",
        "Todos": "All", "Una o más IPs separadas por coma. Si se deja vacío se usa el nombre.":
            "One or more comma-separated IPs. If left empty, the name is used.",
        "Usuario": "User",
        "V": "F", "Ver": "View", "Ver gráficos": "Show graphs", "Ver historial": "View history",
        "Visible en el dashboard": "Visible on the dashboard", "Vista compacta": "Compact view",
        "Volver": "Back", "Volver al historial": "Back to history", "Volver al monitor": "Back to monitor",
        "X": "W", "Yellow": "Yellow",
        "agregar, editar o eliminar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "add, edit, or delete monitored devices: IP address, services to check, and alert suppression schedules.",
        "agregar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "add monitored devices: IP address, services to check, and alert suppression schedules.",
        "consultar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "view monitored devices: IP address, services to check, and alert suppression schedules.",
        "cambios registrados": "recorded changes", "compacto": "compact",
        "desmarcar para ocultar el grupo": "uncheck to hide the group",
        "ej: 192.168.0.50": "e.g. 192.168.0.50", "ej: Cámaras": "e.g. Cameras",
        "ej: Cámaras IP de seguridad": "e.g. Security IP cameras",
        "ej: camara-jardin": "e.g. garden-camera",
        "ej: camara1 dvrcam3 ups_snmp": "e.g. camara1 dvrcam3 ups_snmp",
        "ej: camaras": "e.g. cameras", "ej: altas": "e.g. altas", "ej: cpu": "e.g. cpu",
        "grupos configurados": "configured groups", "hace": "ago", "hosts": "hosts",
        "hosts configurados": "configured hosts",
        "muestra solo el color de cada host, sin columnas de servicios":
            "shows only each host color, without service columns",
        "más": "more", "no verificar el resto de los servicios": "do not check the remaining services",
        "oculto": "hidden", "opcional": "optional",
        "organizar los hosts en grupos para la vista del dashboard.":
            "organize hosts into groups for the dashboard view.",
        "agregar grupos para la vista del dashboard.": "add groups for the dashboard view.",
        "consultar los grupos de la vista del dashboard.": "view the groups in the dashboard view.",
        "recomendado para equipos en red local": "recommended for local network devices",
        "rojo": "red", "se agregará al restaurar": "will be added on restore",
        "se eliminará al restaurar": "will be removed on restore", "seleccionados": "selected",
        "verde": "green", "¿Borrar el reconocimiento de": "Delete acknowledgement for",
        "¿Eliminar el grupo": "Delete group", "¿Eliminar el host": "Delete host",
        "¿Qué puedo hacer aquí?": "What can I do here?",
        "¿Restaurar esta versión de": "Restore this version of",
        "Conectividad": "Connectivity", "Red / Monitoreo": "Network / Monitoring",
        "Rendimiento": "Performance", "Almacenamiento": "Storage", "Sensores": "Sensors",
        "Cámaras": "Cameras", "Cliente": "Client", "Otros": "Other",
        "El nombre del host es obligatorio.": "Host name is required.",
        "La clave del grupo es obligatoria.": "Group key is required.",
        "Administrador": "Administrator",
        "Cambios recientes de usuarios": "Recent user changes",
        "Contraseña": "Password",
        "Datos del usuario": "User data",
        "Dejar vacío para conservar la actual": "Leave empty to keep the current one",
        "Editar usuario": "Edit user",
        "Editar usuario:": "Edit user:",
        "El usuario legacy de configuración sigue activo": "The legacy config user is still active",
        "El nombre de usuario es obligatorio.": "Username is required.",
        "El nombre de usuario solo puede tener letras, números, puntos, guiones y guiones bajos.":
            "The username can only contain letters, numbers, dots, hyphens, and underscores.",
        "Ese usuario es el administrador legacy y se administra desde spong.yaml.":
            "That user is the legacy administrator and is managed from spong.yaml.",
        "Gestionar usuarios": "Manage users",
        "Hash de contraseña": "Password hash",
        "Ingresá la contraseña": "Enter the password",
        "No hay usuarios adicionales configurados.": "No additional users configured.",
        "Nombre de usuario": "Username",
        "Nuevo usuario": "New user",
        "Opcional, si querés pegar un hash ya generado": "Optional, if you want to paste an already generated hash",
        "Rol": "Role",
        "Se guardará como hash en spong.yaml.": "It will be stored as a hash in spong.yaml.",
        "Solo agregar": "Add only",
        "Solo lectura": "Read only",
        "Tenés que cargar una contraseña.": "You need to provide a password.",
        "Tenés que cargar una contraseña o un hash de contraseña.": "You need to provide a password or a password hash.",
        "Tiene que quedar al menos un usuario administrador.": "At least one administrator user must remain.",
        "Usá letras, números, punto, guion y guion bajo.": "Use letters, numbers, dots, hyphens, and underscores.",
        "Solo letras, números, puntos, guiones y guiones bajos. Sin barras ni espacios.":
            "Only letters, numbers, dots, hyphens, and underscores. No slashes or spaces.",
        "Usuario": "User",
        "Usuarios": "Users",
        "usuarios configurados": "configured users",
        "Usuarios de configuración": "Config users",
        "Ya existe un usuario con ese nombre.": "A user with that name already exists.",
        "¿Eliminar el usuario": "Delete user",
        "Ya existe un host con ese nombre.": "A host with that name already exists.",
        "Ya existe un grupo con esa clave.": "A group with that key already exists.",
    },
    "fr": {
        "Acción": "Action", "Agregar el primero": "Ajouter le premier",
        "Agregar grupo": "Ajouter un groupe", "Agregar horario": "Ajouter un horaire",
        "Ampliar grupo": "Développer le groupe",
        "Agregar host": "Ajouter un hôte", "Archivo": "Fichier", "Backup": "Sauvegarde",
        "Backup previo": "Sauvegarde préalable", "Blue": "Bleu",
        "Borrar reconocimiento": "Supprimer l'acquittement", "Buscar host...": "Rechercher un hôte...",
        "Básico": "Base", "Clave": "Clé", "Clave interna": "Clé interne", "Clear": "Clair",
        "Clic para verificar ahora": "Cliquer pour vérifier maintenant",
        "Comparar períodos": "Comparer les périodes", "Configuración": "Configuration",
        "Contenido completo del historial": "Contenu complet de l'historique",
        "D": "D", "Datos del grupo": "Données du groupe", "Datos del host": "Données de l'hôte",
        "Descripción": "Description", "Desde": "De",
        "Diferencias respecto al estado actual": "Différences par rapport à l'état actuel",
        "Dirección IP": "Adresse IP",
        "Durante estos horarios, si el servicio está en rojo, se mostrará en blanco en el dashboard (no dispara alerta).":
            "Pendant ces horaires, si le service est rouge, il sera affiché en blanc sur le tableau de bord (sans déclencher d'alerte).",
        "Días": "Jours", "Editado": "Modifié", "Editar": "Modifier",
        "Editar grupo": "Modifier le groupe", "Editar grupo:": "Modifier le groupe:",
        "Editar host:": "Modifier l'hôte:",
        "El estado actual se guardará como backup automático antes de restaurar.":
            "L'état actuel sera enregistré comme sauvegarde automatique avant la restauration.",
        "El historial es idéntico al estado actual - no hay diferencias.":
            "L'historique est identique à l'état actuel - aucune différence.",
        "El historial siempre guarda el estado real.": "L'historique conserve toujours l'état réel.",
        "El nombre no se puede cambiar. Para renombrar, eliminá y volvé a crear.":
            "Le nom ne peut pas être modifié. Pour le renommer, supprimez-le puis recréez-le.",
        "Eliminado": "Supprimé", "Fecha y hora": "Date et heure",
        "Gestionar grupos": "Gérer les groupes", "Gestionar hosts": "Gérer les hôtes",
        "Green": "Vert", "Guardar": "Enregistrer", "Historial de cambios": "Historique des changements",
        "Horarios de supresión de alertas": "Horaires de suppression des alertes",
        "Hosts": "Hôtes", "Hosts en este grupo": "Hôtes dans ce groupe",
        "IP / Dirección": "IP / Adresse",
        "Identificador único, sin espacios ni caracteres especiales.":
            "Identifiant unique, sans espaces ni caractères spéciaux.",
        "J": "J", "L": "L", "Los próximos cambios que hagas desde aquí quedarán registrados automáticamente.":
            "Les prochains changements effectués ici seront enregistrés automatiquement.",
        "M": "M", "Marcá los hosts que pertenecen a este grupo.":
            "Sélectionnez les hôtes appartenant à ce groupe.",
        "Marcá los servicios que querés verificar en este host.":
            "Sélectionnez les services à vérifier sur cet hôte.",
        "Menú": "Menu", "Miembros": "Membres", "Minimizar grupo": "Réduire le groupe", "Modo claro": "Mode clair",
        "Cambiar modo de visualización de grupos": "Changer le mode d'affichage des groupes",
        "rojo expandido": "rouge déplié",
        "problemas expandidos": "problèmes dépliés",
        "todos expandidos": "tous dépliés",
        "todos minimizados": "tous repliés",
        "Modo oscuro": "Mode sombre", "No hay cambios registrados aún.": "Aucun changement enregistré.",
        "No hay grupos configurados.": "Aucun groupe configuré.",
        "No hay hosts configurados.": "Aucun hôte configuré.",
        "Nombre": "Nom", "Nombre del host": "Nom de l'hôte", "Nombre para mostrar": "Nom affiché",
        "Nuevo": "Nouveau", "Nuevo grupo": "Nouveau groupe", "Nuevo host": "Nouvel hôte",
        "Ocultar gráficos": "Masquer les graphiques", "Opciones": "Options",
        "Plugins que no aparecen arriba, separados por espacio.":
            "Plugins qui n'apparaissent pas ci-dessus, séparés par des espaces.",
        "Purple": "Violet", "Red": "Rouge", "Restaurado": "Restauré",
        "Restaurar": "Restaurer", "Restaurar esta versión": "Restaurer cette version",
        "S": "S", "Servicios a monitorear": "Services à surveiller",
        "Servicios adicionales": "Services supplémentaires", "Si ping falla,": "Si le ping échoue,",
        "Sin datos de disponibilidad": "Aucune donnée de disponibilité",
        "Tiene horarios de supresión": "A des horaires de suppression",
        "Todos": "Tous", "Una o más IPs separadas por coma. Si se deja vacío se usa el nombre.":
            "Une ou plusieurs IP séparées par des virgules. Si vide, le nom est utilisé.",
        "Usuario": "Utilisateur",
        "V": "V", "Ver": "Voir", "Ver gráficos": "Voir les graphiques", "Ver historial": "Voir l'historique",
        "Visible en el dashboard": "Visible sur le tableau de bord", "Vista compacta": "Vue compacte",
        "Salir": "Déconnexion", "Cerrar sesión": "Se déconnecter",
        "admin": "admin", "view": "lecture seule",
        "Volver": "Retour", "Volver al historial": "Retour à l'historique",
        "Volver al monitor": "Retour au moniteur", "X": "M", "Yellow": "Jaune",
        "agregar, editar o eliminar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "ajouter, modifier ou supprimer des équipements surveillés: adresse IP, services à vérifier et horaires de suppression des alertes.",
        "agregar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "ajouter des équipements surveillés: adresse IP, services à vérifier et horaires de suppression des alertes.",
        "consultar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "consulter les équipements surveillés: adresse IP, services à vérifier et horaires de suppression des alertes.",
        "cambios registrados": "changements enregistrés", "compacto": "compact",
        "desmarcar para ocultar el grupo": "décocher pour masquer le groupe",
        "ej: 192.168.0.50": "ex: 192.168.0.50", "ej: Cámaras": "ex: Caméras",
        "ej: Cámaras IP de seguridad": "ex: Caméras IP de sécurité",
        "ej: camara-jardin": "ex: camera-jardin",
        "ej: camara1 dvrcam3 ups_snmp": "ex: camara1 dvrcam3 ups_snmp",
        "ej: camaras": "ex: cameras", "ej: altas": "ex: altas", "ej: cpu": "ex: cpu",
        "grupos configurados": "groupes configurés", "hace": "il y a", "hosts": "hôtes",
        "hosts configurados": "hôtes configurés",
        "muestra solo el color de cada host, sin columnas de servicios":
            "affiche seulement la couleur de chaque hôte, sans colonnes de services",
        "más": "de plus", "no verificar el resto de los servicios": "ne pas vérifier les autres services",
        "oculto": "masqué", "opcional": "facultatif",
        "organizar los hosts en grupos para la vista del dashboard.":
            "organiser les hôtes en groupes pour la vue du tableau de bord.",
        "agregar grupos para la vista del dashboard.": "ajouter des groupes pour la vue du tableau de bord.",
        "consultar los grupos de la vista del dashboard.": "consulter les groupes de la vue du tableau de bord.",
        "recomendado para equipos en red local": "recommandé pour les équipements du réseau local",
        "rojo": "rouge", "se agregará al restaurar": "sera ajouté lors de la restauration",
        "se eliminará al restaurar": "sera supprimé lors de la restauration",
        "seleccionados": "sélectionnés", "verde": "vert",
        "¿Borrar el reconocimiento de": "Supprimer l'acquittement de",
        "¿Eliminar el grupo": "Supprimer le groupe", "¿Eliminar el host": "Supprimer l'hôte",
        "¿Qué puedo hacer aquí?": "Que puis-je faire ici ?",
        "¿Restaurar esta versión de": "Restaurer cette version de",
        "Conectividad": "Connectivité", "Red / Monitoreo": "Réseau / Surveillance",
        "Rendimiento": "Performance", "Almacenamiento": "Stockage", "Sensores": "Capteurs",
        "Cámaras": "Caméras", "Cliente": "Client", "Otros": "Autres",
        "El nombre del host es obligatorio.": "Le nom de l'hôte est obligatoire.",
        "La clave del grupo es obligatoria.": "La clé du groupe est obligatoire.",
        "Agregar usuario": "Ajouter un utilisateur",
        "Administrador": "Administrateur",
        "Cambios recientes de usuarios": "Changements récents des utilisateurs",
        "Contraseña": "Mot de passe",
        "Datos del usuario": "Données de l'utilisateur",
        "Dejar vacío para conservar la actual": "Laisser vide pour conserver l'actuelle",
        "Editar usuario": "Modifier l'utilisateur",
        "Editar usuario:": "Modifier l'utilisateur :",
        "El usuario legacy de configuración sigue activo": "L'utilisateur legacy de configuration est toujours actif",
        "El nombre de usuario es obligatorio.": "Le nom d'utilisateur est obligatoire.",
        "El nombre de usuario solo puede tener letras, números, puntos, guiones y guiones bajos.":
            "Le nom d'utilisateur ne peut contenir que des lettres, des chiffres, des points, des tirets et des underscores.",
        "Ese usuario es el administrador legacy y se administra desde spong.yaml.":
            "Cet utilisateur est l'administrateur legacy et se gère depuis spong.yaml.",
        "Gestionar usuarios": "Gérer les utilisateurs",
        "Hash de contraseña": "Hachage du mot de passe",
        "No hay usuarios adicionales configurados.": "Aucun utilisateur supplémentaire configuré.",
        "Nombre de usuario": "Nom d'utilisateur",
        "Nuevo usuario": "Nouvel utilisateur",
        "Opcional, si querés pegar un hash ya generado": "Optionnel, si vous voulez coller un hachage déjà généré",
        "Rol": "Rôle",
        "Solo agregar": "Ajout seulement",
        "Solo lectura": "Lecture seule",
        "Tenés que cargar una contraseña o un hash de contraseña.": "Vous devez fournir un mot de passe ou un hachage de mot de passe.",
        "Tiene que quedar al menos un usuario administrador.": "Au moins un utilisateur administrateur doit rester.",
        "Usá letras, números, punto, guion y guion bajo.": "Utilisez des lettres, des chiffres, des points, des tirets et des underscores.",
        "Solo letras, números, puntos, guiones y guiones bajos. Sin barras ni espacios.":
            "Uniquement des lettres, des chiffres, des points, des tirets et des underscores. Pas de barres obliques ni d'espaces.",
        "Usuario": "Utilisateur",
        "Usuarios": "Utilisateurs",
        "usuarios configurados": "utilisateurs configurés",
        "Usuarios de configuración": "Utilisateurs de configuration",
        "Ya existe un usuario con ese nombre.": "Un utilisateur avec ce nom existe déjà.",
        "¿Eliminar el usuario": "Supprimer l'utilisateur",
        "Ya existe un host con ese nombre.": "Un hôte avec ce nom existe déjà.",
        "Ya existe un grupo con esa clave.": "Un groupe avec cette clé existe déjà.",
    },
    "de": {
        "Acción": "Aktion", "Agregar el primero": "Ersten hinzufügen",
        "Agregar grupo": "Gruppe hinzufügen", "Agregar horario": "Zeitplan hinzufügen",
        "Ampliar grupo": "Gruppe erweitern",
        "Agregar host": "Host hinzufügen", "Archivo": "Datei", "Backup": "Backup",
        "Backup previo": "Backup vor Wiederherstellung", "Blue": "Blau",
        "Borrar reconocimiento": "Bestätigung löschen", "Buscar host...": "Host suchen...",
        "Básico": "Basis", "Clave": "Schlüssel", "Clave interna": "Interner Schlüssel",
        "Clear": "Klar", "Clic para verificar ahora": "Klicken, um jetzt zu prüfen",
        "Comparar períodos": "Zeiträume vergleichen", "Configuración": "Konfiguration",
        "Contenido completo del historial": "Vollständiger Verlaufsinhalt",
        "D": "S", "Datos del grupo": "Gruppendaten", "Datos del host": "Hostdaten",
        "Descripción": "Beschreibung", "Desde": "Von",
        "Diferencias respecto al estado actual": "Unterschiede zum aktuellen Zustand",
        "Dirección IP": "IP-Adresse",
        "Durante estos horarios, si el servicio está en rojo, se mostrará en blanco en el dashboard (no dispara alerta).":
            "Während dieser Zeiten wird ein roter Dienst im Dashboard weiß angezeigt (löst keinen Alarm aus).",
        "Días": "Tage", "Editado": "Bearbeitet", "Editar": "Bearbeiten",
        "Editar grupo": "Gruppe bearbeiten", "Editar grupo:": "Gruppe bearbeiten:",
        "Editar host:": "Host bearbeiten:",
        "El estado actual se guardará como backup automático antes de restaurar.":
            "Der aktuelle Zustand wird vor der Wiederherstellung automatisch als Backup gespeichert.",
        "El historial es idéntico al estado actual - no hay diferencias.":
            "Der Verlauf ist mit dem aktuellen Zustand identisch - keine Unterschiede.",
        "El historial siempre guarda el estado real.": "Der Verlauf speichert immer den echten Zustand.",
        "El nombre no se puede cambiar. Para renombrar, eliminá y volvé a crear.":
            "Der Name kann nicht geändert werden. Zum Umbenennen löschen und neu erstellen.",
        "Eliminado": "Gelöscht", "Fecha y hora": "Datum und Uhrzeit",
        "Gestionar grupos": "Gruppen verwalten", "Gestionar hosts": "Hosts verwalten",
        "Green": "Grün", "Guardar": "Speichern", "Historial de cambios": "Änderungsverlauf",
        "Horarios de supresión de alertas": "Zeitpläne zur Alarmunterdrückung",
        "Hosts": "Hosts", "Hosts en este grupo": "Hosts in dieser Gruppe",
        "IP / Dirección": "IP / Adresse",
        "Identificador único, sin espacios ni caracteres especiales.":
            "Eindeutiger Bezeichner ohne Leerzeichen oder Sonderzeichen.",
        "J": "D", "L": "M", "Los próximos cambios que hagas desde aquí quedarán registrados automáticamente.":
            "Künftige Änderungen von hier werden automatisch protokolliert.",
        "M": "D", "Marcá los hosts que pertenecen a este grupo.":
            "Wähle die Hosts aus, die zu dieser Gruppe gehören.",
        "Marcá los servicios que querés verificar en este host.":
            "Wähle die Dienste aus, die auf diesem Host geprüft werden sollen.",
        "Menú": "Menü", "Miembros": "Mitglieder", "Minimizar grupo": "Gruppe einklappen", "Modo claro": "Heller Modus",
        "Cambiar modo de visualización de grupos": "Anzeigemodus der Gruppen ändern",
        "rojo expandido": "rot ausgeklappt",
        "problemas expandidos": "Probleme ausgeklappt",
        "todos expandidos": "alle ausgeklappt",
        "todos minimizados": "alle eingeklappt",
        "Modo oscuro": "Dunkler Modus", "No hay cambios registrados aún.": "Noch keine Änderungen erfasst.",
        "No hay grupos configurados.": "Keine Gruppen konfiguriert.",
        "No hay hosts configurados.": "Keine Hosts konfiguriert.",
        "Nombre": "Name", "Nombre del host": "Hostname", "Nombre para mostrar": "Anzeigename",
        "Nuevo": "Neu", "Nuevo grupo": "Neue Gruppe", "Nuevo host": "Neuer Host",
        "Ocultar gráficos": "Graphen ausblenden", "Opciones": "Optionen",
        "Plugins que no aparecen arriba, separados por espacio.":
            "Plugins, die oben nicht erscheinen, durch Leerzeichen getrennt.",
        "Purple": "Lila", "Red": "Rot", "Restaurado": "Wiederhergestellt",
        "Restaurar": "Wiederherstellen", "Restaurar esta versión": "Diese Version wiederherstellen",
        "S": "S", "Servicios a monitorear": "Zu überwachende Dienste",
        "Servicios adicionales": "Zusätzliche Dienste", "Si ping falla,": "Wenn Ping fehlschlägt,",
        "Sin datos de disponibilidad": "Keine Verfügbarkeitsdaten",
        "Tiene horarios de supresión": "Hat Unterdrückungszeitpläne",
        "Todos": "Alle", "Una o más IPs separadas por coma. Si se deja vacío se usa el nombre.":
            "Eine oder mehrere durch Kommas getrennte IPs. Wenn leer, wird der Name verwendet.",
        "Usuario": "Benutzer",
        "V": "F", "Ver": "Ansehen", "Ver gráficos": "Graphen anzeigen", "Ver historial": "Verlauf anzeigen",
        "Visible en el dashboard": "Im Dashboard sichtbar", "Vista compacta": "Kompakte Ansicht",
        "Salir": "Abmelden", "Cerrar sesión": "Abmelden",
        "admin": "Admin", "view": "nur Lesen",
        "Volver": "Zurück", "Volver al historial": "Zurück zum Verlauf",
        "Volver al monitor": "Zurück zum Monitor", "X": "M", "Yellow": "Gelb",
        "agregar, editar o eliminar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "überwachte Geräte hinzufügen, bearbeiten oder löschen: IP-Adresse, zu prüfende Dienste und Zeitpläne zur Alarmunterdrückung.",
        "agregar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "überwachte Geräte hinzufügen: IP-Adresse, zu prüfende Dienste und Zeitpläne zur Alarmunterdrückung.",
        "consultar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "überwachte Geräte anzeigen: IP-Adresse, zu prüfende Dienste und Zeitpläne zur Alarmunterdrückung.",
        "cambios registrados": "erfasste Änderungen", "compacto": "kompakt",
        "desmarcar para ocultar el grupo": "abwählen, um die Gruppe zu verstecken",
        "ej: 192.168.0.50": "z. B. 192.168.0.50", "ej: Cámaras": "z. B. Kameras",
        "ej: Cámaras IP de seguridad": "z. B. Sicherheits-IP-Kameras",
        "ej: camara-jardin": "z. B. garten-kamera",
        "ej: camara1 dvrcam3 ups_snmp": "z. B. camara1 dvrcam3 ups_snmp",
        "ej: camaras": "z. B. kameras", "ej: altas": "z. B. altas", "ej: cpu": "z. B. cpu",
        "grupos configurados": "konfigurierte Gruppen", "hace": "vor", "hosts": "Hosts",
        "hosts configurados": "konfigurierte Hosts",
        "muestra solo el color de cada host, sin columnas de servicios":
            "zeigt nur die Farbe jedes Hosts, ohne Dienstspalten",
        "más": "mehr", "no verificar el resto de los servicios": "die restlichen Dienste nicht prüfen",
        "oculto": "versteckt", "opcional": "optional",
        "organizar los hosts en grupos para la vista del dashboard.":
            "Hosts für die Dashboard-Ansicht in Gruppen organisieren.",
        "agregar grupos para la vista del dashboard.": "Gruppen für die Dashboard-Ansicht hinzufügen.",
        "consultar los grupos de la vista del dashboard.": "Gruppen der Dashboard-Ansicht anzeigen.",
        "recomendado para equipos en red local": "empfohlen für Geräte im lokalen Netzwerk",
        "rojo": "rot", "se agregará al restaurar": "wird beim Wiederherstellen hinzugefügt",
        "se eliminará al restaurar": "wird beim Wiederherstellen entfernt",
        "seleccionados": "ausgewählt", "verde": "grün",
        "¿Borrar el reconocimiento de": "Bestätigung löschen für",
        "¿Eliminar el grupo": "Gruppe löschen", "¿Eliminar el host": "Host löschen",
        "¿Qué puedo hacer aquí?": "Was kann ich hier tun?",
        "¿Restaurar esta versión de": "Diese Version wiederherstellen von",
        "Conectividad": "Konnektivität", "Red / Monitoreo": "Netzwerk / Monitoring",
        "Rendimiento": "Leistung", "Almacenamiento": "Speicher", "Sensores": "Sensoren",
        "Cámaras": "Kameras", "Cliente": "Client", "Otros": "Andere",
        "El nombre del host es obligatorio.": "Der Hostname ist erforderlich.",
        "La clave del grupo es obligatoria.": "Der Gruppenschlüssel ist erforderlich.",
        "Agregar usuario": "Benutzer hinzufügen",
        "Administrador": "Administrator",
        "Cambios recientes de usuarios": "Kürzliche Benutzeränderungen",
        "Contraseña": "Passwort",
        "Datos del usuario": "Benutzerdaten",
        "Dejar vacío para conservar la actual": "Leer lassen, um das aktuelle Passwort zu behalten",
        "Editar usuario": "Benutzer bearbeiten",
        "Editar usuario:": "Benutzer bearbeiten:",
        "El usuario legacy de configuración sigue activo": "Der Legacy-Konfigurationsbenutzer ist noch aktiv",
        "El nombre de usuario es obligatorio.": "Der Benutzername ist erforderlich.",
        "El nombre de usuario solo puede tener letras, números, puntos, guiones y guiones bajos.":
            "Der Benutzername darf nur Buchstaben, Zahlen, Punkte, Bindestriche und Unterstriche enthalten.",
        "Ese usuario es el administrador legacy y se administra desde spong.yaml.":
            "Dieser Benutzer ist der Legacy-Administrator und wird in spong.yaml verwaltet.",
        "Gestionar usuarios": "Benutzer verwalten",
        "Hash de contraseña": "Passworthash",
        "No hay usuarios adicionales configurados.": "Keine zusätzlichen Benutzer konfiguriert.",
        "Nombre de usuario": "Benutzername",
        "Nuevo usuario": "Neuer Benutzer",
        "Opcional, si querés pegar un hash ya generado": "Optional, wenn du einen bereits generierten Hash einfügen willst",
        "Rol": "Rolle",
        "Solo agregar": "Nur hinzufügen",
        "Solo lectura": "Nur lesen",
        "Tenés que cargar una contraseña o un hash de contraseña.": "Du musst ein Passwort oder einen Passworthash angeben.",
        "Tiene que quedar al menos un usuario administrador.": "Mindestens ein Administrator muss übrig bleiben.",
        "Usá letras, números, punto, guion y guion bajo.": "Verwende Buchstaben, Zahlen, Punkte, Bindestriche und Unterstriche.",
        "Solo letras, números, puntos, guiones y guiones bajos. Sin barras ni espacios.":
            "Nur Buchstaben, Zahlen, Punkte, Bindestriche und Unterstriche. Keine Schrägstriche oder Leerzeichen.",
        "Usuario": "Benutzer",
        "Usuarios": "Benutzer",
        "usuarios configurados": "konfigurierte Benutzer",
        "Usuarios de configuración": "Konfigurationsbenutzer",
        "Ya existe un usuario con ese nombre.": "Ein Benutzer mit diesem Namen existiert bereits.",
        "¿Eliminar el usuario": "Benutzer löschen",
        "Ya existe un host con ese nombre.": "Ein Host mit diesem Namen existiert bereits.",
        "Ya existe un grupo con esa clave.": "Eine Gruppe mit diesem Schlüssel existiert bereits.",
    },
    "pt": {
        "Acción": "Ação", "Agregar el primero": "Adicionar o primeiro",
        "Agregar grupo": "Adicionar grupo", "Agregar horario": "Adicionar horário",
        "Ampliar grupo": "Expandir grupo",
        "Agregar host": "Adicionar host", "Archivo": "Arquivo", "Backup": "Backup",
        "Backup previo": "Backup prévio", "Blue": "Azul",
        "Borrar reconocimiento": "Excluir reconhecimento", "Buscar host...": "Buscar host...",
        "Básico": "Básico", "Clave": "Chave", "Clave interna": "Chave interna",
        "Clear": "Limpo", "Clic para verificar ahora": "Clique para verificar agora",
        "Comparar períodos": "Comparar períodos", "Configuración": "Configuração",
        "Contenido completo del historial": "Conteúdo completo do histórico",
        "D": "D", "Datos del grupo": "Dados do grupo", "Datos del host": "Dados do host",
        "Descripción": "Descrição", "Desde": "Desde",
        "Diferencias respecto al estado actual": "Diferenças em relação ao estado atual",
        "Dirección IP": "Endereço IP",
        "Durante estos horarios, si el servicio está en rojo, se mostrará en blanco en el dashboard (no dispara alerta).":
            "Durante esses horários, se o serviço estiver vermelho, será mostrado em branco no dashboard (não dispara alerta).",
        "Días": "Dias", "Editado": "Editado", "Editar": "Editar",
        "Editar grupo": "Editar grupo", "Editar grupo:": "Editar grupo:",
        "Editar host:": "Editar host:",
        "El estado actual se guardará como backup automático antes de restaurar.":
            "O estado atual será salvo como backup automático antes de restaurar.",
        "El historial es idéntico al estado actual - no hay diferencias.":
            "O histórico é idêntico ao estado atual - não há diferenças.",
        "El historial siempre guarda el estado real.": "O histórico sempre guarda o estado real.",
        "El nombre no se puede cambiar. Para renombrar, eliminá y volvé a crear.":
            "O nome não pode ser alterado. Para renomear, exclua e crie novamente.",
        "Eliminado": "Excluído", "Fecha y hora": "Data e hora",
        "Gestionar grupos": "Gerenciar grupos", "Gestionar hosts": "Gerenciar hosts",
        "Green": "Verde", "Guardar": "Salvar", "Historial de cambios": "Histórico de alterações",
        "Horarios de supresión de alertas": "Horários de supressão de alertas",
        "Hosts": "Hosts", "Hosts en este grupo": "Hosts neste grupo",
        "IP / Dirección": "IP / Endereço",
        "Identificador único, sin espacios ni caracteres especiales.":
            "Identificador único, sem espaços nem caracteres especiais.",
        "J": "Q", "L": "S", "Los próximos cambios que hagas desde aquí quedarán registrados automáticamente.":
            "As próximas alterações feitas aqui serão registradas automaticamente.",
        "M": "T", "Marcá los hosts que pertenecen a este grupo.":
            "Marque os hosts que pertencem a este grupo.",
        "Marcá los servicios que querés verificar en este host.":
            "Marque os serviços que quer verificar neste host.",
        "Menú": "Menu", "Miembros": "Membros", "Minimizar grupo": "Recolher grupo", "Modo claro": "Modo claro",
        "Cambiar modo de visualización de grupos": "Mudar modo de exibição dos grupos",
        "rojo expandido": "vermelho expandido",
        "problemas expandidos": "problemas expandidos",
        "todos expandidos": "todos expandidos",
        "todos minimizados": "todos recolhidos",
        "Modo oscuro": "Modo escuro", "No hay cambios registrados aún.": "Ainda não há alterações registradas.",
        "No hay grupos configurados.": "Nenhum grupo configurado.",
        "No hay hosts configurados.": "Nenhum host configurado.",
        "Nombre": "Nome", "Nombre del host": "Nome do host", "Nombre para mostrar": "Nome exibido",
        "Nuevo": "Novo", "Nuevo grupo": "Novo grupo", "Nuevo host": "Novo host",
        "Ocultar gráficos": "Ocultar gráficos", "Opciones": "Opções",
        "Plugins que no aparecen arriba, separados por espacio.":
            "Plugins que não aparecem acima, separados por espaço.",
        "Purple": "Roxo", "Red": "Vermelho", "Restaurado": "Restaurado",
        "Restaurar": "Restaurar", "Restaurar esta versión": "Restaurar esta versão",
        "S": "S", "Servicios a monitorear": "Serviços a monitorar",
        "Servicios adicionales": "Serviços adicionais", "Si ping falla,": "Se o ping falhar,",
        "Sin datos de disponibilidad": "Sem dados de disponibilidade",
        "Tiene horarios de supresión": "Tem horários de supressão",
        "Todos": "Todos", "Una o más IPs separadas por coma. Si se deja vacío se usa el nombre.":
            "Um ou mais IPs separados por vírgula. Se ficar vazio, usa o nome.",
        "Usuario": "Usuário",
        "V": "S", "Ver": "Ver", "Ver gráficos": "Ver gráficos", "Ver historial": "Ver histórico",
        "Visible en el dashboard": "Visível no dashboard", "Vista compacta": "Vista compacta",
        "Salir": "Sair", "Cerrar sesión": "Encerrar sessão",
        "admin": "admin", "view": "somente leitura",
        "Volver": "Voltar", "Volver al historial": "Voltar ao histórico",
        "Volver al monitor": "Voltar ao monitor", "X": "Q", "Yellow": "Amarelo",
        "agregar, editar o eliminar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "adicionar, editar ou excluir equipamentos monitorados: endereço IP, serviços a verificar e horários de supressão de alertas.",
        "agregar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "adicionar equipamentos monitorados: endereço IP, serviços a verificar e horários de supressão de alertas.",
        "consultar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "consultar equipamentos monitorados: endereço IP, serviços a verificar e horários de supressão de alertas.",
        "cambios registrados": "alterações registradas", "compacto": "compacto",
        "desmarcar para ocultar el grupo": "desmarque para ocultar o grupo",
        "ej: 192.168.0.50": "ex: 192.168.0.50", "ej: Cámaras": "ex: Câmeras",
        "ej: Cámaras IP de seguridad": "ex: Câmeras IP de segurança",
        "ej: camara-jardin": "ex: camera-jardim",
        "ej: camara1 dvrcam3 ups_snmp": "ex: camara1 dvrcam3 ups_snmp",
        "ej: camaras": "ex: cameras", "ej: altas": "ex: altas", "ej: cpu": "ex: cpu",
        "grupos configurados": "grupos configurados", "hace": "há", "hosts": "hosts",
        "hosts configurados": "hosts configurados",
        "muestra solo el color de cada host, sin columnas de servicios":
            "mostra apenas a cor de cada host, sem colunas de serviços",
        "más": "mais", "no verificar el resto de los servicios": "não verificar o restante dos serviços",
        "oculto": "oculto", "opcional": "opcional",
        "organizar los hosts en grupos para la vista del dashboard.":
            "organizar os hosts em grupos para a visão do dashboard.",
        "agregar grupos para la vista del dashboard.": "adicionar grupos para a visão do dashboard.",
        "consultar los grupos de la vista del dashboard.": "consultar os grupos da visão do dashboard.",
        "recomendado para equipos en red local": "recomendado para equipamentos na rede local",
        "rojo": "vermelho", "se agregará al restaurar": "será adicionado ao restaurar",
        "se eliminará al restaurar": "será removido ao restaurar",
        "seleccionados": "selecionados", "verde": "verde",
        "¿Borrar el reconocimiento de": "Excluir reconhecimento de",
        "¿Eliminar el grupo": "Excluir grupo", "¿Eliminar el host": "Excluir host",
        "¿Qué puedo hacer aquí?": "O que posso fazer aqui?",
        "¿Restaurar esta versión de": "Restaurar esta versão de",
        "Conectividad": "Conectividade", "Red / Monitoreo": "Rede / Monitoramento",
        "Rendimiento": "Desempenho", "Almacenamiento": "Armazenamento", "Sensores": "Sensores",
        "Cámaras": "Câmeras", "Cliente": "Cliente", "Otros": "Outros",
        "El nombre del host es obligatorio.": "O nome do host é obrigatório.",
        "La clave del grupo es obligatoria.": "A chave do grupo é obrigatória.",
        "Agregar usuario": "Adicionar usuário",
        "Administrador": "Administrador",
        "Cambios recientes de usuarios": "Alterações recentes de usuários",
        "Contraseña": "Senha",
        "Datos del usuario": "Dados do usuário",
        "Dejar vacío para conservar la actual": "Deixe vazio para manter a atual",
        "Editar usuario": "Editar usuário",
        "Editar usuario:": "Editar usuário:",
        "El usuario legacy de configuración sigue activo": "O usuário legado de configuração ainda está ativo",
        "El nombre de usuario es obligatorio.": "O nome de usuário é obrigatório.",
        "El nombre de usuario solo puede tener letras, números, puntos, guiones y guiones bajos.":
            "O nome de usuário pode conter apenas letras, números, pontos, hífens e sublinhados.",
        "Ese usuario es el administrador legacy y se administra desde spong.yaml.":
            "Esse usuário é o administrador legado e é gerenciado pelo spong.yaml.",
        "Gestionar usuarios": "Gerenciar usuários",
        "Hash de contraseña": "Hash da senha",
        "No hay usuarios adicionales configurados.": "Não há usuários adicionais configurados.",
        "Nombre de usuario": "Nome de usuário",
        "Nuevo usuario": "Novo usuário",
        "Opcional, si querés pegar un hash ya generado": "Opcional, se você quiser colar um hash já gerado",
        "Rol": "Função",
        "Solo agregar": "Somente adicionar",
        "Solo lectura": "Somente leitura",
        "Tenés que cargar una contraseña o un hash de contraseña.": "Você precisa informar uma senha ou um hash de senha.",
        "Tiene que quedar al menos un usuario administrador.": "Ao menos um usuário administrador precisa permanecer.",
        "Usá letras, números, punto, guion y guion bajo.": "Use letras, números, ponto, hífen e sublinhado.",
        "Solo letras, números, puntos, guiones y guiones bajos. Sin barras ni espacios.":
            "Apenas letras, números, pontos, hífens e sublinhados. Sem barras nem espaços.",
        "Usuario": "Usuário",
        "Usuarios": "Usuários",
        "usuarios configurados": "usuários configurados",
        "Usuarios de configuración": "Usuários de configuração",
        "Ya existe un usuario con ese nombre.": "Já existe um usuário com esse nome.",
        "¿Eliminar el usuario": "Excluir usuário",
        "Ya existe un host con ese nombre.": "Já existe um host com esse nome.",
        "Ya existe un grupo con esa clave.": "Já existe um grupo com essa chave.",
    },
    "zh": {
        "Acción": "操作", "Agregar el primero": "添加第一个", "Agregar grupo": "添加组",
        "Agregar horario": "添加时间段", "Agregar host": "添加主机", "Ampliar grupo": "展开组", "Archivo": "文件",
        "Backup": "备份", "Backup previo": "恢复前备份", "Blue": "蓝色",
        "Borrar reconocimiento": "删除确认", "Buscar host...": "搜索主机...",
        "Básico": "基础", "Clave": "键", "Clave interna": "内部键", "Clear": "清除",
        "Clic para verificar ahora": "点击立即检查", "Comparar períodos": "比较周期",
        "Configuración": "配置", "Contenido completo del historial": "完整历史内容",
        "D": "日", "Datos del grupo": "组数据", "Datos del host": "主机数据",
        "Descripción": "描述", "Desde": "从",
        "Diferencias respecto al estado actual": "与当前状态的差异",
        "Dirección IP": "IP 地址",
        "Durante estos horarios, si el servicio está en rojo, se mostrará en blanco en el dashboard (no dispara alerta).":
            "在这些时间段内，如果服务为红色，仪表板上会显示为白色（不会触发告警）。",
        "Días": "天", "Editado": "已编辑", "Editar": "编辑", "Editar grupo": "编辑组",
        "Editar grupo:": "编辑组：", "Editar host:": "编辑主机：",
        "El estado actual se guardará como backup automático antes de restaurar.":
            "恢复前会自动保存当前状态备份。",
        "El historial es idéntico al estado actual - no hay diferencias.": "历史与当前状态相同，没有差异。",
        "El historial siempre guarda el estado real.": "历史始终保存真实状态。",
        "El nombre no se puede cambiar. Para renombrar, eliminá y volvé a crear.":
            "名称不能修改。要重命名，请删除后重新创建。",
        "Eliminado": "已删除", "Fecha y hora": "日期和时间",
        "Gestionar grupos": "管理组", "Gestionar hosts": "管理主机", "Green": "绿色",
        "Guardar": "保存", "Historial de cambios": "变更历史",
        "Horarios de supresión de alertas": "告警抑制时间段",
        "Hosts": "主机", "Hosts en este grupo": "此组中的主机", "IP / Dirección": "IP / 地址",
        "Identificador único, sin espacios ni caracteres especiales.": "唯一标识符，不含空格或特殊字符。",
        "J": "四", "L": "一", "Los próximos cambios que hagas desde aquí quedarán registrados automáticamente.":
            "之后在这里所做的更改会自动记录。",
        "M": "二", "Marcá los hosts que pertenecen a este grupo.": "选择属于此组的主机。",
        "Marcá los servicios que querés verificar en este host.": "选择要在此主机上检查的服务。",
        "Menú": "菜单", "Miembros": "成员", "Minimizar grupo": "折叠组", "Modo claro": "浅色模式", "Modo oscuro": "深色模式",
        "Cambiar modo de visualización de grupos": "切换组显示模式",
        "rojo expandido": "仅展开红色",
        "problemas expandidos": "展开问题组",
        "todos expandidos": "全部展开",
        "todos minimizados": "全部折叠",
        "No hay cambios registrados aún.": "尚无记录的变更。", "No hay grupos configurados.": "未配置组。",
        "No hay hosts configurados.": "未配置主机。", "Nombre": "名称", "Nombre del host": "主机名",
        "Nombre para mostrar": "显示名称", "Nuevo": "新建", "Nuevo grupo": "新组",
        "Nuevo host": "新主机", "Ocultar gráficos": "隐藏图表", "Opciones": "选项",
        "Plugins que no aparecen arriba, separados por espacio.": "上方未列出的插件，用空格分隔。",
        "Purple": "紫色", "Red": "红色", "Restaurado": "已恢复", "Restaurar": "恢复",
        "Restaurar esta versión": "恢复此版本", "S": "六", "Servicios a monitorear": "要监控的服务",
        "Servicios adicionales": "附加服务", "Si ping falla,": "如果 ping 失败，",
        "Sin datos de disponibilidad": "无可用性数据", "Tiene horarios de supresión": "有抑制时间段",
        "Todos": "全部", "Una o más IPs separadas por coma. Si se deja vacío se usa el nombre.":
            "一个或多个用逗号分隔的 IP。留空则使用名称。",
        "Usuario": "用户",
        "V": "五", "Ver": "查看", "Ver gráficos": "显示图表", "Ver historial": "查看历史",
        "Visible en el dashboard": "在仪表板中可见", "Vista compacta": "紧凑视图",
        "Salir": "退出", "Cerrar sesión": "退出登录",
        "admin": "管理员", "view": "只读",
        "Volver": "返回", "Volver al historial": "返回历史", "Volver al monitor": "返回监控",
        "X": "三", "Yellow": "黄色",
        "agregar, editar o eliminar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "添加、编辑或删除受监控设备：IP 地址、要检查的服务和告警抑制时间段。",
        "agregar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "添加受监控设备：IP 地址、要检查的服务和告警抑制时间段。",
        "consultar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "查看受监控设备：IP 地址、要检查的服务和告警抑制时间段。",
        "cambios registrados": "已记录变更", "compacto": "紧凑", "desmarcar para ocultar el grupo": "取消选中以隐藏该组",
        "ej: 192.168.0.50": "例：192.168.0.50", "ej: Cámaras": "例：摄像头",
        "ej: Cámaras IP de seguridad": "例：安全 IP 摄像头",
        "ej: camara-jardin": "例：garden-camera",
        "ej: camara1 dvrcam3 ups_snmp": "例：camara1 dvrcam3 ups_snmp",
        "ej: camaras": "例：cameras", "ej: altas": "例：altas", "ej: cpu": "例：cpu",
        "grupos configurados": "已配置组", "hace": "前", "hosts": "主机",
        "hosts configurados": "已配置主机",
        "muestra solo el color de cada host, sin columnas de servicios": "只显示每个主机的颜色，不显示服务列",
        "más": "更多", "no verificar el resto de los servicios": "不检查其余服务",
        "oculto": "隐藏", "opcional": "可选",
        "organizar los hosts en grupos para la vista del dashboard.": "将主机分组用于仪表板视图。",
        "agregar grupos para la vista del dashboard.": "为仪表板视图添加组。",
        "consultar los grupos de la vista del dashboard.": "查看仪表板视图中的组。",
        "recomendado para equipos en red local": "建议用于本地网络设备",
        "rojo": "红色", "se agregará al restaurar": "恢复时将添加",
        "se eliminará al restaurar": "恢复时将删除", "seleccionados": "已选择", "verde": "绿色",
        "¿Borrar el reconocimiento de": "删除确认：", "¿Eliminar el grupo": "删除组",
        "¿Eliminar el host": "删除主机", "¿Qué puedo hacer aquí?": "这里可以做什么？",
        "¿Restaurar esta versión de": "恢复此版本：",
        "Conectividad": "连接", "Red / Monitoreo": "网络 / 监控", "Rendimiento": "性能",
        "Almacenamiento": "存储", "Sensores": "传感器", "Cámaras": "摄像头",
        "Cliente": "客户端", "Otros": "其他",
        "El nombre del host es obligatorio.": "主机名为必填项。",
        "La clave del grupo es obligatoria.": "组键为必填项。",
        "Agregar usuario": "添加用户",
        "Administrador": "管理员",
        "Cambios recientes de usuarios": "最近的用户更改",
        "Contraseña": "密码",
        "Datos del usuario": "用户数据",
        "Dejar vacío para conservar la actual": "留空则保留当前值",
        "Editar usuario": "编辑用户",
        "Editar usuario:": "编辑用户：",
        "El usuario legacy de configuración sigue activo": "旧版配置用户仍然有效",
        "El nombre de usuario es obligatorio.": "用户名为必填项。",
        "El nombre de usuario solo puede tener letras, números, puntos, guiones y guiones bajos.":
            "用户名只能包含字母、数字、点、连字符和下划线。",
        "Ese usuario es el administrador legacy y se administra desde spong.yaml.":
            "该用户是旧版管理员，受 spong.yaml 管理。",
        "Gestionar usuarios": "管理用户",
        "Hash de contraseña": "密码哈希",
        "No hay usuarios adicionales configurados.": "未配置其他用户。",
        "Nombre de usuario": "用户名",
        "Nuevo usuario": "新用户",
        "Opcional, si querés pegar un hash ya generado": "可选，如果你想粘贴已生成的哈希",
        "Rol": "角色",
        "Solo agregar": "仅添加",
        "Solo lectura": "只读",
        "Tenés que cargar una contraseña o un hash de contraseña.": "你需要提供密码或密码哈希。",
        "Tiene que quedar al menos un usuario administrador.": "至少要保留一个管理员用户。",
        "Usá letras, números, punto, guion y guion bajo.": "使用字母、数字、点、连字符和下划线。",
        "Solo letras, números, puntos, guiones y guiones bajos. Sin barras ni espacios.":
            "仅限字母、数字、点、连字符和下划线。不允许斜杠或空格。",
        "Usuario": "用户",
        "Usuarios": "用户",
        "usuarios configurados": "已配置用户",
        "Usuarios de configuración": "配置用户",
        "Ya existe un usuario con ese nombre.": "已存在同名用户。",
        "¿Eliminar el usuario": "删除用户",
        "Ya existe un host con ese nombre.": "已存在同名主机。",
        "Ya existe un grupo con esa clave.": "已存在使用该键的组。",
    },
    "ru": {
        "Acción": "Действие", "Agregar el primero": "Добавить первый",
        "Agregar grupo": "Добавить группу", "Agregar horario": "Добавить расписание",
        "Ampliar grupo": "Развернуть группу",
        "Agregar host": "Добавить хост", "Archivo": "Файл", "Backup": "Резервная копия",
        "Backup previo": "Резервная копия перед восстановлением", "Blue": "Синий",
        "Borrar reconocimiento": "Удалить подтверждение", "Buscar host...": "Искать хост...",
        "Básico": "Базовое", "Clave": "Ключ", "Clave interna": "Внутренний ключ",
        "Clear": "Чистый", "Clic para verificar ahora": "Нажмите, чтобы проверить сейчас",
        "Comparar períodos": "Сравнить периоды", "Configuración": "Конфигурация",
        "Contenido completo del historial": "Полное содержимое истории",
        "D": "В", "Datos del grupo": "Данные группы", "Datos del host": "Данные хоста",
        "Descripción": "Описание", "Desde": "С",
        "Diferencias respecto al estado actual": "Отличия от текущего состояния",
        "Dirección IP": "IP-адрес",
        "Durante estos horarios, si el servicio está en rojo, se mostrará en blanco en el dashboard (no dispara alerta).":
            "В это время красный сервис будет показан белым на панели (без отправки тревоги).",
        "Días": "Дни", "Editado": "Изменено", "Editar": "Изменить",
        "Editar grupo": "Изменить группу", "Editar grupo:": "Изменить группу:",
        "Editar host:": "Изменить хост:",
        "El estado actual se guardará como backup automático antes de restaurar.":
            "Текущее состояние будет автоматически сохранено перед восстановлением.",
        "El historial es idéntico al estado actual - no hay diferencias.":
            "История совпадает с текущим состоянием - различий нет.",
        "El historial siempre guarda el estado real.": "История всегда сохраняет реальное состояние.",
        "El nombre no se puede cambiar. Para renombrar, eliminá y volvé a crear.":
            "Имя нельзя изменить. Чтобы переименовать, удалите и создайте заново.",
        "Eliminado": "Удалено", "Fecha y hora": "Дата и время",
        "Gestionar grupos": "Управлять группами", "Gestionar hosts": "Управлять хостами",
        "Green": "Зелёный", "Guardar": "Сохранить", "Historial de cambios": "История изменений",
        "Horarios de supresión de alertas": "Расписания подавления тревог",
        "Hosts": "Хосты", "Hosts en este grupo": "Хосты в этой группе",
        "IP / Dirección": "IP / Адрес",
        "Identificador único, sin espacios ni caracteres especiales.":
            "Уникальный идентификатор без пробелов и специальных символов.",
        "J": "Ч", "L": "П", "Los próximos cambios que hagas desde aquí quedarán registrados automáticamente.":
            "Следующие изменения отсюда будут записываться автоматически.",
        "M": "В", "Marcá los hosts que pertenecen a este grupo.":
            "Выберите хосты, входящие в эту группу.",
        "Marcá los servicios que querés verificar en este host.":
            "Выберите сервисы, которые нужно проверять на этом хосте.",
        "Menú": "Меню", "Miembros": "Участники", "Minimizar grupo": "Свернуть группу", "Modo claro": "Светлая тема",
        "Cambiar modo de visualización de grupos": "Изменить режим отображения групп",
        "rojo expandido": "только красные развернуты",
        "problemas expandidos": "проблемы развернуты",
        "todos expandidos": "все развернуты",
        "todos minimizados": "все свернуты",
        "Modo oscuro": "Тёмная тема", "No hay cambios registrados aún.": "Изменений пока нет.",
        "No hay grupos configurados.": "Группы не настроены.",
        "No hay hosts configurados.": "Хосты не настроены.",
        "Nombre": "Имя", "Nombre del host": "Имя хоста", "Nombre para mostrar": "Отображаемое имя",
        "Nuevo": "Новый", "Nuevo grupo": "Новая группа", "Nuevo host": "Новый хост",
        "Ocultar gráficos": "Скрыть графики", "Opciones": "Параметры",
        "Plugins que no aparecen arriba, separados por espacio.":
            "Плагины, которых нет выше, через пробел.",
        "Purple": "Фиолетовый", "Red": "Красный", "Restaurado": "Восстановлено",
        "Restaurar": "Восстановить", "Restaurar esta versión": "Восстановить эту версию",
        "S": "С", "Servicios a monitorear": "Сервисы для мониторинга",
        "Servicios adicionales": "Дополнительные сервисы", "Si ping falla,": "Если ping не проходит,",
        "Sin datos de disponibilidad": "Нет данных доступности",
        "Tiene horarios de supresión": "Есть расписания подавления",
        "Todos": "Все", "Una o más IPs separadas por coma. Si se deja vacío se usa el nombre.":
            "Один или несколько IP через запятую. Если пусто, используется имя.",
        "Usuario": "Пользователь",
        "V": "П", "Ver": "Открыть", "Ver gráficos": "Показать графики",
        "Ver historial": "Посмотреть историю", "Visible en el dashboard": "Видно на панели",
        "Salir": "Выйти", "Cerrar sesión": "Выйти из системы",
        "admin": "администратор", "view": "только просмотр",
        "Vista compacta": "Компактный вид", "Volver": "Назад",
        "Volver al historial": "Назад к истории", "Volver al monitor": "Назад к монитору",
        "X": "С", "Yellow": "Жёлтый",
        "agregar, editar o eliminar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "добавлять, изменять или удалять наблюдаемые устройства: IP-адрес, сервисы для проверки и расписания подавления тревог.",
        "agregar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "добавлять наблюдаемые устройства: IP-адрес, сервисы для проверки и расписания подавления тревог.",
        "consultar equipos monitoreados: dirección IP, servicios a verificar y horarios de supresión de alertas.":
            "просматривать наблюдаемые устройства: IP-адрес, сервисы для проверки и расписания подавления тревог.",
        "cambios registrados": "записанные изменения", "compacto": "компактно",
        "desmarcar para ocultar el grupo": "снимите флажок, чтобы скрыть группу",
        "ej: 192.168.0.50": "напр. 192.168.0.50", "ej: Cámaras": "напр. Камеры",
        "ej: Cámaras IP de seguridad": "напр. IP-камеры безопасности",
        "ej: camara-jardin": "напр. garden-camera",
        "ej: camara1 dvrcam3 ups_snmp": "напр. camara1 dvrcam3 ups_snmp",
        "ej: camaras": "напр. cameras", "ej: altas": "напр. altas", "ej: cpu": "напр. cpu",
        "grupos configurados": "настроенные группы", "hace": "назад", "hosts": "хостов",
        "hosts configurados": "настроенные хосты",
        "muestra solo el color de cada host, sin columnas de servicios":
            "показывает только цвет каждого хоста, без колонок сервисов",
        "más": "ещё", "no verificar el resto de los servicios": "не проверять остальные сервисы",
        "oculto": "скрыто", "opcional": "необязательно",
        "organizar los hosts en grupos para la vista del dashboard.":
            "организовать хосты по группам для панели.",
        "agregar grupos para la vista del dashboard.": "добавлять группы для панели.",
        "consultar los grupos de la vista del dashboard.": "просматривать группы панели.",
        "recomendado para equipos en red local": "рекомендуется для устройств локальной сети",
        "rojo": "красный", "se agregará al restaurar": "будет добавлено при восстановлении",
        "se eliminará al restaurar": "будет удалено при восстановлении",
        "seleccionados": "выбрано", "verde": "зелёный",
        "¿Borrar el reconocimiento de": "Удалить подтверждение для",
        "¿Eliminar el grupo": "Удалить группу", "¿Eliminar el host": "Удалить хост",
        "¿Qué puedo hacer aquí?": "Что можно сделать здесь?",
        "¿Restaurar esta versión de": "Восстановить эту версию",
        "Conectividad": "Связность", "Red / Monitoreo": "Сеть / Мониторинг",
        "Rendimiento": "Производительность", "Almacenamiento": "Хранилище",
        "Sensores": "Датчики", "Cámaras": "Камеры", "Cliente": "Клиент",
        "Otros": "Прочее", "El nombre del host es obligatorio.": "Имя хоста обязательно.",
        "La clave del grupo es obligatoria.": "Ключ группы обязателен.",
        "Agregar usuario": "Добавить пользователя",
        "Administrador": "Администратор",
        "Cambios recientes de usuarios": "Недавние изменения пользователей",
        "Contraseña": "Пароль",
        "Datos del usuario": "Данные пользователя",
        "Dejar vacío para conservar la actual": "Оставьте пустым, чтобы сохранить текущий",
        "Editar usuario": "Изменить пользователя",
        "Editar usuario:": "Изменить пользователя:",
        "El usuario legacy de configuración sigue activo": "Устаревший пользователь конфигурации все еще активен",
        "El nombre de usuario es obligatorio.": "Имя пользователя обязательно.",
        "El nombre de usuario solo puede tener letras, números, puntos, guiones y guiones bajos.":
            "Имя пользователя может содержать только буквы, цифры, точки, дефисы и подчёркивания.",
        "Ese usuario es el administrador legacy y se administra desde spong.yaml.":
            "Этот пользователь является устаревшим администратором и управляется из spong.yaml.",
        "Gestionar usuarios": "Управлять пользователями",
        "Hash de contraseña": "Хеш пароля",
        "No hay usuarios adicionales configurados.": "Дополнительные пользователи не настроены.",
        "Nombre de usuario": "Имя пользователя",
        "Nuevo usuario": "Новый пользователь",
        "Opcional, si querés pegar un hash ya generado": "Необязательно, если хотите вставить уже сгенерированный хеш",
        "Rol": "Роль",
        "Solo agregar": "Только добавление",
        "Solo lectura": "Только чтение",
        "Tenés que cargar una contraseña o un hash de contraseña.": "Нужно указать пароль или хеш пароля.",
        "Tiene que quedar al menos un usuario administrador.": "Должен остаться как минимум один администратор.",
        "Usá letras, números, punto, guion y guion bajo.": "Используйте буквы, цифры, точки, дефисы и подчёркивания.",
        "Solo letras, números, puntos, guiones y guiones bajos. Sin barras ni espacios.":
            "Только буквы, цифры, точки, дефисы и подчёркивания. Без косых чёрт и пробелов.",
        "Usuario": "Пользователь",
        "Usuarios": "Пользователи",
        "usuarios configurados": "настроенные пользователи",
        "Usuarios de configuración": "Пользователи конфигурации",
        "Ya existe un usuario con ese nombre.": "Пользователь с таким именем уже существует.",
        "¿Eliminar el usuario": "Удалить пользователя",
        "Ya existe un host con ese nombre.": "Хост с таким именем уже существует.",
        "Ya existe un grupo con esa clave.": "Группа с таким ключом уже существует.",
    },
}

for _lang, _entries in _EXTRA_TRANSLATIONS.items():
    _TRANSLATIONS.setdefault(_lang, {}).update(_entries)
    _TRANSLATIONS[_lang].setdefault("UPS", "UPS")


def _make_translator(lang: str):
    d = _TRANSLATIONS.get(lang, {})
    def t(s: str) -> str:
        return d.get(s, s)
    return t


def _make_time_ago(lang: str):
    def f(seconds: int) -> str:
        seconds = int(seconds)
        if lang == "en":
            if seconds < 60:    return f"{seconds}s ago"
            if seconds < 3600:  return f"{seconds // 60}m ago"
            if seconds < 86400: return f"{seconds // 3600}h ago"
            return f"{seconds // 86400}d ago"
        elif lang == "fr":
            if seconds < 60:    return f"il y a {seconds}s"
            if seconds < 3600:  return f"il y a {seconds // 60}m"
            if seconds < 86400: return f"il y a {seconds // 3600}h"
            return f"il y a {seconds // 86400}j"
        elif lang == "de":
            if seconds < 60:    return f"vor {seconds}s"
            if seconds < 3600:  return f"vor {seconds // 60}m"
            if seconds < 86400: return f"vor {seconds // 3600}h"
            return f"vor {seconds // 86400}T"
        elif lang == "pt":
            if seconds < 60:    return f"há {seconds}s"
            if seconds < 3600:  return f"há {seconds // 60}m"
            if seconds < 86400: return f"há {seconds // 3600}h"
            return f"há {seconds // 86400}d"
        elif lang == "zh":
            if seconds < 60:    return f"{seconds}秒前"
            if seconds < 3600:  return f"{seconds // 60}分钟前"
            if seconds < 86400: return f"{seconds // 3600}小时前"
            return f"{seconds // 86400}天前"
        elif lang == "ru":
            if seconds < 60:    return f"{seconds}с назад"
            if seconds < 3600:  return f"{seconds // 60}м назад"
            if seconds < 86400: return f"{seconds // 3600}ч назад"
            return f"{seconds // 86400}д назад"
        else:  # es
            if seconds < 60:    return f"hace {seconds}s"
            if seconds < 3600:  return f"hace {seconds // 60}m"
            if seconds < 86400: return f"hace {seconds // 3600}h"
            return f"hace {seconds // 86400}d"
    return f


def _fmt_duration(seconds: int) -> str:
    seconds = int(seconds)
    if seconds < 60:    return f"{seconds}s"
    if seconds < 3600:  return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400: return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"


def _auto_refresh_seconds() -> int:
    endpoint = request.endpoint or ''
    refreshable = {'index', 'problems', 'acks_page', 'uptime_page', 'history_page'}
    if endpoint not in refreshable:
        return 0
    try:
        seconds = int(config.get('web.auto_refresh_seconds', 300))
    except (TypeError, ValueError):
        seconds = 300
    return max(0, seconds)


app.jinja_env.globals["fmt_duration"] = _fmt_duration


@app.context_processor
def inject_i18n():
    lang = request.cookies.get("lang", "es")
    if lang not in _SUPPORTED_LANGS:
        lang = "es"
    theme = request.cookies.get("theme", "dark")
    if theme not in ("light", "dark"):
        theme = "dark"
    return {
        "_": _make_translator(lang),
        "current_lang": lang,
        "time_ago": _make_time_ago(lang),
        "lang_meta": _LANG_META,
        "current_theme": theme,
        "auto_refresh_seconds": _auto_refresh_seconds(),
    }


@app.context_processor
def inject_spong_auth():
    role = getattr(g, "spong_role", "") or ""
    return {
        "spong_user": getattr(g, "spong_user", "") or "",
        "spong_role": role,
        "spong_can_admin": role == "admin",
        "spong_can_ack": role in _SPONG_ACK_ROLES,
        "spong_auth_enabled": bool(_spong_user_entries()),
        "sgt_enabled": sgt_link.enabled(),
    }


@app.template_filter("strftime")
def _strftime(ts, fmt="%Y-%m-%d %H:%M"):
    try:
        return time.strftime(fmt, time.localtime(float(ts)))
    except Exception:
        return "?"


@app.context_processor
def inject_sidebar():
    _, sidebar = _get_dashboard_snapshot()
    return {"sidebar_groups": sidebar}


# ---- Routes ----

@app.route("/")
def index():
    group_data, _ = _get_dashboard_snapshot()
    return render_template("index.html", groups=group_data)


def _apply_ack_colors(services: dict, acks: list) -> None:
    """Mark acked non-green services as blue, in-place."""
    for svc_name, svc in services.items():
        if svc.color not in ("green", "blue"):
            if any(ack.covers(svc_name) for ack in acks):
                svc.color = "blue"


def _apply_schedule_suppression(hostname: str, services: dict) -> None:
    """Suppress red/yellow to clear when inside a configured time window, in-place."""
    for svc_name, svc in services.items():
        if svc.color in ("red", "yellow") and config.is_suppressed(hostname, svc_name):
            svc.color = "clear"


@app.route("/host/<hostname>")
def host_detail(hostname):
    host_cfg = config.get_host(hostname)
    config_host_edit_available = bool(host_cfg) and config_permission_available("edit")
    services = _load_visible_services(hostname)
    acks = database.load_acks(hostname)
    _apply_ack_colors(services, acks)
    _apply_schedule_suppression(hostname, services)
    history = database.load_history(hostname, max_age_days=7, status_changes_only=True)
    # Order services by hosts.yaml order, then any extras alphabetically
    cfg_order = [s for s, _ in config.host_services(hostname)]
    sorted_services = sorted(
        services.items(),
        key=lambda kv: (cfg_order.index(kv[0]) if kv[0] in cfg_order else len(cfg_order), kv[0])
    )
    return render_template(
        "host.html",
        hostname=hostname,
        host_cfg=host_cfg,
        config_host_edit_available=config_host_edit_available,
        services=sorted_services,
        acks=acks,
        history=sorted(history, key=lambda e: e.timestamp, reverse=True),
    )


@app.route("/service/<hostname>/<service>")
def service_detail(hostname, service):
    svc = database.load_service(hostname, service) if _is_visible_service(hostname, service) else None
    acks = database.load_acks(hostname)
    is_acked = database.is_acknowledged(hostname, service)
    service_ack = next((a for a in acks if a.covers(service)), None)
    if svc and is_acked and svc.color not in ("green", "blue"):
        svc.color = "blue"
    return render_template(
        "service.html",
        hostname=hostname,
        service=service,
        svc=svc,
        acks=acks,
        is_acked=is_acked,
        service_ack=service_ack,
    )


@app.route("/problems")
def problems():
    hosts = config.get_hosts()
    issues = []
    for hostname in hosts:
        services = _load_visible_services(hostname)
        acks = database.load_acks(hostname)
        _apply_ack_colors(services, acks)
        _apply_schedule_suppression(hostname, services)
        for svc_name, svc in sorted(services.items()):
            if svc.color in ("red", "yellow", "purple"):
                issues.append({
                    "host": hostname,
                    "service": svc_name,
                    "color": svc.color,
                    "summary": svc.summary,
                    "duration": svc.duration,
                    "start_time": svc.start_time,
                    "report_time": svc.report_time,
                    "lag": time.time() - svc.report_time,
                })
    issues.sort(key=lambda x: (x["color"] != "red", x["color"] != "yellow"))
    links = sgt_link.links_for_issues(issues) if sgt_link.enabled() else {}
    for issue in issues:
        issue["sgt_link"] = links.get(f"{issue['host']}\x00{issue['service']}")
    return render_template(
        "problems.html",
        issues=issues,
        sgt_ok=request.args.get("sgt_ok", ""),
        sgt_err=request.args.get("sgt_err", ""),
    )


@app.route("/acks")
def acks_page():
    hosts = config.get_hosts()
    all_acks = []
    for hostname in hosts:
        svcs = _load_visible_services(hostname)
        for ack in database.load_acks(hostname):
            services_status = [(sn, sv) for sn, sv in svcs.items() if ack.covers(sn)]
            all_acks.append({
                "ack": ack,
                "services_status": services_status,
            })
    all_acks.sort(key=lambda x: x["ack"].end_time)
    return render_template("acks.html", all_acks=all_acks)


def _parse_duration(value: str) -> float:
    """Parse duration like +4h, +2d, +1m, +1a, never into seconds. Returns 0 for never."""
    import re
    value = value.strip().lstrip("+")
    if value.lower() in ("never", "siempre", "0"):
        return 0
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([hdmayHDMAY]?)", value)
    if not m:
        return 4 * 3600  # default 4 horas
    n = float(m.group(1))
    unit = m.group(2).lower()
    return n * {"h": 3600, "d": 86400, "m": 30 * 86400, "a": 365 * 86400, "y": 365 * 86400}.get(unit, 3600)


@app.route("/ack", methods=["GET", "POST"])
@require_spong_ack
def ack():
    if request.method == "POST":
        host = request.form.get("host", "")
        services = request.form.get("services", ".*")
        duration_str = request.form.get("hours", "+4h")
        contact = request.form.get("contact", "web-user@localhost")
        message = request.form.get("message", "acknowledged via web")

        duration_secs = _parse_duration(duration_str)
        end_time = 0 if duration_secs == 0 else time.time() + duration_secs
        send_ack(host=host, services=services, end_time=end_time,
                 contact=contact, message=message)
        _invalidate_dashboard_cache()
        return redirect(url_for("host_detail", hostname=host))

    host = request.args.get("host", "")
    service = request.args.get("service", ".*")
    return render_template("ack.html", host=host, service=service)


@app.route("/sgt-ticket", methods=["GET", "POST"])
@require_spong_ack
def sgt_ticket():
    """Crea un ticket en SGT a partir de un problema activo en spong.

    GET con host/service en query string muestra la pantalla de confirmación.
    POST efectivamente lo crea y vuelve a /problems con un flash de éxito o
    error. Si la integración SGT está deshabilitada, 404.
    """
    if not sgt_link.enabled():
        from flask import abort
        abort(404)

    if request.method == "POST":
        host = request.form.get("host", "").strip()
        service = request.form.get("service", "").strip()
        color = request.form.get("color", "red").strip() or "red"
        summary = request.form.get("summary", "").strip()
        if not host or not service:
            return redirect(url_for("problems", sgt_err="Faltan host/service."))
        try:
            link = sgt_link.crear_ticket(
                host=host, service=service, color=color, summary=summary,
                creado_por=getattr(g, "spong_user", "") or "",
            )
            return redirect(url_for("problems", sgt_ok=link["ticket_display"]))
        except sgt_link.SgtError as e:
            return redirect(url_for("problems", sgt_err=str(e)[:300]))

    host = request.args.get("host", "").strip()
    service = request.args.get("service", "").strip()
    color = request.args.get("color", "red").strip() or "red"
    summary = request.args.get("summary", "").strip()
    existing = sgt_link.link_for(host, service) if host and service else None
    return render_template(
        "sgt_ticket.html",
        host=host, service=service, color=color, summary=summary,
        existing=existing,
    )


@app.after_request
def refresh_cookies(response):
    if getattr(g, "spong_clear_logout", False):
        response.delete_cookie("spong_logged_out")
        response.delete_cookie("spong_reauth")
    if request.endpoint in ("set_lang", "set_theme", "config_admin.set_lang", "config_admin.set_theme"):
        return response
    lang = request.cookies.get("lang")
    if lang and lang in _SUPPORTED_LANGS:
        response.set_cookie("lang", lang, max_age=10 * 365 * 86400, samesite="Lax")
    theme = request.cookies.get("theme")
    if theme in ("light", "dark"):
        response.set_cookie("theme", theme, max_age=10 * 365 * 86400, samesite="Lax")
    return response


@app.route("/set-theme/<theme>")
def set_theme(theme):
    if theme not in ("light", "dark"):
        theme = "light"
    resp = redirect(request.referrer or url_for("index"))
    resp.set_cookie("theme", theme, max_age=10 * 365 * 86400, samesite="Lax")
    return resp


@app.route("/set-lang/<lang>")
def set_lang(lang):
    if lang not in _SUPPORTED_LANGS:
        lang = "es"
    resp = redirect(request.referrer or url_for("index"))
    resp.set_cookie("lang", lang, max_age=10 * 365 * 86400, samesite="Lax")
    return resp


@app.route("/ack-del/<hostname>/<ack_file_id>")
@app.route("/ack-del/<path:ack_id>")
@require_spong_ack
def ack_del(ack_id=None, hostname=None, ack_file_id=None):
    if hostname and ack_file_id:
        database.delete_ack_by_id(hostname, ack_file_id)
        send_ack_del(f"{hostname}-{ack_file_id}")
    elif ack_id:
        send_ack_del(ack_id)
    _invalidate_dashboard_cache()
    referrer = request.referrer or url_for("index")
    return redirect(referrer)


@app.route("/api/status")
def api_status():
    """JSON API for status data."""
    hosts = config.get_hosts()
    result = {}
    for hostname in hosts:
        services = _load_visible_services(hostname)
        result[hostname] = {
            svc_name: {
                "color": svc.color,
                "summary": svc.summary,
                "report_time": svc.report_time,
                "duration": svc.duration,
            }
            for svc_name, svc in services.items()
        }
    return jsonify(result)


@app.route("/api/service/<hostname>/<service>")
def api_service(hostname, service):
    payload = _service_status_payload(hostname, service)
    if payload is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(payload)


@app.route("/api/check/<hostname>/<service>", methods=["POST"])
@require_spong_admin
def api_check(hostname, service):
    """Ejecuta el plugin de red on-demand y devuelve el nuevo estado."""
    import importlib
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
    from spong.status_sender import send_status as _send_status

    if hostname not in config.get_hosts():
        return jsonify({"error": "unknown host"}), 404
    if not _is_visible_service(hostname, service):
        return jsonify({"error": "unknown service"}), 404

    # Cargar plugin de red
    try:
        mod = importlib.import_module(f"spong.plugins.network.{service}")
        func = getattr(mod, f"check_{service}", None)
    except ImportError:
        func = None

    if not func:
        payload = _service_status_payload(hostname, service)
        if payload is None:
            return jsonify({"error": "no_plugin"}), 400
        payload.update({
            "checked": False,
            "no_plugin": True,
            "throttled": False,
            "cooldown_seconds": _CHECK_COOLDOWN_SECONDS,
        })
        return jsonify(payload)

    allowed, reason = _check_begin(hostname, service)
    if not allowed:
        payload = _service_status_payload(hostname, service) or {
            "color": "clear",
            "summary": "check already running" if reason == "running" else f"recheck rate limited ({_CHECK_COOLDOWN_SECONDS}s)",
            "message": "",
            "report_time": time.time(),
            "duration": 0,
        }
        payload.update({
            "checked": False,
            "throttled": True,
            "throttle_reason": reason,
            "cooldown_seconds": _CHECK_COOLDOWN_SECONDS,
        })
        return jsonify(payload), 202

    try:
        # Ejecutar con timeout para evitar que la request cuelgue indefinidamente
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(func, hostname)
                color, summary, message = future.result(timeout=35)
        except FuturesTimeout:
            color, summary, message = "yellow", "check timeout", ""
        except Exception as e:
            color, summary, message = "red", f"check error: {e}", ""

        _send_status(hostname, service, color, summary, message)

        # Pequeña pausa para que spong-server persista el resultado
        time.sleep(0.3)

        payload = _service_status_payload(hostname, service)
        if payload is None:
            payload = {
                "color": color,
                "summary": summary,
                "message": message,
                "report_time": time.time(),
                "duration": 0,
            }
        payload.update({
            "checked": True,
            "throttled": False,
            "cooldown_seconds": _CHECK_COOLDOWN_SECONDS,
        })
        return jsonify(payload)
    finally:
        _check_end(hostname, service)


def _uptime_stats(host: str, service: str, periods_days: list[int]) -> dict:
    """Calculate uptime % for a service over multiple periods.

    Returns dict: { days: pct|None }
    "Up" = green or yellow. "Down" = red. purple/clear = excluded from total.
    """
    import re as _re
    from pathlib import Path as _Path

    now = time.time()
    max_days = max(periods_days)
    cutoff = now - max_days * 86400

    history_file = _Path(f"/usr/local/spong/var/database/{host}/history/current")
    events = []  # list of (timestamp, color)

    if history_file.exists():
        try:
            for line in history_file.read_text().splitlines():
                parts = line.split(None, 4)
                if len(parts) < 4 or parts[0] != "status":
                    continue
                ts, svc, color = int(parts[1]), parts[2], parts[3]
                if svc == service and ts >= cutoff:
                    events.append((ts, color))
        except Exception:
            pass

    # Add current state as final sentinel
    svc_dir = _Path(f"/usr/local/spong/var/database/{host}/services")
    current_color = None
    for f in svc_dir.glob(f"{service}-*"):
        current_color = f.name.split("-", 1)[1]
        try:
            m = _re.search(r"^timestamp \d+ (\d+)", f.read_text())
            ts = int(m.group(1)) if m else int(now)
        except Exception:
            ts = int(now)
        events.append((ts, current_color))
        break

    if not events:
        return {d: None for d in periods_days}

    events.sort()
    # Append "now" as a virtual event to close the last interval
    events.append((int(now), events[-1][1]))

    result = {}
    for days in periods_days:
        period_start = now - days * 86400
        up = 0.0
        total = 0.0
        for i in range(len(events) - 1):
            ts, color = events[i]
            next_ts = events[i + 1][0]
            seg_start = max(ts, period_start)
            seg_end = min(next_ts, now)
            if seg_end <= seg_start:
                continue
            duration = seg_end - seg_start
            if color in ("green", "yellow", "blue"):
                up += duration
                total += duration
            elif color == "red":
                total += duration
            # purple/clear: excluded (no data → don't penalize)
        result[days] = round(up / total * 100, 1) if total > 0 else None
    return result


def _global_status_history(max_age_days: float = 30) -> list[dict]:
    items = []
    hosts = sorted(set(config.get_hosts()) | set(database.list_hosts()))
    for host in hosts:
        for entry in database.load_history(host, max_age_days=max_age_days, status_changes_only=True):
            items.append({"host": host, "entry": entry})
    items.sort(key=lambda item: (item["entry"].timestamp, item["host"], item["entry"].service), reverse=True)
    return items


def _general_history_days() -> int:
    try:
        days = int(config.get("web.general_history_days", 7))
    except (TypeError, ValueError):
        days = 7
    return max(1, days)


@app.route("/uptime")
def uptime_page():
    groups   = config.get_groups()
    hosts_cfg = config.get_hosts()
    periods  = [1, 7, 30]

    group_rows = []
    for gname, gdata in groups.items():
        if not gdata.get("display", True):
            continue
        rows = []
        for hostname in gdata.get("members", []):
            if hostname not in hosts_cfg:
                continue
            services = [s for s, _ in config.host_services(hostname)]
            for svc in services:
                stats = _uptime_stats(hostname, svc, periods)
                # current color
                cur_color = "clear"
                from pathlib import Path as _P
                for f in _P(f"/usr/local/spong/var/database/{hostname}/services").glob(f"{svc}-*"):
                    cur_color = f.name.split("-", 1)[1]
                    break
                rows.append({
                    "host": hostname,
                    "service": svc,
                    "color": cur_color,
                    "stats": stats,
                })
        if rows:
            group_rows.append({"name": gdata.get("name", gname), "rows": rows})

    return render_template("uptime.html", group_rows=group_rows, periods=periods)


@app.route("/history")
def history_page():
    history_days = _general_history_days()
    history = _global_status_history(max_age_days=history_days)
    service_options = sorted({item["entry"].service.lower() for item in history})

    raw_service_filters = []
    for value in request.args.getlist("service"):
        raw_service_filters.extend(part.strip().lower() for part in value.split(","))
    service_filters = []
    for value in raw_service_filters:
        if value and value not in service_filters:
            service_filters.append(value)

    raw_color_filters = []
    for value in request.args.getlist("color"):
        raw_color_filters.extend(part.strip().lower() for part in value.split(","))
    color_filters = []
    for value in raw_color_filters:
        if value and value not in color_filters:
            color_filters.append(value)

    if service_filters:
        allowed_services = set(service_filters)
        history = [
            item for item in history
            if item["entry"].service.lower() in allowed_services
        ]
    if color_filters:
        allowed_colors = set(color_filters)
        history = [
            item for item in history
            if item["entry"].color.lower() in allowed_colors
        ]

    return render_template(
        "history.html",
        history=history,
        history_days=history_days,
        service_filters=service_filters,
        service_options=service_options,
        color_filters=color_filters,
        color_options=("red", "yellow", "green", "purple", "clear", "blue"),
    )


@app.route("/api/problems")
def api_problems():
    hosts = config.get_hosts()
    issues = []
    for hostname in hosts:
        services = _load_visible_services(hostname)
        for svc_name, svc in services.items():
            if svc.color in ("red", "yellow", "purple"):
                issues.append({
                    "host": hostname,
                    "service": svc_name,
                    "color": svc.color,
                    "summary": svc.summary,
                    "duration": svc.duration,
                })
    return jsonify(issues)


@app.route("/rrd/<hostname>/<service>.png", methods=["GET", "HEAD"])
def rrd_graph(hostname, service):
    from spong import rrd as _rrd

    cache_headers = {"Cache-Control": f"public, max-age={_GRAPH_CACHE_TTL}"}
    if request.method == "HEAD":
        # Old host pages probe /rrd/... with HEAD to decide whether to show graph buttons.
        # Answer cheaply instead of rendering PNGs just for availability checks.
        return Response(status=204, headers=cache_headers)

    period = request.args.get("period", "24h")
    width = int(request.args.get("w", 500))
    height = int(request.args.get("h", 120))
    mounts_mode = "full" if service.lower() in ("disk", "diski") else request.args.get("mounts", "filtered").strip().lower()
    if mounts_mode not in ("filtered", "full"):
        mounts_mode = "filtered"
    cache_key = (hostname, service.lower(), period, width, height, mounts_mode)

    cached = _graph_cache_get(cache_key)
    if cached is not None:
        status, data = cached
        headers = {**cache_headers, "X-Spong-Graph-Cache": "HIT"}
        if status != 200:
            return Response("Sin datos RRD", status=status, headers=headers)
        return Response(data, mimetype="image/png", headers=headers)

    data = _rrd.graph_png(hostname, service, period, width, height, mounts=mounts_mode)
    headers = {**cache_headers, "X-Spong-Graph-Cache": "MISS"}
    if not data:
        _graph_cache_put(cache_key, 404, None)
        return Response("Sin datos RRD", status=404, headers=headers)

    _graph_cache_put(cache_key, 200, data)
    return Response(data, mimetype="image/png", headers=headers)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8090, debug=False, use_reloader=False)
