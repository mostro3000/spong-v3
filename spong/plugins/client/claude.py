"""Client check: estado de Claude Code (CLI) en el host, sin gastar tokens.

Responde tres preguntas por cada cuenta configurada:

- ¿Está Claude Code instalado y arranca?  ``claude --version`` (instantáneo,
  no toca la config).
- ¿Hay que rehacer login?  Lee ``~/.claude/.credentials.json`` (lo que escribe
  ``claude auth login``): ``refreshTokenExpiresAt`` es la fecha en que la
  propia CLI exige ``/login`` de nuevo ("Your login expires in N days");
  ``expiresAt`` es el token de acceso (~8 h) que se renueva solo al usar la
  CLI, así que vencido NO es problema, solo impide consultar la API.
- ¿Falta de pago / límite de uso?  Con ese token consulta los endpoints
  ``api/oauth/profile`` (``subscription_status``) y ``api/oauth/usage``
  (% usado de la ventana de 5 h y semanal) que la CLI usa para ``/usage``.
  No pasan por ``/v1/messages``: no consumen tokens ni cuota, y no hace
  falta API key (cuenta Pro/Max con login de claude.ai).

Nunca refresca tokens ni ejecuta ``claude -p``: refrescar desde afuera puede
invalidar la sesión de la CLI, y un prompt gastaría cuota. Tampoco corre
``claude auth status`` porque escribe ``.claude.json`` en el dir de config
(como root, en el home de otro usuario, dejaría archivos de root).

Config en spong.yaml (``thresholds.claude``):

    users: "root"          # usuarios cuyo ~/.claude se verifica, o rutas
                           # absolutas a un CLAUDE_CONFIG_DIR; default: el
                           # usuario que corre spong-client
    usage_warn: 80         # % de una ventana de uso -> amarillo
    usage_crit: 100        # % -> rojo (Claude Code bloqueado hasta el reset)
    refresh_warn_days: 3   # amarillo si el login vence en menos de N días
    interval: 600          # segundos entre consultas a api.anthropic.com
                           # (entre medio se reusa la última respuesta)

``commands.claude`` fija el binario; si no, se busca ``~/.local/bin/claude``
del usuario (instalador nativo) y después en el PATH. Habilitar agregando
``claude`` a ``checks:``; solo Linux (en macOS las credenciales van al
Keychain, no a un archivo).
"""

from __future__ import annotations

import json
import logging
import os
import pwd
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from ... import __version__, config
from ...safe_exec import safe_exec
from ...status_sender import send_status

log = logging.getLogger(__name__)

_SEV = {"green": 0, "yellow": 1, "red": 2}

_API = "https://api.anthropic.com/api/oauth/"
_HTTP_TIMEOUT = 15
_DAY = 86400
_CACHE_FILE = "claude_check.json"

# Ventanas de uso de api/oauth/usage -> etiqueta corta. Las que vienen en
# null (p. ej. semana Opus en planes sin ese límite) se ignoran.
_WINDOWS = (
    ("five_hour", "5h"),
    ("seven_day", "semana"),
    ("seven_day_opus", "semana Opus"),
    ("seven_day_sonnet", "semana Sonnet"),
)

_PLAN = {
    "claude_max": "Max",
    "claude_pro": "Pro",
    "claude_team": "Team",
    "claude_enterprise": "Enterprise",
}

# subscription_status (estilo Stripe) que no son problema de pago.
_SUB_OK = {"active", "trialing"}

_VERSION_RE = re.compile(r"\d+\.\d+[\w.\-]*")
_TIER_MULT_RE = re.compile(r"_(\d+x)$")


def _worse(a: str, b: str) -> str:
    return b if _SEV[b] > _SEV[a] else a


# --- helpers de tiempo / formato ---------------------------------------------

def _ms(value) -> float | None:
    """Epoch en milisegundos (como guarda la CLI) -> segundos."""
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    return None


def _iso(value) -> float | None:
    """``2026-09-15T23:10:00.824335+00:00`` -> epoch. None si no parsea."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _fmt(ts: float | None, fmt: str = "%d/%m %H:%M") -> str:
    if ts is None:
        return "?"
    return datetime.fromtimestamp(ts).strftime(fmt)


def _rel(seconds: float) -> str:
    seconds = abs(seconds)
    if seconds < 3600:
        return f"{seconds / 60:.0f} min"
    if seconds < 2 * _DAY:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / _DAY:.1f} días"


def _when(ts: float, now: float) -> str:
    """'vence 2026-09-30 02:37 (en 14.4 días)' / '(hace 3.0 h)'."""
    rel = f"hace {_rel(now - ts)}" if ts <= now else f"en {_rel(ts - now)}"
    return f"vence {_fmt(ts, '%Y-%m-%d %H:%M')} ({rel})"


def _kv(label: str, value: str) -> str:
    return f"{label + ':':<14}{value}"


# --- recolección (I/O) -------------------------------------------------------

def _accounts() -> list[tuple[str, Path | None]]:
    """[(etiqueta, config_dir)] según thresholds.claude.users.

    Una entrada que empieza con ``/`` se toma como CLAUDE_CONFIG_DIR literal.
    Usuario inexistente -> config_dir None (se reporta rojo).
    """
    raw = config.get_threshold("claude", "users", "")
    if isinstance(raw, (list, tuple)):
        entries = [str(x) for x in raw]
    else:
        entries = str(raw or "").split()
    if not entries:
        entries = [pwd.getpwuid(os.getuid()).pw_name]
    accounts: list[tuple[str, Path | None]] = []
    for entry in entries:
        if entry.startswith("/"):
            accounts.append((entry, Path(entry)))
            continue
        try:
            home = pwd.getpwnam(entry).pw_dir
        except KeyError:
            accounts.append((entry, None))
            continue
        accounts.append((entry, Path(home) / ".claude"))
    return accounts


def _find_binary(cfg_dir: Path | None) -> str | None:
    cmd = config.get_command("claude", "")
    if cmd:
        return cmd
    if cfg_dir is not None:
        native = cfg_dir.parent / ".local" / "bin" / "claude"
        if os.access(native, os.X_OK):
            return str(native)
    path = os.environ.get("PATH", "") + ":/usr/local/bin:/usr/bin"
    return shutil.which("claude", path=path)


def _read_credentials(cfg_dir: Path) -> dict | None:
    """Metadatos del login de claude.ai. None = sin login.

    El token solo se usa en memoria para las consultas; nunca se loguea ni
    se guarda en el caché.
    """
    path = cfg_dir / ".credentials.json"
    try:
        data = json.loads(path.read_text())
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        return {"error": f"{path}: {e}"}
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None
    return {
        "token": oauth["accessToken"],
        "expires_at": _ms(oauth.get("expiresAt")),
        "refresh_expires_at": _ms(oauth.get("refreshTokenExpiresAt")),
        "subscription_type": oauth.get("subscriptionType"),
        "rate_limit_tier": oauth.get("rateLimitTier"),
        "mtime": mtime,
    }


def _api_get(endpoint: str, token: str) -> dict:
    """GET api/oauth/<endpoint> -> {status, data, error}. status None = sin red."""
    req = urllib.request.Request(_API + endpoint, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json",
        "User-Agent": f"spong-client/{__version__}",
    })
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            return {"status": resp.status, "data": json.loads(resp.read().decode()), "error": None}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        try:
            msg = json.loads(body).get("error", {}).get("message") or body
        except (ValueError, AttributeError):
            msg = body
        return {"status": e.code, "data": None, "error": msg}
    except Exception as e:  # red caída, DNS, timeout, JSON roto...
        log.warning("claude check: %s: %s", endpoint, e)
        return {"status": None, "data": None, "error": str(e)}


def _cache_path() -> Path:
    return config.tmp_path() / _CACHE_FILE


def _load_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(cache, f)
    except OSError as e:
        log.warning("claude check: no se pudo escribir %s: %s", path, e)


def _query(cfg_key: str, creds: dict, now: float, interval: int, cache: dict) -> dict:
    """Perfil + uso, reusando la respuesta anterior durante ``interval`` s.

    Se cachea toda respuesta HTTP (incluidos 401/403: son definitivos hasta
    que cambie el login); los errores de red no, para reintentar al ciclo
    siguiente. Un cambio en .credentials.json (re-login, refresh) invalida.
    """
    entry = cache.get(cfg_key)
    if (isinstance(entry, dict) and entry.get("creds_mtime") == creds["mtime"]
            and now - float(entry.get("ts", 0)) < interval):
        return entry
    profile = _api_get("profile", creds["token"])
    usage = _api_get("usage", creds["token"])
    entry = {"ts": now, "creds_mtime": creds["mtime"], "profile": profile, "usage": usage}
    if profile["status"] is not None and usage["status"] is not None:
        cache[cfg_key] = entry
    else:
        cache.pop(cfg_key, None)
    return entry


def _collect(label: str, cfg_dir: Path | None, now: float, interval: int,
             cache: dict) -> dict:
    info: dict = {
        "label": label, "cfg_dir": str(cfg_dir) if cfg_dir else None,
        "binary": None, "version": None,
        "creds": None, "profile": None, "usage": None, "queried_at": None,
    }
    info["binary"] = _find_binary(cfg_dir)
    if info["binary"]:
        out = "".join(safe_exec(f"{info['binary']} --version", timeout=20))
        m = _VERSION_RE.match(out.strip())
        info["version"] = m.group(0) if m else None
    if cfg_dir is None:
        return info
    creds = _read_credentials(cfg_dir)
    info["creds"] = creds
    if not creds or creds.get("error"):
        return info
    refresh_ok = creds["refresh_expires_at"] is None or creds["refresh_expires_at"] > now
    access_ok = creds["expires_at"] is None or creds["expires_at"] > now
    if refresh_ok and access_ok:
        q = _query(str(cfg_dir), creds, now, interval, cache)
        info["profile"], info["usage"], info["queried_at"] = q["profile"], q["usage"], q["ts"]
    return info


# --- evaluación (pura) -------------------------------------------------------

def _eval_http(resp: dict, what: str) -> tuple[str, str | None]:
    """Clasifica una respuesta no-200 -> (color, issue). ('green', None) si 200."""
    status = resp["status"]
    if status == 200 and isinstance(resp["data"], dict):
        return "green", None
    if status is None:
        return "yellow", f"sin respuesta de api.anthropic.com ({resp['error']})"
    if status == 401:
        return "red", "sesión rechazada por Anthropic (401): rehacer login"
    if status in (402, 403):
        return "red", f"acceso denegado ({status}) al consultar {what}: ¿suscripción vencida?"
    return "yellow", f"{what}: HTTP {status} ({resp['error']})"


def _eval_profile(resp: dict, creds: dict, lines: list[str],
                  issues: list[str], oks: list[str]) -> str:
    color, issue = _eval_http(resp, "perfil")
    if issue:
        issues.append(issue)
        return color
    org = resp["data"].get("organization") or {}
    acct = resp["data"].get("account") or {}
    org_type = org.get("organization_type") or ""
    plan = _PLAN.get(org_type, org_type or creds.get("subscription_type") or "plan ?")
    tier = org.get("rate_limit_tier") or creds.get("rate_limit_tier") or ""
    m = _TIER_MULT_RE.search(tier)
    plan_label = f"{plan} {m.group(1)}" if m else plan
    status = org.get("subscription_status")
    invoice = org.get("payment_auth_hosted_invoice_url")

    lines.append(_kv("cuenta", f"{acct.get('email') or '?'} — org \"{org.get('name') or '?'}\""
                     f" ({org_type or '?'}{', ' + tier if tier else ''})"))
    lines.append(_kv("suscripción", f"{status or '?'}"
                     f"{' (' + org['billing_type'] + ')' if org.get('billing_type') else ''}"))
    if invoice:
        lines.append(_kv("factura", f"pendiente de autorización: {invoice}"))

    if status and status not in _SUB_OK:
        issues.append(f"suscripción {status}: ¿falta de pago?")
        return "red"
    if invoice:
        issues.append("pago pendiente de autorización")
        return "red"
    if org_type in ("claude_max", "claude_pro") and not (
            acct.get("has_claude_max") or acct.get("has_claude_pro")):
        issues.append("sin plan Pro/Max activo")
        return "red"
    oks.append(f"{plan_label} ok")
    return "green"


def _eval_usage(resp: dict, warn: float, crit: float, lines: list[str],
                issues: list[str], oks: list[str]) -> str:
    color, issue = _eval_http(resp, "uso")
    if issue:
        issues.append(issue)
        return color
    data = resp["data"]
    found = False
    for key, label in _WINDOWS:
        win = data.get(key)
        if not isinstance(win, dict) or win.get("utilization") is None:
            continue
        found = True
        pct = float(win["utilization"])
        resets = _iso(win.get("resets_at"))
        lines.append(_kv(f"uso {label}", f"{pct:.0f}% (resetea {_fmt(resets)})"))
        locked = win.get("locked_reason")
        if locked:
            color = _worse(color, "red")
            issues.append(f"{label} bloqueado: {locked}")
        elif pct >= crit:
            color = _worse(color, "red")
            issues.append(f"límite {label} alcanzado ({pct:.0f}%, resetea {_fmt(resets)})")
        elif pct >= warn:
            color = _worse(color, "yellow")
            issues.append(f"{label} al {pct:.0f}% (resetea {_fmt(resets)})")
        else:
            oks.append(f"{label} {pct:.0f}%")
    if not found:
        lines.append(_kv("uso", "sin ventanas en la respuesta"))
    extra = data.get("extra_usage") or {}
    if extra.get("is_enabled") and extra.get("spend_limit_reached"):
        color = _worse(color, "yellow")
        issues.append("límite de uso extra alcanzado")
    return color


def _evaluate(info: dict, now: float, warn: float, crit: float,
              refresh_warn_days: float) -> tuple[str, str, str]:
    """Pure: info recolectada -> (color, summary de una línea, detalle).

    Summary verde: "Max 5x ok · 5h 6% · semana 1% · login ok hasta 30/09 · v2.1.272".
    Con problemas: "; ".join(problemas + oks) y la versión al final.
    """
    color = "green"
    issues: list[str] = []
    oks: list[str] = []
    tail: list[str] = []  # notas de login/versión, siempre al final del summary
    lines: list[str] = []

    def done(final: str) -> tuple[str, str, str]:
        # dict.fromkeys dedupea manteniendo orden (401/sin red salen igual
        # para perfil y uso)
        parts = list(dict.fromkeys(issues + oks + tail if issues else oks + tail))
        summary = ("; " if issues else " · ").join(parts) or "ok"
        return final, summary, "\n".join(lines)

    if not info["binary"]:
        color = "red"
        issues.append("claude no instalado")
    elif not info["version"]:
        color = "red"
        issues.append(f"{info['binary']} no arranca")
        lines.append(_kv("binario", f"{info['binary']} (--version falló)"))
    else:
        tail.append(f"v{info['version']}")
        lines.append(_kv("binario", f"{info['binary']} ({info['version']})"))

    if info["cfg_dir"] is None:
        issues.append(f"usuario {info['label']} inexistente")
        return done("red")
    creds = info["creds"]
    if creds is None:
        issues.append("sin login: correr `claude auth login`")
        return done("red")
    if creds.get("error"):
        issues.append(f"credenciales ilegibles ({creds['error']})")
        return done(_worse(color, "yellow"))

    refresh = creds["refresh_expires_at"]
    access = creds["expires_at"]
    if refresh is not None:
        lines.append(_kv("login", _when(refresh, now)))
    if access is not None:
        lines.append(_kv("token acceso", _when(access, now)))

    if refresh is not None and refresh <= now:
        issues.append(f"login vencido el {_fmt(refresh)}: rehacer `claude auth login`")
        return done("red")
    if refresh is not None and refresh - now < refresh_warn_days * _DAY:
        color = _worse(color, "yellow")
        issues.append(f"login vence en {_rel(refresh - now)} ({_fmt(refresh)}): correr /login")
    else:
        tail.insert(0, f"login ok hasta {_fmt(refresh, '%d/%m')}" if refresh else "login ok")

    if access is not None and access <= now:
        lines.append(_kv("uso", "sin consultar: token de acceso vencido "
                                "(se renueva solo al usar claude)"))
        oks.append(f"uso s/d (token de acceso vencido hace {_rel(now - access)})")
        return done(color)

    if info["profile"] is None:
        return done(color)
    color = _worse(color, _eval_profile(info["profile"], creds, lines, issues, oks))
    color = _worse(color, _eval_usage(info["usage"], warn, crit, lines, issues, oks))
    if info["queried_at"]:
        lines.append(_kv("consultado", _fmt(info["queried_at"], "%Y-%m-%d %H:%M:%S")))
    return done(color)


def check_claude(hostname: str) -> None:
    warn = float(config.get_threshold("claude", "usage_warn", 80))
    crit = float(config.get_threshold("claude", "usage_crit", 100))
    refresh_warn_days = float(config.get_threshold("claude", "refresh_warn_days", 3))
    interval = int(config.get_threshold("claude", "interval", 600))

    now = time.time()
    cache = _load_cache()
    results: list[tuple[dict, str, str, str]] = []
    for label, cfg_dir in _accounts():
        info = _collect(label, cfg_dir, now, interval, cache)
        color, summary, detail = _evaluate(info, now, warn, crit, refresh_warn_days)
        results.append((info, color, summary, detail))
    _save_cache(cache)

    color = "green"
    for _, c, _, _ in results:
        color = _worse(color, c)
    if len(results) == 1:
        summary = results[0][2]
    else:
        summary = " | ".join(f"{info['label']}: {s}" for info, _, s, _ in results)
    message = "\n\n".join(
        f"== {info['label']}"
        f"{'' if info['cfg_dir'] == info['label'] else ' (' + (info['cfg_dir'] or 'usuario inexistente') + ')'}"
        f" ==\n{detail}"
        for info, _, _, detail in results
    )
    send_status(hostname, "claude", color, summary, message)
