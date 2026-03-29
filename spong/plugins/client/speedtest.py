"""Client check: velocidad de internet via speedtest (Ookla CLI).

Mide bajada, subida y latencia. Corre el test completo y guarda los resultados.
El test tarda ~20-30s, por eso el TTL del estado es largo (el check_interval
del cliente debería configurarse con un intervalo mayor para este servicio,
o se puede usar el TTL para que no alarme si no se actualiza frecuentemente).

Umbrales (configurables en spong.yaml bajo thresholds.speedtest):
  down_warn: 10   (Mbps)
  down_crit:  5
  up_warn:    5
  up_crit:    2
  ping_warn: 50   (ms)
  ping_crit: 100
"""

import json
import subprocess
import time
from ... import config
from ...status_sender import send_status

_SPEEDTEST_CMD = "speedtest"
_TIMEOUT = 90   # segundos máximos para el test


def check_speedtest(hostname: str) -> None:
    thresholds = config.get("thresholds", {}).get("speedtest", {})
    down_warn  = thresholds.get("down_warn",  10)
    down_crit  = thresholds.get("down_crit",   5)
    up_warn    = thresholds.get("up_warn",     5)
    up_crit    = thresholds.get("up_crit",     2)
    ping_warn  = thresholds.get("ping_warn",  50)
    ping_crit  = thresholds.get("ping_crit", 100)

    try:
        result = subprocess.run(
            [_SPEEDTEST_CMD, "--format=json", "--progress=no"],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
        data = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        send_status(hostname, "speedtest", "red",
                    f"speedtest: timeout ({_TIMEOUT}s)", "El test no completó a tiempo")
        return
    except (json.JSONDecodeError, FileNotFoundError) as e:
        send_status(hostname, "speedtest", "red",
                    "speedtest: error", str(e))
        return
    except Exception as e:
        send_status(hostname, "speedtest", "red",
                    f"speedtest: error: {e}", "")
        return

    if data.get("type") != "result":
        send_status(hostname, "speedtest", "red",
                    "speedtest: sin resultado", result.stdout[:200])
        return

    # Convertir bytes/s → Mbps
    down_bps  = data["download"]["bandwidth"]
    up_bps    = data["upload"]["bandwidth"]
    down_mbps = round(down_bps * 8 / 1_000_000, 1)
    up_mbps   = round(up_bps   * 8 / 1_000_000, 1)
    ping_ms   = round(data["ping"]["latency"], 1)
    jitter_ms = round(data["ping"]["jitter"],  1)
    server    = data["server"]["name"]
    isp       = data.get("isp", "")

    # Determinar color
    color = "green"
    problems = []

    if down_mbps < down_crit:
        color = "red";    problems.append(f"bajada {down_mbps} Mbps")
    elif down_mbps < down_warn:
        if color != "red": color = "yellow"
        problems.append(f"bajada {down_mbps} Mbps")

    if up_mbps < up_crit:
        color = "red";    problems.append(f"subida {up_mbps} Mbps")
    elif up_mbps < up_warn:
        if color != "red": color = "yellow"
        problems.append(f"subida {up_mbps} Mbps")

    if ping_ms > ping_crit:
        color = "red";    problems.append(f"ping {ping_ms}ms")
    elif ping_ms > ping_warn:
        if color != "red": color = "yellow"
        problems.append(f"ping {ping_ms}ms")

    summary = f"↓{down_mbps}Mbps  ↑{up_mbps}Mbps  ping:{ping_ms}ms"
    if problems:
        summary += "  ⚠ " + ", ".join(problems)

    message = (
        f"Bajada:   {down_mbps} Mbps\n"
        f"Subida:   {up_mbps} Mbps\n"
        f"Ping:     {ping_ms} ms  (jitter {jitter_ms} ms)\n"
        f"ISP:      {isp}\n"
        f"Servidor: {server}\n"
        f"URL:      {data['result'].get('url','')}"
    )

    send_status(hostname, "speedtest", color, summary, message)
