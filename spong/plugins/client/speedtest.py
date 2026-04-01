"""Client check: velocidad de internet via speedtest (Ookla CLI).

Umbrales (configurables en spong.yaml bajo thresholds.speedtest):
  down_warn: 10   (Mbps)  — amarillo si bajada < este valor
  down_crit:  5           — rojo si bajada < este valor
  up_warn:   10   (Mbps)
  up_crit:    5
  ping_warn: 50   (ms)
  ping_crit: 100
  server_id: 27292        — ID de servidor Ookla (opcional; omitir para automático)
"""

import json
import os
import subprocess
import time
from ... import config
from ...database import load_service
from ...status_sender import send_status

_SPEEDTEST_CMD = "speedtest"
_TIMEOUT = 120  # segundos máximos para el test
_DEFAULT_INTERVAL = 3600  # 1 hora entre mediciones


def check_speedtest(hostname: str) -> None:
    thresholds = config.get("thresholds", {}).get("speedtest", {})
    down_warn  = thresholds.get("down_warn",  10)
    down_crit  = thresholds.get("down_crit",   5)
    up_warn    = thresholds.get("up_warn",    10)
    up_crit    = thresholds.get("up_crit",     5)
    ping_warn  = thresholds.get("ping_warn",  50)
    ping_crit  = thresholds.get("ping_crit", 100)
    server_id  = thresholds.get("server_id",  None)
    interval   = thresholds.get("interval",   _DEFAULT_INTERVAL)

    # Saltear si la última medición fue hace menos de `interval` segundos
    svc = load_service(hostname, "speedtest")
    if svc and svc.report_time and (time.time() - svc.report_time) < interval:
        return

    cmd = [_SPEEDTEST_CMD, "--format=json", "--progress=no",
           "--accept-license", "--accept-gdpr"]
    if server_id:
        cmd += ["--server-id", str(server_id)]

    try:
        env = os.environ.copy()
        env.setdefault("HOME", "/root")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_TIMEOUT, env=env,
        )
        # El CLI puede emitir varias líneas JSON (logs + result); buscar la de resultado
        data = None
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parsed = json.loads(line)
            if parsed.get("type") == "result":
                data = parsed
                break
        if data is None:
            raise json.JSONDecodeError("no result line found", result.stdout, 0)
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

    summary = f"↓{down_mbps}Mbps  ↑{up_mbps}Mbps  ping:{ping_ms}ms  jitter:{jitter_ms}ms"
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
