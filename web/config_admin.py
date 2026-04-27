"""Web UI Blueprint for editing SPONG YAML configuration files."""
from __future__ import annotations

import difflib
import json
import os
import re
import yaml
from datetime import datetime
from pathlib import Path
from functools import wraps
from flask import Blueprint, render_template, request, redirect, url_for, Response

from spong import config as spong_config

config_bp = Blueprint('config_admin', __name__, url_prefix='/config')

ETC_DIR     = Path('/usr/local/spong/etc')
PLUGIN_DIR  = Path('/usr/local/spong/spong/plugins/network')
HISTORY_DIR = Path('/usr/local/spong/var/config_history')

_NUMBERED_RE = re.compile(r'^(.+?)\d+$')
_MAX_HISTORY = 100

_CONFIG_FILES = {
    'hosts':  ETC_DIR / 'hosts.yaml',
    'groups': ETC_DIR / 'groups.yaml',
}

_ACTION_LABELS = {
    'new':         'Nuevo',
    'edit':        'Editado',
    'delete':      'Eliminado',
    'restore':     'Restaurado',
    'pre-restore': 'Backup previo',
}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def _check_config_auth(username: str, password: str) -> bool:
    user   = spong_config.get('web.config_user', '')
    passwd = spong_config.get('web.config_password', '')
    return bool(user) and username == user and password == passwd


def _require_config_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not spong_config.get('web.config_user', ''):
            return Response('Config UI no habilitado. Configurá web.config_user en spong.yaml.', 403)
        auth = request.authorization
        if not auth or not _check_config_auth(auth.username, auth.password):
            return Response(
                'Acceso restringido — ingresá usuario y contraseña de administración.',
                401,
                {'WWW-Authenticate': 'Basic realm="SPONG Config"'},
            )
        return f(*args, **kwargs)
    return decorated


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

    log.append({
        'ts':     datetime.now().isoformat(timespec='seconds'),
        'ts_key': ts_key,
        'config': config_name,
        'action': action,
        'detail': detail,
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
        if p.name != '__init__.py' and not p.name.startswith('_') and p.stem
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


# ---------------------------------------------------------------------------
# Routes — Index
# ---------------------------------------------------------------------------

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
    )


# ---------------------------------------------------------------------------
# Routes — Hosts
# ---------------------------------------------------------------------------

@config_bp.route('/hosts')
@_require_config_auth
def hosts():
    data = _load_yaml(ETC_DIR / 'hosts.yaml')
    return render_template('config_hosts.html', hosts=data.get('hosts', {}))


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

    if request.method == 'POST':
        new_name = request.form.get('hostname', '').strip()
        if not new_name:
            error = 'El nombre del host es obligatorio.'
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
            _save_config_snapshot('hosts', data, action, detail)
            spong_config.load_all()
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
    data = _load_yaml(ETC_DIR / 'hosts.yaml')
    data.get('hosts', {}).pop(hostname, None)
    _save_yaml(ETC_DIR / 'hosts.yaml', data)
    _save_config_snapshot('hosts', data, 'delete', f'Eliminó host: {hostname}')
    spong_config.load_all()
    return redirect(url_for('config_admin.hosts'))


# ---------------------------------------------------------------------------
# Routes — Groups
# ---------------------------------------------------------------------------

@config_bp.route('/groups')
@_require_config_auth
def groups():
    data = _load_yaml(ETC_DIR / 'groups.yaml')
    return render_template('config_groups.html', groups=data.get('groups', {}))


@config_bp.route('/group/new', methods=['GET', 'POST'])
@config_bp.route('/group/<group_key>/edit', methods=['GET', 'POST'])
@_require_config_auth
def group_edit(group_key=None):
    gdata     = _load_yaml(ETC_DIR / 'groups.yaml')
    gdict     = gdata.setdefault('groups', {})
    hdata     = _load_yaml(ETC_DIR / 'hosts.yaml')
    all_hosts = sorted(hdata.get('hosts', {}).keys())
    error     = None

    if request.method == 'POST':
        new_key = request.form.get('group_key', '').strip()
        if not new_key:
            error = 'La clave del grupo es obligatoria.'
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
    if config_name not in _CONFIG_FILES:
        return Response('Config no válida', 400)

    snapshot_yaml = _get_snapshot_yaml(config_name, ts_key)
    if snapshot_yaml is None:
        return Response('Snapshot no encontrado', 404)

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
    if config_name not in _CONFIG_FILES:
        return Response('Config no válida', 400)

    snapshot_yaml = _get_snapshot_yaml(config_name, ts_key)
    if snapshot_yaml is None:
        return Response('Snapshot no encontrado', 404)

    # Auto-backup current state before overwriting
    current_data = _load_yaml(_CONFIG_FILES[config_name])
    _save_config_snapshot(config_name, current_data, 'pre-restore',
                          f'Backup automático antes de restaurar ({ts_key})')

    snapshot_data = yaml.safe_load(snapshot_yaml) or {}
    _save_yaml(_CONFIG_FILES[config_name], snapshot_data)
    spong_config.load_all()

    return redirect(url_for('config_admin.history'))
