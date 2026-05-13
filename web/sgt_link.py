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

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import requests

import spong.config as config

log = logging.getLogger(__name__)

_VAR_DIR = Path(__file__).resolve().parent.parent / "var"
_LINKS_PATH = _VAR_DIR / "sgt_links.json"

_DEFAULT_PRIO_MAP = {"red": "ALTA", "yellow": "MEDIA", "purple": "ALTA"}


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
    _VAR_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _LINKS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    os.replace(tmp, _LINKS_PATH)


def link_for(host: str, service: str) -> dict[str, Any] | None:
    return _load_links().get(_key(host, service))


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
    existing = link_for(host, service)
    if existing:
        return existing

    base_url = (config.get("sgt.base_url") or "").rstrip("/")
    token = config.get("sgt.token") or ""
    categoria_id = config.get("sgt.categoria_id")
    facultad_id = config.get("sgt.facultad_id")
    if not base_url or not token or not categoria_id or not facultad_id:
        raise SgtError("Falta config: revisá sgt.base_url, sgt.token, sgt.categoria_id, sgt.facultad_id.")

    payload = _build_payload(host, service, color, summary)
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
    _save_links(data)
    return link


def borrar_link(host: str, service: str) -> bool:
    """Borra el link para un par (host, service). True si había algo."""
    data = _load_links()
    k = _key(host, service)
    if k in data:
        del data[k]
        _save_links(data)
        return True
    return False
