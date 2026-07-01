"""Integración con SGT (Sistema de Gestión de Tareas) UNSL.

Permite que desde la vista /problems se cree un ticket en SGT con un
clic. La integración se controla con el bloque `sgt:` de spong.yaml:

    sgt:
      enabled: true | false             # switch global. default false.
      base_url: https://sgt.unsl.edu.ar
      token: "<api token de SGT>"
      categoria_id: <id de la categoría que recibe estos tickets>
      facultad_id: <id de la facultad por default (ej. Rectorado)>
      verify_tls: true | false | <path> # default true. false sólo si el
                                        # upstream sirve un chain
                                        # incompleto.
      prioridad_por_color:              # mapeo opcional spong→sgt
        red: ALTA
        yellow: MEDIA
        purple: ALTA

Si `enabled` es False (o falta el bloque), la integración queda
apagada y la ruta /sgt-ticket devuelve 404.

La dedup se hace con un archivo JSON en var/sgt_links.json mapping
"host\\x00service" → {ticket_numero, url, creado_iso, creado_por}.
Mientras el problema en spong siga activo y el ticket no sea cerrado
desde SGT, el botón se transforma en "→ SGT-N".
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import requests

import spong.config as config

log = logging.getLogger(__name__)

_VAR_DIR = Path(__file__).resolve().parent.parent / "var"
_LINKS_PATH = _VAR_DIR / "sgt_links.json"
_LOCK_PATH = _VAR_DIR / "sgt_links.lock"

_DEFAULT_PRIO_MAP = {"red": "ALTA", "yellow": "MEDIA", "purple": "ALTA"}

# El web (crear_ticket/borrar_link) y el proceso spong-sgt-sync (sync_once)
# mutan sgt_links.json en procesos distintos. Un lock de hilos no alcanza:
# usamos flock sobre un archivo de lock aparte (el de datos se reemplaza con
# os.replace, así que bloquear su FD no serviría) más un lock de hilos para
# ordenar los hilos dentro del propio proceso web.
_thread_lock = threading.Lock()


@contextmanager
def _links_locked():
    """Sección crítica para el read-modify-write de sgt_links.json.

    Serializa entre hilos (web) y entre procesos (web ↔ spong-sgt-sync).
    """
    _VAR_DIR.mkdir(parents=True, exist_ok=True)
    with _thread_lock:
        with open(_LOCK_PATH, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def enabled() -> bool:
    return bool(config.get("sgt.enabled", False))


def _verify_param():
    v = config.get("sgt.verify_tls", True)
    if isinstance(v, str):
        return v
    return bool(v)


def _key(host: str, service: str) -> str:
    return f"{host}\x00{service}"


def _load_links() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_LINKS_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as e:
        log.warning("No pude leer %s: %s", _LINKS_PATH, e)
        return {}


def _save_links(data: dict[str, dict[str, Any]]) -> None:
    """Escritura atómica y durable. Los llamadores toman `_links_locked()`.

    Usa un temporal de nombre único (no un `.json.tmp` fijo que dos escritores
    pisarían), lo fsync-ea y hace os.replace. Propaga OSError para que el
    llamador decida (no se traga el error en silencio: perdería el link).
    """
    _VAR_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(dir=str(_VAR_DIR),
                                   prefix=".sgt_links.", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmpname, _LINKS_PATH)
    except OSError:
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise


def link_for(host: str, service: str) -> dict[str, Any] | None:
    return _load_links().get(_key(host, service))


def find_link(links: dict[str, dict[str, Any]], host: str, service: str) -> dict[str, Any] | None:
    """Versión barata de link_for() cuando ya tenés cargado el dict."""
    return links.get(_key(host, service))


def all_links() -> dict[str, dict[str, Any]]:
    """Snapshot del JSON de links. Útil para iterar sin pegarle al disco varias veces."""
    return _load_links()


def ticket_url(numero) -> str:
    """Reconstruye la URL del ticket SGT a partir de su número.

    Devuelve "" si falta base_url o numero. Útil para renderizar referencias
    históricas (var/database/<host>/history/current) cuando el link ya fue
    limpiado de sgt_links.json.
    """
    base = (config.get("sgt.base_url") or "").rstrip("/")
    if not base or not numero:
        return ""
    return f"{base}/tickets/{numero}"


def ticket_display(numero) -> str:
    if numero in (None, ""):
        return ""
    return f"SGT-{numero}"


def links_for_issues(issues: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not issues:
        return {}
    data = _load_links()
    out = {}
    for issue in issues:
        k = _key(issue["host"], issue["service"])
        if k in data:
            out[k] = data[k]
    return out


def _build_payload(host: str, service: str, color: str, summary: str) -> dict[str, Any]:
    categoria_id = config.get("sgt.categoria_id")
    facultad_id = config.get("sgt.facultad_id")
    prio_map = config.get("sgt.prioridad_por_color", _DEFAULT_PRIO_MAP) or _DEFAULT_PRIO_MAP
    prio = prio_map.get(color, "MEDIA")
    descripcion = (
        f"Reportado por SPONG.\n"
        f"Host: {host}\n"
        f"Servicio: {service}\n"
        f"Color: {color}\n"
        f"Resumen: {summary or '(sin resumen)'}\n"
    )
    return {
        "titulo": f"[SPONG {color}] {host}/{service}",
        "descripcion": descripcion,
        "categoria": categoria_id,
        "facultad": facultad_id,
        "prioridad": prio,
        "edificio_libre": f"host:{host}",
    }


class SgtError(Exception):
    """Cualquier falla del proceso de creación de ticket."""


def crear_ticket(*, host: str, service: str, color: str, summary: str,
                 creado_por: str = "") -> dict[str, Any]:
    """POST /api/v1/tickets/ contra SGT. Idempotente sobre (host, service):
    si ya hay link, devuelve el existente sin crear otro."""
    if not enabled():
        raise SgtError("Integración SGT deshabilitada (sgt.enabled=False).")

    base_url = (config.get("sgt.base_url") or "").rstrip("/")
    token = config.get("sgt.token") or ""
    categoria_id = config.get("sgt.categoria_id")
    facultad_id = config.get("sgt.facultad_id")
    if not base_url or not token or not categoria_id or not facultad_id:
        raise SgtError("Falta config: revisá sgt.base_url, sgt.token, sgt.categoria_id, sgt.facultad_id.")

    payload = _build_payload(host, service, color, summary)

    # Todo el dedup+POST+save va bajo el lock: sin él, dos clics simultáneos
    # pasarían ambos el chequeo de existencia y crearían dos tickets en SGT.
    with _links_locked():
        existing = find_link(_load_links(), host, service)
        if existing:
            return existing

        try:
            r = requests.post(
                f"{base_url}/api/v1/tickets/",
                headers={
                    "Authorization": f"Token {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
                verify=_verify_param(),
            )
        except requests.RequestException as e:
            raise SgtError(f"Error de red contra SGT: {e}") from e

        if r.status_code != 201:
            snippet = (r.text or "")[:500]
            raise SgtError(f"SGT respondió HTTP {r.status_code}: {snippet}")
        body = r.json()
        numero = body.get("numero")
        display = body.get("numero_display") or f"SGT-{numero}"

        link = {
            "ticket_numero": numero,
            "ticket_display": display,
            "url": f"{base_url}/tickets/{numero}",
            "creado_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "creado_por": creado_por,
            "host": host,
            "service": service,
            "color": color,
        }
        data = _load_links()
        data[_key(host, service)] = link
        try:
            _save_links(data)
        except OSError as e:
            # El ticket ya existe en SGT; no podemos deshacerlo. Avisamos fuerte
            # pero devolvemos el link para no ocultarle el número al usuario.
            log.error("Ticket %s creado en SGT pero no pude persistir el link "
                      "(%s/%s): %s", display, host, service, e)

    # Referencia permanente en el historial del host. Sobrevive a la
    # limpieza de sgt_links.json (cierre del ticket, sync, borrado manual)
    # para que la traza del ticket quede siempre visible en /host/<x> y
    # /history. event_type="sgt" + color=str(numero) — load_history mantiene
    # estos registros sin pasarlos por el dedup de status.
    try:
        from spong import database
        from spong.models import HistoryEntry
        summary = f"Ticket {display} creado"
        if creado_por:
            summary += f" por {creado_por}"
        database.append_history(host, HistoryEntry(
            event_type="sgt",
            timestamp=time.time(),
            service=service,
            color=str(numero),
            summary=summary,
            user=creado_por,
        ))
    except Exception as e:
        log.warning("No pude escribir historial SGT para %s/%s: %s",
                    host, service, e)

    return link


def borrar_link(host: str, service: str) -> bool:
    """Borra el link para un par (host, service). True si había algo."""
    k = _key(host, service)
    with _links_locked():
        data = _load_links()
        if k in data:
            del data[k]
            _save_links(data)
            return True
    return False

# ============================================================================
# Sincronización periódica spong↔SGT.
# ============================================================================
# Llamado por /usr/local/sbin/spong-sgt-sync (systemd timer cada 5 min) para
# cerrar el lazo entre los dos sistemas:
#   - Si el servicio volvió a verde, mandamos auto-resolver al ticket en SGT
#     y borramos el link local.
#   - Si el ticket fue cerrado/cancelado en SGT, borramos el link local.
# Si la integración está deshabilitada (sgt.enabled=False), la función sale
# inmediatamente sin hacer nada.


def _color_actual(host: str, service: str) -> str | None:
    """Color actual del servicio en spong, o None si no existe el archivo."""
    # Lazy import — spong.database depende de muchas cosas y no queremos
    # cargarlo a menos que estemos sincronizando.
    from spong import database
    svc = database.load_service(host, service)
    return svc.color if svc else None


def _get_ticket(numero: int) -> dict | None:
    """GET /api/v1/tickets/N/ — devuelve dict o None si 404/error."""
    base_url = (config.get("sgt.base_url") or "").rstrip("/")
    token = config.get("sgt.token") or ""
    try:
        r = requests.get(
            f"{base_url}/api/v1/tickets/{numero}/",
            headers={"Authorization": f"Token {token}"},
            timeout=10,
            verify=_verify_param(),
        )
    except requests.RequestException as e:
        log.warning("GET ticket %s falló: %s", numero, e)
        return None
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return None
    log.warning("GET ticket %s -> HTTP %s: %s", numero, r.status_code, (r.text or "")[:200])
    return None


def _post_auto_resolver(numero: int) -> bool:
    """POST /api/v1/tickets/N/auto-resolver/. True si éxito."""
    base_url = (config.get("sgt.base_url") or "").rstrip("/")
    token = config.get("sgt.token") or ""
    try:
        r = requests.post(
            f"{base_url}/api/v1/tickets/{numero}/auto-resolver/",
            headers={"Authorization": f"Token {token}",
                     "Content-Type": "application/json"},
            json={},
            timeout=10,
            verify=_verify_param(),
        )
    except requests.RequestException as e:
        log.warning("POST auto-resolver %s falló: %s", numero, e)
        return False
    if r.status_code in (200, 201):
        return True
    log.warning("POST auto-resolver %s -> HTTP %s: %s",
                numero, r.status_code, (r.text or "")[:200])
    return False


def sync_once() -> dict[str, int]:
    """Reconcilia sgt_links.json con el estado actual de spong y de SGT.

    Para cada link guardado:
      - Si el servicio NO está en rojo/amarillo/púrpura → manda auto-resolver
        a SGT y borra el link.
      - Si está en rojo/amarillo/púrpura pero el ticket está CERRADO o
        CANCELADO en SGT → borra el link (deja que un humano cree uno nuevo
        manualmente si quiere).

    Devuelve un dict con contadores: {resueltos, ya_terminados, sin_cambio}.
    """
    counts = {"resueltos": 0, "ya_terminados": 0, "sin_cambio": 0, "errores": 0}
    if not enabled():
        return counts

    data = _load_links()
    if not data:
        return counts

    # Fase de red SIN lock: decidimos qué links borrar (cada _get_ticket es una
    # llamada HTTP; no queremos bloquear la creación de tickets del web durante
    # toda la reconciliación). Los borrados se aplican después bajo lock con una
    # relectura fresca, para no pisar un link recién creado en paralelo.
    to_delete: list[str] = []
    estados_terminales = {"CERRADO", "CANCELADO"}
    estados_resueltos = estados_terminales | {"RESUELTO"}
    for k, link in list(data.items()):
        host = link.get("host", "")
        service = link.get("service", "")
        numero = link.get("ticket_numero")
        if not (host and service and numero):
            log.warning("Link malformado en sgt_links.json, lo borro: %r", k)
            to_delete.append(k); continue

        color = _color_actual(host, service)
        # Verde, clear, blue, sin archivo → problema desapareció en el monitor.
        problema_activo = color in ("red", "yellow", "purple")

        if not problema_activo:
            # Auto-resolver en SGT (si todavía no está terminado).
            ticket = _get_ticket(numero)
            if ticket is None:
                counts["errores"] += 1
                continue
            if ticket.get("estado") in estados_resueltos:
                # Ya resuelto/cerrado/cancelado, sólo limpieza local.
                counts["ya_terminados"] += 1
            else:
                if _post_auto_resolver(numero):
                    counts["resueltos"] += 1
                else:
                    counts["errores"] += 1
                    continue
            to_delete.append(k)
        else:
            # Problema activo: verificamos si el ticket fue cerrado/cancelado
            # en SGT (humano decidió descartarlo). Si es así, soltamos el link.
            ticket = _get_ticket(numero)
            if ticket is None:
                counts["errores"] += 1
                continue
            if ticket.get("estado") in estados_terminales:
                to_delete.append(k)
                counts["ya_terminados"] += 1
            else:
                counts["sin_cambio"] += 1

    if to_delete:
        with _links_locked():
            fresh = _load_links()
            for k in to_delete:
                fresh.pop(k, None)
            _save_links(fresh)
    return counts
