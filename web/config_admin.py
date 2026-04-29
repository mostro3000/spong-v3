"""Web UI Blueprint for editing SPONG YAML configuration files."""
from __future__ import annotations

import difflib
import ipaddress
import json
import os
import re
import secrets
import yaml
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, Response, g, has_request_context, make_response, current_app
from werkzeug.security import generate_password_hash

from spong import config as spong_config, database as spong_database
from auth_utils import check_basic_auth

config_bp = Blueprint('config_admin', __name__, url_prefix='/config')

ETC_DIR     = Path('/usr/local/spong/etc')
PLUGIN_DIR  = Path('/usr/local/spong/spong/plugins/network')
HISTORY_DIR = Path('/usr/local/spong/var/config_history')

_NUMBERED_RE = re.compile(r'^(.+?)\d+$')
_SERVICE_NAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]*$')
_USER_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]+$')
_MAX_HISTORY = 100

_CONFIG_FILES = {
    'hosts':  ETC_DIR / 'hosts.yaml',
    'groups': ETC_DIR / 'groups.yaml',
}

_SUPPORTED_CONFIG_LANGS = {'es', 'en', 'fr', 'de', 'pt', 'zh', 'ru'}
_CONFIG_REALM = 'SPONG Config'
_CONFIG_LOGGED_OUT_REALM = 'SPONG Config signed out'

_ACTION_LABELS = {
    'new':         'Nuevo',
    'edit':        'Editado',
    'delete':      'Eliminado',
    'restore':     'Restaurado',
    'pre-restore': 'Backup previo',
}

_CONFIG_PERMISSIONS = {'view', 'add', 'edit', 'delete', 'restore', 'users'}
_CONFIG_ROLES = {
    'admin':  _CONFIG_PERMISSIONS,
    'editor': {'view', 'add', 'edit'},
    'add':    {'view', 'add'},
    'read':   {'view'},
}
_CONFIG_ROLE_ALIASES = {
    'owner':     'admin',
    'write':     'editor',
    'readonly':  'read',
    'read-only': 'read',
    'viewer':    'read',
    'add-only':  'add',
    'add_only':  'add',
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _normalize_config_role(role: str | None) -> str:
    role = (role or 'read').strip().lower()
    return _CONFIG_ROLE_ALIASES.get(role, role if role in _CONFIG_ROLES else 'read')


def _normalize_permissions(raw_permissions, role: str) -> frozenset[str]:
    if raw_permissions is None:
        return frozenset(_CONFIG_ROLES[role])
    if isinstance(raw_permissions, str):
        values = re.split(r'[\s,]+', raw_permissions)
    elif isinstance(raw_permissions, (list, tuple, set)):
        values = raw_permissions
    else:
        values = []
    permissions = {str(p).strip().lower() for p in values if str(p).strip()}
    permissions = {p for p in permissions if p in _CONFIG_PERMISSIONS}
    permissions.add('view')
    return frozenset(permissions)


def _config_user_entries() -> dict[str, dict]:
    entries: dict[str, dict] = {}
    users_cfg = spong_config.get('web.config_users', {})
    if isinstance(users_cfg, dict):
        for username, entry in users_cfg.items():
            username = str(username or '').strip()
            if not username or not isinstance(entry, dict):
                continue
            role = _normalize_config_role(entry.get('role'))
            entries[username] = {
                'password': entry.get('password', ''),
                'password_hash': entry.get('password_hash', ''),
                'role': role,
                'permissions': _normalize_permissions(entry.get('permissions'), role),
            }

    legacy_user = spong_config.get('web.config_user', '')
    if legacy_user and legacy_user not in entries:
        entries[legacy_user] = {
            'password': spong_config.get('web.config_password', ''),
            'password_hash': spong_config.get('web.config_password_hash', ''),
            'role': 'admin',
            'permissions': frozenset(_CONFIG_ROLES['admin']),
        }
    return entries


def _config_users_section() -> dict[str, dict]:
    data = _load_yaml(ETC_DIR / 'spong.yaml')
    web = data.get('web', {})
    users_cfg = web.get('config_users', {})
    return users_cfg if isinstance(users_cfg, dict) else {}


def _dump_config_users_section(users_cfg: dict[str, dict]) -> None:
    data = _load_yaml(ETC_DIR / 'spong.yaml')
    web = data.setdefault('web', {})
    web['config_users'] = users_cfg
    _save_yaml(ETC_DIR / 'spong.yaml', data)


def _config_users_snapshot_data() -> dict:
    return {'config_users': _config_users_section()}


def _current_config_users_yaml() -> str:
    return yaml.dump(_config_users_snapshot_data(), default_flow_style=False, allow_unicode=True, sort_keys=False)


def _legacy_config_user() -> str:
    return spong_config.get('web.config_user', '') or ''


def _config_users_count() -> int:
    return len(_config_users_section()) + (1 if _legacy_config_user() else 0)


def _has_admin_user(users_cfg: dict[str, dict], include_legacy: bool = True) -> bool:
    if include_legacy and _legacy_config_user():
        return True
    for entry in users_cfg.values():
        if isinstance(entry, dict) and _normalize_config_role(entry.get('role')) == 'admin':
            return True
    return False


def _authenticate_config_user(username: str | None, password: str | None) -> tuple[str, str, frozenset[str]] | None:
    for expected_user, entry in _config_user_entries().items():
        if check_basic_auth(username, password, expected_user, entry['password'], entry['password_hash']):
            return expected_user, entry['role'], entry['permissions']
    return None


def _config_can(permission: str) -> bool:
    return permission in getattr(g, 'config_permissions', frozenset())


def config_permission_available(permission: str) -> bool:
    return any(permission in entry['permissions'] for entry in _config_user_entries().values())


def _permission_denied(permission: str) -> Response:
    return Response('Permiso insuficiente para esta acción.', 403)


def _config_no_store(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def _require_config_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _config_user_entries():
            return Response('Config UI no habilitado. Configurá web.config_user o web.config_users en spong.yaml.', 403)
        logged_out_token = request.cookies.get('spong_config_logged_out')
        reauth_token = request.cookies.get('spong_config_reauth')
        auth = request.authorization
        identity = _authenticate_config_user(auth.username, auth.password) if auth else None
        if logged_out_token:
            realm = f'{_CONFIG_LOGGED_OUT_REALM} {logged_out_token}'
            if reauth_token != logged_out_token:
                resp = Response(
                    'Sesión cerrada. Volvé a autenticarte para entrar a la configuración.',
                    401,
                    {'WWW-Authenticate': f'Basic realm="{realm}"'},
                )
                resp.set_cookie('spong_config_reauth', logged_out_token, max_age=5 * 60, samesite='Lax')
                return _config_no_store(resp)
            if not identity:
                return _config_no_store(Response(
                    'Sesión cerrada. Volvé a autenticarte para entrar a la configuración.',
                    401,
                    {'WWW-Authenticate': f'Basic realm="{realm}"'},
                ))
            g.config_user, g.config_role, g.config_permissions = identity
            resp = make_response(f(*args, **kwargs))
            resp.delete_cookie('spong_config_logged_out')
            resp.delete_cookie('spong_config_reauth')
            return _config_no_store(resp)
        if not identity:
            return _config_no_store(Response(
                'Acceso restringido — ingresá usuario y contraseña de administración.',
                401,
                {'WWW-Authenticate': f'Basic realm="{_CONFIG_REALM}"'},
            ))
        g.config_user, g.config_role, g.config_permissions = identity
        return _config_no_store(make_response(f(*args, **kwargs)))
    return decorated


@config_bp.context_processor
def _config_auth_context():
    permissions = getattr(g, 'config_permissions', frozenset())
    return {
        'config_user': getattr(g, 'config_user', ''),
        'config_role': getattr(g, 'config_role', 'read'),
        'config_can': lambda permission: permission in permissions,
    }


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _save_yaml(path: Path, data: dict) -> None:
    tmp = path.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# History helpers
# ---------------------------------------------------------------------------

def _current_config_actor() -> tuple[str, str, str]:
    if not has_request_context():
        return 'system', 'system', ''
    return (
        getattr(g, 'config_user', '') or 'unknown',
        getattr(g, 'config_role', '') or 'unknown',
        request.headers.get('X-Forwarded-For', request.remote_addr or '').split(',')[0].strip(),
    )


def _save_config_snapshot(config_name: str, data: dict, action: str, detail: str) -> None:
    """Save a YAML snapshot of the config state after a change."""
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    config_dir = HISTORY_DIR / config_name
    config_dir.mkdir(exist_ok=True)

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    snapshot_path = config_dir / f'{ts}.yaml'
    counter = 0
    while snapshot_path.exists():
        counter += 1
        snapshot_path = config_dir / f'{ts}_{counter}.yaml'
    ts_key = snapshot_path.stem

    with open(snapshot_path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    log_path = HISTORY_DIR / 'log.json'
    try:
        log = json.loads(log_path.read_text())
    except (FileNotFoundError, ValueError):
        log = []

    actor, role, remote_addr = _current_config_actor()
    log.append({
        'ts':     datetime.now().isoformat(timespec='seconds'),
        'ts_key': ts_key,
        'config': config_name,
        'action': action,
        'detail': detail,
        'user':   actor,
        'role':   role,
        'remote_addr': remote_addr,
    })

    if len(log) > _MAX_HISTORY:
        for entry in log[:-_MAX_HISTORY]:
            p = HISTORY_DIR / entry['config'] / f"{entry['ts_key']}.yaml"
            if p.exists():
                p.unlink()
        log = log[-_MAX_HISTORY:]

    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2))


def _load_history_log() -> list:
    log_path = HISTORY_DIR / 'log.json'
    try:
        return json.loads(log_path.read_text())
    except (FileNotFoundError, ValueError):
        return []


def _get_snapshot_yaml(config_name: str, ts_key: str) -> str | None:
    if config_name == 'users':
        if not re.match(r'^\d{8}_\d{6}(_\d+)?$', ts_key):
            return None
        p = HISTORY_DIR / 'users' / f'{ts_key}.yaml'
        return p.read_text() if p.exists() else None
    if config_name not in _CONFIG_FILES:
        return None
    if not re.match(r'^\d{8}_\d{6}(_\d+)?$', ts_key):
        return None
    p = HISTORY_DIR / config_name / f'{ts_key}.yaml'
    return p.read_text() if p.exists() else None


# ---------------------------------------------------------------------------
# Service helpers
# ---------------------------------------------------------------------------

def _available_services() -> list[str]:
    if not PLUGIN_DIR.exists():
        return []
    all_svcs = sorted(
        p.stem for p in PLUGIN_DIR.glob('*.py')
        if (
            p.name != '__init__.py'
            and not p.name.startswith(('_', '.'))
            and _SERVICE_NAME_RE.match(p.stem)
        )
    )
    base_set = set(all_svcs)
    filtered = []
    for s in all_svcs:
        m = _NUMBERED_RE.match(s)
        if m and m.group(1) in base_set:
            continue  # skip camara1..N when camara exists
        filtered.append(s)
    return filtered


_SERVICE_CATEGORIES = [
    ('Conectividad',    ['ping', 'http', 'https', 'ssh', 'ftp', 'smtp', 'pop', 'imap',
                         'telnet', 'dns', 'ntp', 'poppassd']),
    ('Red / Monitoreo', ['snmp', 'macs', 'iftraffic', 'presence', 'wuptime', 'suptime',
                         'ruptime', 'wassoc', 'proxy', 'proxy2', 'proxy_google']),
    ('Rendimiento',     ['cpu', 'mem', 'rcpu', 'rtemp', 'scpu', 'scpu1m', 'scpu5s', 'memolt']),
    ('Almacenamiento',  ['disk', 'diski', 'nfs']),
    ('Sensores',        ['temp', 'hum', 'co2', 'soil', 'viento', 'presion', 'rafaga',
                         'termica', 'freq_in', 'freq_out', 'temp_bat', 'temp_ext',
                         'volt_in', 'volt_out']),
    ('Cámaras',         ['rtsp', 'camara', 'dvrcam']),
    ('UPS',             ['ups']),
    ('Cliente',         ['jobs', 'logs', 'memory', 'sensors', 'hddtemp', 'uptime', 'speedtest']),
]


def _categorize_services(available: list[str]) -> list[tuple[str, list[str]]]:
    avail_set = set(available)
    assigned: set[str] = set()
    result = []
    for cat_name, cat_svcs in _SERVICE_CATEGORIES:
        items = [s for s in cat_svcs if s in avail_set and s not in assigned]
        if items:
            result.append((cat_name, items))
            assigned.update(items)
    others = [s for s in available if s not in assigned]
    if others:
        result.append(('Otros', others))
    return result


def _parse_services(services_str: str) -> tuple[list[str], set[str]]:
    svcs: list[str] = []
    stops: set[str] = set()
    for token in re.split(r'[\s,]+', services_str or ''):
        token = token.strip()
        if not token:
            continue
        if token.endswith(':'):
            name = token[:-1]
            svcs.append(name)
            stops.add(name)
        else:
            svcs.append(token)
    return svcs, stops


def _build_services_str(services: list[str], stops: set[str]) -> str:
    return ' '.join(s + ':' if s in stops else s for s in services if s)


def _service_set_from_entry(entry: dict | None) -> set[str]:
    svcs, _ = _parse_services((entry or {}).get('services', ''))
    return set(svcs)


def _delete_service_statuses(hostnames, services: set[str]) -> None:
    for host in sorted(set(h for h in hostnames if h)):
        for service in sorted(services):
            spong_database.delete_service(host, service)


def _clear_dashboard_cache() -> None:
    cache = current_app.config.get('SPONG_DASHBOARD_CACHE')
    lock = current_app.config.get('SPONG_DASHBOARD_CACHE_LOCK')
    if not isinstance(cache, dict):
        return
    if lock:
        with lock:
            cache.update({'ts': 0.0, 'group_data': None, 'sidebar': None})
    else:
        cache.update({'ts': 0.0, 'group_data': None, 'sidebar': None})


# ---------------------------------------------------------------------------
# Routes — Index
# ---------------------------------------------------------------------------

@config_bp.route('/set-theme/<theme>')
@_require_config_auth
def set_theme(theme):
    if theme not in ('light', 'dark'):
        theme = 'light'
    resp = redirect(request.referrer or url_for('config_admin.index'))
    resp.set_cookie('theme', theme, max_age=10 * 365 * 86400, samesite='Lax')
    return resp


@config_bp.route('/set-lang/<lang>')
@_require_config_auth
def set_lang(lang):
    if lang not in _SUPPORTED_CONFIG_LANGS:
        lang = 'es'
    resp = redirect(request.referrer or url_for('config_admin.index'))
    resp.set_cookie('lang', lang, max_age=10 * 365 * 86400, samesite='Lax')
    return resp


@config_bp.route('/logout')
def logout():
    logout_token = secrets.token_urlsafe(8)
    resp = redirect(url_for('index'))
    resp.set_cookie('spong_config_logged_out', logout_token, max_age=30 * 60, samesite='Lax')
    resp.delete_cookie('spong_config_reauth')
    return resp


@config_bp.route('/')
@_require_config_auth
def index():
    hosts_data  = _load_yaml(ETC_DIR / 'hosts.yaml')
    groups_data = _load_yaml(ETC_DIR / 'groups.yaml')
    return render_template(
        'config_index.html',
        host_count=len(hosts_data.get('hosts', {})),
        group_count=len(groups_data.get('groups', {})),
        history_count=len(_load_history_log()),
        users_count=_config_users_count(),
    )


# ---------------------------------------------------------------------------
# Routes — Users
# ---------------------------------------------------------------------------

def _normalize_user_entry(username: str, entry: dict) -> dict:
    role = _normalize_config_role(entry.get('role'))
    return {
        'username': username,
        'role': role,
        'has_password': bool(entry.get('password_hash') or entry.get('password')),
        'permissions': _normalize_permissions(entry.get('permissions'), role),
    }


def _sorted_config_users() -> list[dict]:
    users = [
        _normalize_user_entry(username, entry)
        for username, entry in _config_users_section().items()
        if isinstance(entry, dict)
    ]
    return sorted(users, key=lambda item: item['username'].lower())


def _users_log_entries() -> list[dict]:
    return [
        entry for entry in sorted(_load_history_log(), key=lambda e: e.get('ts', ''), reverse=True)
        if entry.get('config') == 'users'
    ]


@config_bp.route('/users')
@_require_config_auth
def users():
    if not _config_can('users'):
        return _permission_denied('users')
    legacy_user = _legacy_config_user()
    return render_template(
        'config_users.html',
        users=_sorted_config_users(),
        legacy_user=legacy_user,
        history_entries=_users_log_entries()[:10],
        role_labels={
            'admin': 'Administrador',
            'editor': 'Editor',
            'add': 'Solo agregar',
            'read': 'Solo lectura',
        },
    )


@config_bp.route('/user/new', methods=['GET', 'POST'])
@config_bp.route('/user/<username>/edit', methods=['GET', 'POST'])
@_require_config_auth
def user_edit(username=None):
    if not _config_can('users'):
        return _permission_denied('users')

    users_cfg = _config_users_section()
    legacy_user = _legacy_config_user()
    error = None
    existing = users_cfg.get(username, {}) if username else {}
    if username and username not in users_cfg:
        return Response('Usuario no encontrado.', 404)

    if request.method == 'POST':
        new_username = request.form.get('username', '').strip()
        role = _normalize_config_role(request.form.get('role', 'read'))
        password = request.form.get('password', '')

        if not new_username:
            error = 'El nombre de usuario es obligatorio.'
        elif not _USER_NAME_RE.match(new_username):
            error = 'El nombre de usuario solo puede tener letras, números, puntos, guiones y guiones bajos.'
        elif new_username == legacy_user:
            error = 'Ese usuario es el administrador legacy y se administra desde spong.yaml.'
        elif new_username != username and new_username in users_cfg:
            error = 'Ya existe un usuario con ese nombre.'
        else:
            if password:
                password_hash = generate_password_hash(password)
            elif existing.get('password_hash'):
                password_hash = existing.get('password_hash', '')
            elif existing.get('password'):
                password_hash = generate_password_hash(existing.get('password', ''))
            else:
                password_hash = ''
            if not password_hash:
                error = 'Tenés que cargar una contraseña.'
            else:
                candidate_users = dict(users_cfg)
                if new_username != username and username in candidate_users:
                    del candidate_users[username]
                candidate_users[new_username] = {
                    'password': '',
                    'password_hash': password_hash,
                    'role': role,
                }
                if not _has_admin_user(candidate_users):
                    error = 'Tiene que quedar al menos un usuario administrador.'
                else:
                    _dump_config_users_section(candidate_users)
                    action = 'edit' if username else 'new'
                    detail = f"{'Editó' if username else 'Nuevo'} usuario: {new_username} ({role})"
                    _save_config_snapshot('users', _config_users_snapshot_data(), action, detail)
                    spong_config.load_all()
                    return redirect(url_for('config_admin.users'))

    return render_template(
        'config_user_edit.html',
        username=username,
        user=existing,
        error=error,
        roles=[
            ('admin', 'Administrador'),
            ('editor', 'Editor'),
            ('add', 'Solo agregar'),
            ('read', 'Solo lectura'),
        ],
    )


@config_bp.route('/user/<username>/delete', methods=['POST'])
@_require_config_auth
def user_delete(username):
    if not _config_can('users'):
        return _permission_denied('users')
    legacy_user = _legacy_config_user()
    if username == legacy_user:
        return Response('El usuario legacy se administra desde spong.yaml.', 403)
    users_cfg = _config_users_section()
    if username not in users_cfg:
        return Response('Usuario no encontrado.', 404)
    candidate_users = dict(users_cfg)
    del candidate_users[username]
    if not _has_admin_user(candidate_users):
        return Response('Tiene que quedar al menos un usuario administrador.', 403)
    _dump_config_users_section(candidate_users)
    _save_config_snapshot('users', _config_users_snapshot_data(), 'delete', f'Eliminó usuario: {username}')
    spong_config.load_all()
    return redirect(url_for('config_admin.users'))


# ---------------------------------------------------------------------------
# Routes — Hosts
# ---------------------------------------------------------------------------

def _first_host_ip(host: dict) -> str:
    ips = host.get('ip_addr') or []
    if isinstance(ips, str):
        return ips.strip()
    return str(ips[0]).strip() if ips else ''


def _host_ip_sort_key(host: dict) -> tuple:
    ip_value = _first_host_ip(host)
    try:
        parsed = ipaddress.ip_address(ip_value)
        return (0, parsed.version, int(parsed), '')
    except ValueError:
        return (1, 0, 0, ip_value.lower())


@config_bp.route('/hosts')
@_require_config_auth
def hosts():
    data = _load_yaml(ETC_DIR / 'hosts.yaml')
    hosts_dict = data.get('hosts', {})
    sort_by = request.args.get('sort', 'host').strip().lower()
    sort_dir = request.args.get('dir', 'asc').strip().lower()
    if sort_by not in ('host', 'ip'):
        sort_by = 'host'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'

    host_rows = list(hosts_dict.items())
    if sort_by == 'ip':
        host_rows.sort(key=lambda item: (_host_ip_sort_key(item[1]), item[0].lower()))
    else:
        host_rows.sort(key=lambda item: item[0].lower())
    if sort_dir == 'desc':
        host_rows.reverse()

    return render_template(
        'config_hosts.html',
        hosts=host_rows,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


@config_bp.route('/host/new', methods=['GET', 'POST'])
@config_bp.route('/host/<hostname>/edit', methods=['GET', 'POST'])
@_require_config_auth
def host_edit(hostname=None):
    data       = _load_yaml(ETC_DIR / 'hosts.yaml')
    hosts_dict = data.setdefault('hosts', {})
    contacts   = list((data.get('contacts') or {}).keys())
    available  = _available_services()
    categories = _categorize_services(available)
    avail_set  = set(available)
    error      = None
    required_permission = 'edit' if hostname else 'add'
    if not _config_can(required_permission):
        return _permission_denied(required_permission)

    if request.method == 'POST':
        new_name = request.form.get('hostname', '').strip()
        if not new_name:
            error = 'El nombre del host es obligatorio.'
        elif not hostname and new_name in hosts_dict and not _config_can('edit'):
            error = 'Ya existe un host con ese nombre.'
        else:
            # IPs
            ips_raw = request.form.get('ip_addr', '').strip()
            ips = [ip.strip() for ip in re.split(r'[\s\n,]+', ips_raw) if ip.strip()]
            if not ips:
                ips = [new_name]

            contact = request.form.get('contact', 'mt').strip()

            # Services — collect checkboxes, then custom text
            selected = request.form.getlist('services')
            ping_stop = 'ping_stop' in request.form
            stops: set[str] = set()
            ordered: list[str] = []

            if 'ping' in selected:
                ordered.append('ping')
                if ping_stop:
                    stops.add('ping')
                selected = [s for s in selected if s != 'ping']
            ordered.extend(selected)

            custom_raw = request.form.get('custom_services', '')
            for s in re.split(r'[\s,]+', custom_raw):
                s = s.strip()
                if s and s not in ordered:
                    ordered.append(s)

            services_str = _build_services_str(ordered, stops)
            old_services = _service_set_from_entry(hosts_dict.get(hostname)) if hostname else set()
            new_services = set(ordered)
            removed_services = old_services - new_services

            # Schedules
            s_svcs = request.form.getlist('sched_service')
            s_days = request.form.getlist('sched_days')
            s_from = request.form.getlist('sched_from')
            s_to   = request.form.getlist('sched_to')
            schedules: dict[str, list] = {}
            for svc, days, frm, to_ in zip(s_svcs, s_days, s_from, s_to):
                svc, days, frm, to_ = svc.strip(), days.strip(), frm.strip(), to_.strip()
                if svc and days and frm and to_:
                    schedules.setdefault(svc, []).append(
                        {'days': days, 'from': frm, 'to': to_}
                    )

            entry: dict = {
                'services': services_str,
                'contact':  contact,
                'ip_addr':  ips,
            }
            if schedules:
                entry['schedules'] = schedules

            if hostname and hostname != new_name and hostname in hosts_dict:
                del hosts_dict[hostname]
            hosts_dict[new_name] = entry

            action = 'edit' if hostname else 'new'
            detail = f"{'Editó' if hostname else 'Nuevo'} host: {new_name}"
            _save_yaml(ETC_DIR / 'hosts.yaml', data)
            if removed_services:
                _delete_service_statuses({hostname, new_name}, removed_services)
            _save_config_snapshot('hosts', data, action, detail)
            spong_config.load_all()
            _clear_dashboard_cache()
            return redirect(url_for('config_admin.hosts'))

    # GET — load existing data
    host = hosts_dict.get(hostname, {}) if hostname else {}
    svcs_list, stops_set = _parse_services(host.get('services', ''))
    svcs_set = set(svcs_list)
    # Custom: services in use but not in the known plugin list
    custom_list = [s for s in svcs_list if s not in avail_set and s != 'ping']

    return render_template(
        'config_host_edit.html',
        hostname=hostname,
        host=host,
        svcs_list=svcs_list,
        svcs_set=svcs_set,
        stops_set=stops_set,
        contacts=contacts,
        categories=categories,
        custom_list=custom_list,
        error=error,
    )


@config_bp.route('/host/<hostname>/delete', methods=['POST'])
@_require_config_auth
def host_delete(hostname):
    if not _config_can('delete'):
        return _permission_denied('delete')
    data = _load_yaml(ETC_DIR / 'hosts.yaml')
    data.get('hosts', {}).pop(hostname, None)
    _save_yaml(ETC_DIR / 'hosts.yaml', data)
    _save_config_snapshot('hosts', data, 'delete', f'Eliminó host: {hostname}')
    spong_config.load_all()
    _clear_dashboard_cache()
    return redirect(url_for('config_admin.hosts'))


# ---------------------------------------------------------------------------
# Routes — Groups
# ---------------------------------------------------------------------------

@config_bp.route('/groups')
@_require_config_auth
def groups():
    data = _load_yaml(ETC_DIR / 'groups.yaml')
    groups_dict = data.get('groups', {})
    sort_by = request.args.get('sort', 'key').strip().lower()
    sort_dir = request.args.get('dir', 'asc').strip().lower()
    if sort_by not in ('key', 'name'):
        sort_by = 'key'
    if sort_dir not in ('asc', 'desc'):
        sort_dir = 'asc'

    group_rows = list(groups_dict.items())
    if sort_by == 'name':
        group_rows.sort(key=lambda item: (str(item[1].get('name', item[0])).lower(), item[0].lower()))
    else:
        group_rows.sort(key=lambda item: item[0].lower())
    if sort_dir == 'desc':
        group_rows.reverse()

    return render_template(
        'config_groups.html',
        groups=group_rows,
        sort_by=sort_by,
        sort_dir=sort_dir,
    )


@config_bp.route('/group/new', methods=['GET', 'POST'])
@config_bp.route('/group/<group_key>/edit', methods=['GET', 'POST'])
@_require_config_auth
def group_edit(group_key=None):
    gdata     = _load_yaml(ETC_DIR / 'groups.yaml')
    gdict     = gdata.setdefault('groups', {})
    hdata     = _load_yaml(ETC_DIR / 'hosts.yaml')
    all_hosts = sorted(hdata.get('hosts', {}).keys())
    error     = None
    required_permission = 'edit' if group_key else 'add'
    if not _config_can(required_permission):
        return _permission_denied(required_permission)

    if request.method == 'POST':
        new_key = request.form.get('group_key', '').strip()
        if not new_key:
            error = 'La clave del grupo es obligatoria.'
        elif not group_key and new_key in gdict and not _config_can('edit'):
            error = 'Ya existe un grupo con esa clave.'
        else:
            entry = {
                'name':     request.form.get('name', new_key).strip(),
                'summary':  request.form.get('summary', '').strip(),
                'members':  request.form.getlist('members'),
                'compress': 'compress' in request.form,
                'display':  'display'  in request.form,
            }
            if group_key and group_key != new_key and group_key in gdict:
                del gdict[group_key]
            gdict[new_key] = entry

            action = 'edit' if group_key else 'new'
            detail = f"{'Editó' if group_key else 'Nuevo'} grupo: {new_key}"
            _save_yaml(ETC_DIR / 'groups.yaml', gdata)
            _save_config_snapshot('groups', gdata, action, detail)
            spong_config.load_all()
            return redirect(url_for('config_admin.groups'))

    group = gdict.get(group_key, {}) if group_key else {}
    return render_template(
        'config_group_edit.html',
        group_key=group_key,
        group=group,
        all_hosts=all_hosts,
        error=error,
    )


@config_bp.route('/group/<group_key>/delete', methods=['POST'])
@_require_config_auth
def group_delete(group_key):
    if not _config_can('delete'):
        return _permission_denied('delete')
    data = _load_yaml(ETC_DIR / 'groups.yaml')
    data.get('groups', {}).pop(group_key, None)
    _save_yaml(ETC_DIR / 'groups.yaml', data)
    _save_config_snapshot('groups', data, 'delete', f'Eliminó grupo: {group_key}')
    spong_config.load_all()
    return redirect(url_for('config_admin.groups'))


# ---------------------------------------------------------------------------
# Routes — History
# ---------------------------------------------------------------------------

@config_bp.route('/history')
@_require_config_auth
def history():
    log = sorted(_load_history_log(), key=lambda e: e.get('ts', ''), reverse=True)
    return render_template('config_history.html', entries=log, action_labels=_ACTION_LABELS)


@config_bp.route('/history/<config_name>/<ts_key>')
@_require_config_auth
def history_view(config_name, ts_key):
    if config_name != 'users' and config_name not in _CONFIG_FILES:
        return Response('Config no válida', 400)

    snapshot_yaml = _get_snapshot_yaml(config_name, ts_key)
    if snapshot_yaml is None:
        return Response('Snapshot no encontrado', 404)

    if config_name == 'users':
        current_yaml = _current_config_users_yaml()
    else:
        current_path = _CONFIG_FILES[config_name]
        current_yaml = current_path.read_text() if current_path.exists() else ''

    # diff: current → snapshot  (+: will be added, -: will be removed on restore)
    diff_lines = list(difflib.unified_diff(
        current_yaml.splitlines(),
        snapshot_yaml.splitlines(),
        fromfile='actual',
        tofile='historial',
        lineterm='',
    ))

    log = _load_history_log()
    meta = next(
        (e for e in log if e.get('ts_key') == ts_key and e.get('config') == config_name),
        {},
    )

    return render_template(
        'config_history_view.html',
        config_name=config_name,
        ts_key=ts_key,
        meta=meta,
        snapshot_yaml=snapshot_yaml,
        diff_lines=diff_lines,
        action_labels=_ACTION_LABELS,
        identical=(len(diff_lines) == 0),
    )


@config_bp.route('/history/<config_name>/<ts_key>/restore', methods=['POST'])
@_require_config_auth
def history_restore(config_name, ts_key):
    if not _config_can('restore'):
        return _permission_denied('restore')
    if config_name != 'users' and config_name not in _CONFIG_FILES:
        return Response('Config no válida', 400)

    snapshot_yaml = _get_snapshot_yaml(config_name, ts_key)
    if snapshot_yaml is None:
        return Response('Snapshot no encontrado', 404)

    snapshot_data = yaml.safe_load(snapshot_yaml) or {}
    if config_name == 'users':
        current_data = _config_users_snapshot_data()
        _save_config_snapshot('users', current_data, 'pre-restore',
                              f'Backup automático antes de restaurar ({ts_key})')
        _dump_config_users_section(snapshot_data.get('config_users', {}))
        _save_config_snapshot('users', snapshot_data, 'restore',
                              f'Restauró usuarios de configuración desde {ts_key}')
    else:
        # Auto-backup current state before overwriting
        current_data = _load_yaml(_CONFIG_FILES[config_name])
        _save_config_snapshot(config_name, current_data, 'pre-restore',
                              f'Backup automático antes de restaurar ({ts_key})')
        _save_yaml(_CONFIG_FILES[config_name], snapshot_data)
        _save_config_snapshot(config_name, snapshot_data, 'restore',
                              f'Restauró {config_name}.yaml desde {ts_key}')
    spong_config.load_all()

    return redirect(url_for('config_admin.history'))
