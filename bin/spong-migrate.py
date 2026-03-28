#!/usr/bin/env python3
"""
spong-migrate.py — Convierte configuración SPONG Perl → YAML v3

Uso:
  python3 spong-migrate.py [opciones]

Opciones:
  --conf   <archivo>   Convertir spong.conf  → spong.yaml
  --hosts  <archivo>   Convertir spong.hosts → hosts.yaml
  --groups <archivo>   Convertir spong.groups → groups.yaml
  --all                Busca spong.conf / spong.hosts / spong.groups en el
                       directorio actual y convierte los que existan
  --outdir <dir>       Directorio de salida (default: directorio actual)
  --force              Sobreescribir archivos de salida si ya existen

Ejemplos:
  python3 spong-migrate.py --all
  python3 spong-migrate.py --hosts /etc/spong/spong.hosts --outdir /tmp/
  python3 spong-migrate.py --conf spong.conf --hosts spong.hosts --groups spong.groups
"""

import sys
import os
import re
import argparse
from pathlib import Path


# ---------------------------------------------------------------------------
# Utilidades de parseo Perl
# ---------------------------------------------------------------------------

def _strip_comments(text: str) -> str:
    """Elimina comentarios Perl (#...) fuera de strings."""
    out = []
    for line in text.splitlines():
        # Si la línea es solo comentario, la conservamos vacía para mantener
        # numeración pero la descartamos del parseo
        stripped = re.sub(r'(?<![\'"])#.*$', '', line)
        out.append(stripped)
    return '\n'.join(out)


def _perl_string(s: str) -> str:
    """Desenvuelve comillas simples o dobles de un string Perl."""
    s = s.strip()
    if (s.startswith('"') and s.endswith('"')) or \
       (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    return s


def _perl_scalar(s: str):
    """Convierte un escalar Perl a int/float/str Python."""
    s = s.strip().rstrip(';').strip()
    if (s.startswith('"') and s.endswith('"')) or \
       (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def _perl_list(s: str) -> list:
    """Parsea una lista Perl: ("a", "b", 'c') → ['a','b','c']."""
    s = s.strip()
    # Remover paréntesis externos si los hay
    if s.startswith('(') and s.endswith(')'):
        s = s[1:-1]
    items = []
    # Tokenizar respetando comillas
    for m in re.finditer(r'"([^"]*)"' + r"|'([^']*)'", s):
        items.append(m.group(1) if m.group(1) is not None else m.group(2))
    return items


def _extract_block(text: str, start_pattern: str) -> str | None:
    """
    Busca `start_pattern` en el texto y extrae el bloque balanceado de
    paréntesis que lo sigue. Devuelve el contenido interno (sin los parens
    externos), o None si no se encuentra.
    """
    m = re.search(start_pattern, text, re.IGNORECASE)
    if not m:
        return None
    pos = m.end()
    # Avanzar hasta el primer '('
    while pos < len(text) and text[pos] != '(':
        pos += 1
    if pos >= len(text):
        return None
    depth = 0
    start = pos
    for i in range(pos, len(text)):
        if text[i] == '(':
            depth += 1
        elif text[i] == ')':
            depth -= 1
            if depth == 0:
                return text[start+1:i]
    return None


# ---------------------------------------------------------------------------
# Parseo de spong.hosts → contacts + hosts
# ---------------------------------------------------------------------------

def _parse_inner_hash(text: str) -> dict:
    """
    Parsea el interior de un hash Perl anónimo { key => value, ... }
    Soporta:
      - valores string: 'foo' / "foo"
      - valores array: [ 'a', 'b' ]
    """
    result = {}
    # Remover llaves externas si las hay
    text = text.strip()
    if text.startswith('{') and text.endswith('}'):
        text = text[1:-1]

    # Tokenizar respetando strings y arrays anidados
    i = 0
    tokens = []
    current = ''
    depth = 0
    in_sq = False
    in_dq = False
    while i < len(text):
        c = text[i]
        if in_sq:
            current += c
            if c == "'":
                in_sq = False
        elif in_dq:
            current += c
            if c == '"':
                in_dq = False
        elif c == "'":
            in_sq = True
            current += c
        elif c == '"':
            in_dq = True
            current += c
        elif c in ('(', '[', '{'):
            depth += 1
            current += c
        elif c in (')', ']', '}'):
            depth -= 1
            current += c
        elif c == ',' and depth == 0:
            tokens.append(current.strip())
            current = ''
        else:
            current += c
        i += 1
    if current.strip():
        tokens.append(current.strip())

    # Parsear pares key => value
    for tok in tokens:
        m = re.match(r"(['\"]?)(\w[\w_-]*)(\1)\s*=>\s*(.+)$", tok.strip(), re.DOTALL)
        if not m:
            m = re.match(r"(\w[\w_-]*)\s*=>\s*(.+)$", tok.strip(), re.DOTALL)
            if not m:
                continue
            key = m.group(1)
            val_raw = m.group(2).strip()
        else:
            key = m.group(2)
            val_raw = m.group(4).strip()

        val_raw = val_raw.rstrip(',').strip()

        if val_raw.startswith('['):
            # Array ref
            inner = re.sub(r'^\[|\]$', '', val_raw).strip()
            result[key] = _perl_list('(' + inner + ')')
        elif val_raw.startswith("'") or val_raw.startswith('"'):
            result[key] = _perl_string(val_raw)
        else:
            try:
                result[key] = int(val_raw)
            except ValueError:
                result[key] = val_raw

    return result


def _parse_top_hash(block: str) -> dict[str, dict]:
    """
    Parsea un bloque %HASH = ( 'key' => { ... }, 'key2' => { ... } )
    Devuelve dict{ nombre -> dict_interno }
    """
    result = {}
    text = block.strip()
    i = 0
    while i < len(text):
        # Buscar próxima key (string entre comillas)
        m = re.search(r"(?:^|,)\s*['\"]([^'\"]+)['\"]\s*=>", text[i:], re.DOTALL)
        if not m:
            break
        key = m.group(1)
        # Avanzar hasta después del '=>'
        arrow_end = i + m.end()
        # Buscar el '{' que abre el hash interno
        j = arrow_end
        while j < len(text) and text[j] != '{':
            if text[j] == '#':  # comentario inline
                while j < len(text) and text[j] != '\n':
                    j += 1
            j += 1
        if j >= len(text):
            break
        # Extraer hash interno balanceado
        depth = 0
        start = j
        for k in range(j, len(text)):
            if text[k] == '{':
                depth += 1
            elif text[k] == '}':
                depth -= 1
                if depth == 0:
                    inner = text[start:k+1]
                    result[key] = _parse_inner_hash(inner)
                    i = k + 1
                    break
        else:
            break

    return result


def _strip_perl_comments(text: str) -> str:
    """Elimina líneas de comentario Perl (#) para facilitar el parseo de bloques."""
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith('#'):
            out.append('')  # Conservar número de línea pero vaciar
        else:
            # Eliminar comentarios inline solo fuera de strings
            clean = re.sub(r"(?<!['\"])#[^'\"\n]*$", '', line)
            out.append(clean)
    return '\n'.join(out)


def parse_hosts_file(path: str) -> tuple[dict, dict]:
    """Parsea spong.hosts → (contacts_dict, hosts_dict)"""
    raw = Path(path).read_text(encoding='utf-8', errors='replace')
    text = _strip_perl_comments(raw)

    # Extraer bloque %HUMANS
    humans_block = _extract_block(text, r'%HUMANS\s*=')
    contacts = {}
    if humans_block:
        humans = _parse_top_hash(humans_block)
        for hid, hdata in humans.items():
            contacts[hid] = {
                'name':  hdata.get('name', hid),
                'email': hdata.get('email', f'{hid}@localhost'),
            }

    # Extraer bloque %HOSTS
    hosts_block = _extract_block(text, r'%HOSTS\s*=')
    hosts = {}
    if hosts_block:
        raw_hosts = _parse_top_hash(hosts_block)
        for hname, hdata in raw_hosts.items():
            entry = {}
            if 'services' in hdata:
                entry['services'] = hdata['services']
            if 'contact' in hdata:
                entry['contact'] = hdata['contact']
            if 'ip_addr' in hdata:
                ips = hdata['ip_addr']
                entry['ip_addr'] = ips if isinstance(ips, list) else [ips]
            hosts[hname] = entry

    return contacts, hosts


# ---------------------------------------------------------------------------
# Parseo de spong.groups
# ---------------------------------------------------------------------------

def parse_groups_file(path: str) -> dict:
    """Parsea spong.groups → groups_dict"""
    text = _strip_perl_comments(
        Path(path).read_text(encoding='utf-8', errors='replace')
    )
    groups_block = _extract_block(text, r'%GROUPS\s*=')
    if not groups_block:
        return {}

    groups = {}
    raw = _parse_top_hash(groups_block)
    for gid, gdata in raw.items():
        entry = {
            'name': gdata.get('name', gid),
        }
        if 'members' in gdata:
            members = gdata['members']
            entry['members'] = members if isinstance(members, list) else [members]
        entry['display'] = bool(gdata.get('display', 1))
        entry['compress'] = bool(gdata.get('compress', 1))
        groups[gid] = entry

    return groups


# ---------------------------------------------------------------------------
# Parseo de spong.conf
# ---------------------------------------------------------------------------

def parse_conf_file(path: str) -> dict:
    """Parsea spong.conf → dict con claves del nuevo formato"""
    text = _strip_perl_comments(
        Path(path).read_text(encoding='utf-8', errors='replace')
    )
    cfg = {}

    def _get_scalar(pattern):
        m = re.search(pattern, text)
        if m:
            return _perl_scalar(m.group(1))
        return None

    def _get_hash_val(varname, key):
        k = re.escape(key)
        pattern = r'\$' + varname + r'\{[\'\"' + k + r'[\'\"]\}\s*=\s*([^;]+);'
        # Más simple: buscar literalmente
        pattern2 = r'\$' + re.escape(varname) + r"\{['\"]" + k + r"['\"]\}\s*=\s*([^;]+);"
        m = re.search(pattern2, text)
        if m:
            return _perl_scalar(m.group(1))
        return None

    def _get_array(varname):
        pattern = rf'@{varname}\s*=\s*\(([^)]*)\);'
        m = re.search(pattern, text)
        if m:
            return _perl_list('(' + m.group(1) + ')')
        return None

    # server
    cfg['server'] = {}
    v = _get_scalar(r'\$SPONGSERVER\s*=\s*([^;]+);')
    if v: cfg['server']['host'] = v
    v = _get_scalar(r'\$SPONG_UPDATE_PORT\s*=\s*([^;]+);')
    if v: cfg['server']['update_port'] = v
    v = _get_scalar(r'\$SPONG_QUERY_PORT\s*=\s*([^;]+);')
    if v: cfg['server']['query_port'] = v
    v = _get_scalar(r'\$SPONG_BB_UPDATE_PORT\s*=\s*([^;]+);')
    if v: cfg['server']['bb_port'] = v
    v = _get_scalar(r'\$SPONG_SERVER_ALARM\s*=\s*([^;]+);')
    if v: cfg['server']['alarm_timeout'] = v

    # database
    cfg['database'] = {}
    v = _get_scalar(r'\$SPONGDB\s*=\s*([^;]+);')
    if v: cfg['database']['path'] = v
    v = _get_scalar(r'\$SPONG_ARCHIVE\s*=\s*([^;]+);')
    if v: cfg['database']['archive_path'] = v

    # sleep
    cfg['sleep'] = {}
    v = _get_hash_val('SPONGSLEEP', 'DEFAULT')
    if v is None:
        v = _get_scalar(r'\$SPONGSLEEP\s*=\s*([^;]+);')
    if v: cfg['sleep']['default'] = v
    v = _get_hash_val('SPONGSLEEP', 'spong-network')
    if v: cfg['sleep']['spong-network'] = v
    v = _get_hash_val('SPONGSLEEP', 'spong-client')
    if v: cfg['sleep']['spong-client'] = v
    v = _get_hash_val('SPONGSLEEP', 'spong-server')
    if v: cfg['sleep']['spong-server'] = v

    # network
    cfg['network'] = {}
    v = _get_scalar(r'\$CRIT_WARN_LEVEL\s*=\s*([^;]+);')
    if v: cfg['network']['crit_warn_level'] = v
    v = _get_scalar(r'\$RECHECKSLEEP\s*=\s*([^;]+);')
    if v: cfg['network']['recheck_sleep'] = v

    # checks
    v = _get_scalar(r'\$CHECKS\s*=\s*([^;]+);')
    if v: cfg['checks'] = v

    # thresholds
    cfg['thresholds'] = {'disk': {'warn': {}, 'crit': {}}, 'cpu': {}, 'memory': {}}

    for m in re.finditer(r'\$DFWARN\{[\'"]([^\'"]+)[\'"]\}\s*=\s*(\d+)', text):
        cfg['thresholds']['disk']['warn'][m.group(1)] = int(m.group(2))
    for m in re.finditer(r'\$DFCRIT\{[\'"]([^\'"]+)[\'"]\}\s*=\s*(\d+)', text):
        cfg['thresholds']['disk']['crit'][m.group(1)] = int(m.group(2))

    v = _get_scalar(r'\$CPUWARN\s*=\s*([^;]+);')
    if v: cfg['thresholds']['cpu']['warn'] = v
    v = _get_scalar(r'\$CPUCRIT\s*=\s*([^;]+);')
    if v: cfg['thresholds']['cpu']['crit'] = v
    v = _get_scalar(r'\$MEMWARN\s*=\s*([^;]+);')
    if v: cfg['thresholds']['memory']['warn'] = v
    v = _get_scalar(r'\$MEMCRIT\s*=\s*([^;]+);')
    if v: cfg['thresholds']['memory']['crit'] = v

    # processes
    cfg['processes'] = {}
    procs_crit = _get_array('PROCSCRIT')
    if procs_crit: cfg['processes']['crit'] = procs_crit
    procs_warn = _get_array('PROCSWARN')
    if procs_warn: cfg['processes']['warn'] = procs_warn

    # Limpiar secciones vacías
    for section in list(cfg.keys()):
        if isinstance(cfg[section], dict) and not cfg[section]:
            del cfg[section]

    return cfg


# ---------------------------------------------------------------------------
# Generación de YAML manual (sin librería externa)
# ---------------------------------------------------------------------------

def _yaml_str(s: str) -> str:
    """Formatea un string para YAML: usa comillas si contiene chars especiales."""
    if any(c in str(s) for c in (':', '#', '{', '}', '[', ']', ',', '&', '*',
                                   '?', '|', '-', '<', '>', '=', '!', '%',
                                   '@', '\\', '\n', '"')):
        return f'"{s}"'
    if str(s).strip() == '' or str(s).lower() in ('true', 'false', 'null', 'yes', 'no'):
        return f'"{s}"'
    return str(s)


def _yaml_val(v) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    return _yaml_str(str(v))


def generate_spong_yaml(cfg: dict) -> str:
    lines = [
        '# spong.yaml — Configuración principal de SPONG v3',
        '# Generado por spong-migrate.py desde spong.conf',
        '',
    ]

    srv = cfg.get('server', {})
    lines.append('server:')
    lines.append(f'  host: "{srv.get("host", "spong-server")}"')
    lines.append(f'  update_port: {srv.get("update_port", 1998)}')
    lines.append(f'  query_port:  {srv.get("query_port", 1999)}')
    lines.append(f'  bb_port:     {srv.get("bb_port", 1984)}')
    lines.append(f'  alarm_timeout: {srv.get("alarm_timeout", 10)}')
    lines.append('')

    db = cfg.get('database', {})
    lines.append('database:')
    lines.append(f'  path: "{db.get("path", "/usr/local/spong/var/database")}"')
    lines.append(f'  archive_path: "{db.get("archive_path", "/usr/local/spong/var/archives")}"')
    lines.append('')

    sl = cfg.get('sleep', {})
    lines.append('sleep:')
    lines.append(f'  default:        {sl.get("default", 300)}')
    if 'spong-client' in sl:
        lines.append(f'  spong-client:   {sl["spong-client"]}')
    if 'spong-network' in sl:
        lines.append(f'  spong-network:  {sl["spong-network"]}')
    if 'spong-server' in sl:
        lines.append(f'  spong-server:   {sl["spong-server"]}')
    lines.append('')

    net = cfg.get('network', {})
    lines.append('network:')
    lines.append(f'  crit_warn_level: {net.get("crit_warn_level", 1)}')
    lines.append(f'  recheck_sleep:   {net.get("recheck_sleep", 15)}')
    lines.append('')

    checks = cfg.get('checks', 'disk cpu memory uptime')
    lines.append(f'checks: "{checks}"')
    lines.append('')

    thr = cfg.get('thresholds', {})
    lines.append('thresholds:')

    disk = thr.get('disk', {})
    if disk.get('warn') or disk.get('crit'):
        lines.append('  disk:')
        if disk.get('warn'):
            lines.append('    warn:')
            for k, v in disk['warn'].items():
                lines.append(f'      {_yaml_str(k)}: {v}')
        if disk.get('crit'):
            lines.append('    crit:')
            for k, v in disk['crit'].items():
                lines.append(f'      {_yaml_str(k)}: {v}')
    else:
        lines += ['  disk:', '    warn:', '      ALL: 90', '    crit:', '      ALL: 95']

    cpu = thr.get('cpu', {})
    lines.append('  cpu:')
    lines.append(f'    warn: {cpu.get("warn", 7.0)}')
    lines.append(f'    crit: {cpu.get("crit", 8.0)}')

    mem = thr.get('memory', {})
    lines.append('  memory:')
    lines.append(f'    warn: {mem.get("warn", 90)}')
    lines.append(f'    crit: {mem.get("crit", 95)}')
    lines.append('')

    procs = cfg.get('processes', {})
    if procs:
        lines.append('processes:')
        if procs.get('crit'):
            lines.append('  crit:')
            for p in procs['crit']:
                lines.append(f'    - {p}')
        if procs.get('warn'):
            lines.append('  warn:')
            for p in procs['warn']:
                lines.append(f'    - {p}')
        lines.append('')

    lines += [
        'web:',
        '  auth_user: ""       # vacío = sin autenticación',
        '  auth_password: ""',
        '',
        'cleanup:',
        '  old_service_days: 20',
        '  old_history_days: 30',
    ]

    return '\n'.join(lines) + '\n'


def generate_hosts_yaml(contacts: dict, hosts: dict) -> str:
    lines = [
        '# hosts.yaml — Contactos y hosts monitoreados',
        '# Generado por spong-migrate.py desde spong.hosts',
        '',
        'contacts:',
    ]

    for cid, cdata in contacts.items():
        lines.append(f'  {cid}:')
        lines.append(f'    name: "{cdata.get("name", cid)}"')
        lines.append(f'    email: "{cdata.get("email", cid + "@localhost")}"')

    lines.append('')
    lines.append('hosts:')

    for hname, hdata in hosts.items():
        lines.append(f'  {_yaml_str(hname)}:')
        svc = hdata.get('services', 'ping')
        lines.append(f'    services: "{svc}"')
        contact = hdata.get('contact', '')
        if contact:
            lines.append(f'    contact: "{contact}"')
        ips = hdata.get('ip_addr', [])
        if ips:
            lines.append('    ip_addr:')
            for ip in ips:
                lines.append(f'      - "{ip}"')

    return '\n'.join(lines) + '\n'


def generate_groups_yaml(groups: dict) -> str:
    lines = [
        '# groups.yaml — Grupos de hosts',
        '# Generado por spong-migrate.py desde spong.groups',
        '',
        'groups:',
    ]

    for gid, gdata in groups.items():
        lines.append(f'  {gid}:')
        lines.append(f'    name: "{gdata.get("name", gid)}"')
        members = gdata.get('members', [])
        if members:
            lines.append('    members:')
            for m in members:
                lines.append(f'      - {_yaml_str(m)}')
        lines.append(f'    display: {str(gdata.get("display", True)).lower()}')
        lines.append(f'    compress: {str(gdata.get("compress", True)).lower()}')

    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _write_output(content: str, outpath: Path, force: bool) -> bool:
    if outpath.exists() and not force:
        print(f'  SKIP: {outpath} ya existe (usar --force para sobreescribir)')
        return False
    outpath.write_text(content, encoding='utf-8')
    print(f'  OK:   {outpath}')
    return True


def main():
    parser = argparse.ArgumentParser(
        description='Convierte configuración SPONG Perl → YAML v3',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--conf',   metavar='ARCHIVO', help='spong.conf a convertir')
    parser.add_argument('--hosts',  metavar='ARCHIVO', help='spong.hosts a convertir')
    parser.add_argument('--groups', metavar='ARCHIVO', help='spong.groups a convertir')
    parser.add_argument('--all',    action='store_true',
                        help='Busca spong.conf/hosts/groups en el directorio actual')
    parser.add_argument('--outdir', metavar='DIR', default='.',
                        help='Directorio de salida (default: directorio actual)')
    parser.add_argument('--force',  action='store_true',
                        help='Sobreescribir archivos existentes')
    args = parser.parse_args()

    if not any([args.conf, args.hosts, args.groups, args.all]):
        parser.print_help()
        sys.exit(0)

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    conf_file   = args.conf
    hosts_file  = args.hosts
    groups_file = args.groups

    if args.all:
        cwd = Path('.')
        if not conf_file   and (cwd / 'spong.conf').exists():
            conf_file = str(cwd / 'spong.conf')
        if not hosts_file  and (cwd / 'spong.hosts').exists():
            hosts_file = str(cwd / 'spong.hosts')
        if not groups_file and (cwd / 'spong.groups').exists():
            groups_file = str(cwd / 'spong.groups')

    converted = 0

    if conf_file:
        print(f'\nConvirtiendo {conf_file} ...')
        try:
            cfg = parse_conf_file(conf_file)
            yaml_text = generate_spong_yaml(cfg)
            if _write_output(yaml_text, outdir / 'spong.yaml', args.force):
                converted += 1
        except Exception as e:
            print(f'  ERROR: {e}')

    if hosts_file:
        print(f'\nConvirtiendo {hosts_file} ...')
        try:
            contacts, hosts = parse_hosts_file(hosts_file)
            yaml_text = generate_hosts_yaml(contacts, hosts)
            if _write_output(yaml_text, outdir / 'hosts.yaml', args.force):
                converted += 1
            print(f'        {len(contacts)} contactos, {len(hosts)} hosts')
        except Exception as e:
            print(f'  ERROR: {e}')

    if groups_file:
        print(f'\nConvirtiendo {groups_file} ...')
        try:
            groups = parse_groups_file(groups_file)
            yaml_text = generate_groups_yaml(groups)
            if _write_output(yaml_text, outdir / 'groups.yaml', args.force):
                converted += 1
            print(f'        {len(groups)} grupos')
        except Exception as e:
            print(f'  ERROR: {e}')

    if converted == 0 and not (conf_file or hosts_file or groups_file):
        print('No se encontraron archivos para convertir.')
        print('Asegurate de estar en el directorio con spong.conf / spong.hosts / spong.groups')
        print('o especificá los archivos con --conf, --hosts, --groups')
        sys.exit(1)

    print(f'\n{converted} archivo(s) generado(s) en {outdir}/')


if __name__ == '__main__':
    main()
