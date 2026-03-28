"""SPONG central server - asyncio TCP server.

Listens on:
  - Update port (1998): receives status updates, acks, events
  - Query port  (1999): responds to queries from CLI/web clients
  - BB port     (1984): BigBrother protocol compatibility
"""

from __future__ import annotations
import asyncio
import logging
import os
import re
import signal
import subprocess
import time
import shlex
from pathlib import Path
from typing import Optional

from . import config, database
from .models import HistoryEntry, worst_color
from .protocol import parse_update, parse_query, StatusMessage, AckMessage, AckDelMessage

log = logging.getLogger(__name__)

BINDIR = Path("/usr/local/spong/bin")


# ---------------------------------------------------------------------------
# Update handler
# ---------------------------------------------------------------------------

async def handle_update(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        header_bytes = await asyncio.wait_for(reader.readline(), timeout=30)
        header = header_bytes.decode(errors="replace")
        body_bytes = await asyncio.wait_for(reader.read(100_000), timeout=30)
        body = body_bytes.decode(errors="replace")
    except asyncio.TimeoutError:
        log.warning("update: connection from %s timed out", peer)
        writer.close()
        return
    finally:
        writer.close()

    msg = parse_update(header, body)
    if msg is None:
        return

    if isinstance(msg, StatusMessage):
        await _process_status(msg)
    elif isinstance(msg, AckMessage):
        await asyncio.get_event_loop().run_in_executor(None, _process_ack, msg)
    elif isinstance(msg, AckDelMessage):
        await asyncio.get_event_loop().run_in_executor(None, _process_ack_del, msg)


async def _process_status(msg: StatusMessage) -> None:
    loop = asyncio.get_event_loop()

    # Run DB write in executor to avoid blocking
    color_changed = await loop.run_in_executor(
        None,
        _db_save_status,
        msg,
    )

    # Trigger spong-message if status changed
    if color_changed or msg.cmd in ("page",):
        await loop.run_in_executor(None, _trigger_message, msg)


def _db_save_status(msg: StatusMessage) -> bool:
    changed = database.save_status(
        host=msg.host,
        service=msg.service,
        color=msg.color,
        report_time=msg.timestamp,
        summary=msg.summary,
        message=msg.message,
        ttl=msg.ttl,
    )
    try:
        from . import rrd as _rrd
        _rrd.update_from_status(msg.host, msg.service, msg.summary, msg.message, msg.timestamp)
    except Exception as _e:
        log.debug("rrd update skipped: %s", _e)
    if changed:
        entry = HistoryEntry(
            event_type=msg.cmd,
            timestamp=msg.timestamp,
            service=msg.service,
            color=msg.color,
            summary=msg.summary,
        )
        database.append_history(msg.host, entry)
        if config.get("status_history", True):
            database.save_status_detail(
                host=msg.host,
                service=msg.service,
                color=msg.color,
                start_time=msg.timestamp,
                report_time=msg.timestamp,
                summary=msg.summary,
                message=msg.message,
            )
        log.info("status change: %s/%s -> %s: %s",
                 msg.host, msg.service, msg.color, msg.summary)
    return changed


def _trigger_message(msg: StatusMessage) -> None:
    smessage = str(BINDIR / "spong-message")
    if not os.path.exists(smessage):
        return
    send_mode = config.get("messaging.send_mode", "RED-CHANGE")
    if send_mode == "NONE":
        return
    if send_mode == "RED" and msg.color != "red":
        return

    duration = 0  # Would need to read from DB for real duration
    cmd = [
        smessage,
        msg.color, msg.host, msg.service,
        str(int(msg.timestamp)), msg.summary,
        msg.message.strip() or msg.summary,
    ]
    try:
        subprocess.Popen(cmd, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        log.error("trigger_message: %s", e)


def _process_ack(msg: AckMessage) -> None:
    database.save_ack(
        host=msg.host,
        services=msg.services,
        start_time=msg.start_time,
        end_time=msg.end_time,
        contact=msg.contact,
        message=msg.message,
    )
    entry = HistoryEntry(
        event_type="ack",
        timestamp=msg.start_time,
        service=msg.services,
        user=msg.contact,
    )
    database.append_history(msg.host, entry)
    log.info("ack saved: %s/%s until %s by %s",
             msg.host, msg.services, int(msg.end_time), msg.contact)


def _process_ack_del(msg: AckDelMessage) -> None:
    database.delete_ack(msg.host, msg.end_time)
    log.info("ack deleted: %s", msg.ack_id)


# ---------------------------------------------------------------------------
# Query handler
# ---------------------------------------------------------------------------

async def handle_query(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    try:
        line_bytes = await asyncio.wait_for(reader.readline(), timeout=30)
    except asyncio.TimeoutError:
        writer.close()
        return

    line = line_bytes.decode(errors="replace")
    query = parse_query(line)
    if not query:
        writer.close()
        return

    log.debug("query from %s: %s [%s:%s]",
              peer, query.command, query.fmt_type, query.view)

    loop = asyncio.get_event_loop()
    response = await loop.run_in_executor(None, dispatch_query, query)

    try:
        writer.write(response.encode(errors="replace"))
        await writer.drain()
    except OSError:
        pass
    finally:
        writer.close()


def dispatch_query(query) -> str:
    hosts_cfg = config.get_hosts()
    groups_cfg = config.get_groups()

    def resolve_hosts(hostspec: str) -> list[str]:
        if hostspec == "all":
            return list(hosts_cfg.keys())
        if hostspec in groups_cfg:
            return groups_cfg[hostspec].get("members", [])
        return [h.strip() for h in hostspec.split(",") if h.strip()]

    host_list = resolve_hosts(query.hosts)

    if query.command == "summary":
        return _render_summary(host_list, query.view)
    if query.command == "problems":
        return _render_problems(host_list, query.view)
    if query.command == "history":
        return _render_history(host_list, query.view)
    if query.command == "host":
        return _render_host(host_list, query.view)
    if query.command == "services":
        return _render_services(host_list, query.view)
    if query.command == "acks":
        return _render_acks(host_list, query.view)
    if query.command == "grpsummary":
        return _render_grp_summary(query.view)
    if query.command == "grpproblems":
        return _render_grp_problems(query.view)
    return f"Unknown command: {query.command}\n"


COLOR_SYMBOLS = {
    "red": "RED   ",
    "yellow": "YELLOW",
    "green": "GREEN ",
    "purple": "PURPLE",
    "blue": "BLUE  ",
    "clear": "CLEAR ",
}


def _render_summary(hosts: list[str], view: str) -> str:
    lines = []
    for host in hosts:
        services = database.load_all_services(host)
        host_color = worst_color([s.color for s in services.values()]) if services else "green"
        sym = COLOR_SYMBOLS.get(host_color, "??????")
        svc_summary = "  ".join(
            f"{n}:{s.color}" for n, s in sorted(services.items())
        )
        lines.append(f"{sym} {host:<30} {svc_summary}")
    return "\n".join(lines) + "\n" if lines else "No hosts found\n"


def _render_problems(hosts: list[str], view: str) -> str:
    lines = []
    for host in hosts:
        services = database.load_all_services(host)
        for svc_name, svc in sorted(services.items()):
            if svc.color in ("red", "yellow", "purple"):
                sym = COLOR_SYMBOLS.get(svc.color, "??????")
                lines.append(f"{sym} {host}/{svc_name}: {svc.summary}")
    return "\n".join(lines) + "\n" if lines else "No problems\n"


def _render_history(hosts: list[str], view: str) -> str:
    days = {"brief": 1, "standard": 7, "full": 30}.get(view, 7)
    lines = []
    for host in hosts:
        entries = database.load_history(host, max_age_days=days)
        if entries:
            lines.append(f"\n=== {host} ===")
            for e in sorted(entries, key=lambda x: x.timestamp, reverse=True):
                ts_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.timestamp))
                lines.append(f"  {ts_str} {e.event_type:8} {e.service:15} "
                             f"{e.color:8} {e.summary}")
    return "\n".join(lines) + "\n" if lines else "No history\n"


def _render_host(hosts: list[str], view: str) -> str:
    lines = []
    for host in hosts:
        lines.append(f"\n=== {host} ===")
        services = database.load_all_services(host)
        acks = database.load_acks(host)
        acked = {a.services for a in acks}
        for svc_name, svc in sorted(services.items()):
            sym = COLOR_SYMBOLS.get(svc.color, "??????")
            lines.append(f"  {sym} {svc_name:20} {svc.summary}")
            if view in ("standard", "full") and svc.message:
                for mline in svc.message.splitlines()[:5]:
                    lines.append(f"    {mline}")
    return "\n".join(lines) + "\n"


def _render_services(hosts: list[str], view: str) -> str:
    all_services: dict[str, dict] = {}
    for host in hosts:
        for svc_name, svc in database.load_all_services(host).items():
            if svc_name not in all_services:
                all_services[svc_name] = {"worst": "green", "hosts": []}
            all_services[svc_name]["hosts"].append((host, svc.color))
            all_services[svc_name]["worst"] = worst_color(
                [all_services[svc_name]["worst"], svc.color]
            )
    lines = []
    for svc_name, info in sorted(all_services.items()):
        sym = COLOR_SYMBOLS.get(info["worst"], "??????")
        host_str = ", ".join(f"{h}:{c}" for h, c in info["hosts"])
        lines.append(f"{sym} {svc_name:20} {host_str}")
    return "\n".join(lines) + "\n" if lines else "No services\n"


def _render_acks(hosts: list[str], view: str) -> str:
    lines = []
    for host in hosts:
        acks = database.load_acks(host)
        for ack in acks:
            until = time.strftime("%Y-%m-%d %H:%M", time.localtime(ack.end_time))
            lines.append(f"{host} {ack.services} acked by {ack.contact} until {until}")
            if ack.message:
                lines.append(f"  {ack.message.strip()}")
    return "\n".join(lines) + "\n" if lines else "No acknowledgments\n"


def _render_grp_summary(view: str) -> str:
    groups = config.get_groups()
    lines = []
    for gname, gdata in groups.items():
        if not gdata.get("display", True):
            continue
        members = gdata.get("members", [])
        colors = []
        for host in members:
            services = database.load_all_services(host)
            if services:
                colors.append(worst_color([s.color for s in services.values()]))
        group_color = worst_color(colors) if colors else "green"
        sym = COLOR_SYMBOLS.get(group_color, "??????")
        lines.append(f"{sym} {gdata.get('name', gname):20} ({len(members)} hosts)")
    return "\n".join(lines) + "\n" if lines else "No groups\n"


def _render_grp_problems(view: str) -> str:
    groups = config.get_groups()
    lines = []
    for gname, gdata in groups.items():
        members = gdata.get("members", [])
        for host in members:
            for svc_name, svc in database.load_all_services(host).items():
                if svc.color in ("red", "yellow", "purple"):
                    sym = COLOR_SYMBOLS.get(svc.color, "??????")
                    lines.append(f"{sym} {host}/{svc_name}: {svc.summary}")
    return "\n".join(lines) + "\n" if lines else "No problems\n"


# ---------------------------------------------------------------------------
# BigBrother compatibility handler
# ---------------------------------------------------------------------------

async def handle_bb_update(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Handle BigBrother protocol updates and convert to SPONG format."""
    try:
        header_bytes = await asyncio.wait_for(reader.readline(), timeout=30)
        body_bytes = await asyncio.wait_for(reader.read(100_000), timeout=30)
    except asyncio.TimeoutError:
        writer.close()
        return
    finally:
        writer.close()

    header = header_bytes.decode(errors="replace").strip()
    body = body_bytes.decode(errors="replace")

    # Parse BB format: status host.service color <BB date string> summary
    m = re.match(
        r"^(\w+)\s+([\w,\-_]+)\.(\w+)\s+(\w+)\s+"
        r"(\w{3} \w{3}\s+\d+ \d{2}:\d{2}:\d{2}[ A-Z]+\d{4})\s+(.*)$",
        header,
    )
    if not m:
        return

    cmd, host, service, color, bb_time, summary = m.groups()
    host = host.replace(",", ".")

    # Parse BB date to Unix timestamp
    try:
        ts = time.mktime(time.strptime(bb_time.strip(), "%a %b %d %H:%M:%S %Z %Y"))
    except ValueError:
        ts = time.time()

    # Check for FQDN override in summary
    fqdn_m = re.match(r"\[([A-Za-z0-9._\-]+)\]\w*(.*)", summary)
    if fqdn_m:
        host = fqdn_m.group(1)
        summary = fqdn_m.group(2).strip()

    if color not in ("red", "yellow", "green", "purple", "clear"):
        return

    msg = StatusMessage(
        cmd=cmd, host=host, service=service, color=color,
        timestamp=ts, ttl=0, summary=summary, message=body,
    )
    await _process_status(msg)


# ---------------------------------------------------------------------------
# Stale-data purple scanner
# ---------------------------------------------------------------------------

async def stale_data_scanner(interval: int = 900) -> None:
    """Periodically mark stale services as purple."""
    while True:
        await asyncio.sleep(interval)
        hosts = database.list_hosts()
        for host in hosts:
            configured = {s for s, _ in config.host_services(host)}
            for svc_name, svc in database.load_all_services(host).items():
                # Remove services no longer configured for this host
                if configured and svc_name not in configured:
                    log.debug("removing unconfigured service %s/%s", host, svc_name)
                    database.delete_service(host, svc_name)
                    continue
                age = time.time() - svc.report_time
                # Services not updated in 2x their expected interval → purple
                if age > 1800 and svc.color not in ("purple", "clear"):
                    log.debug("marking %s/%s purple (age=%.0fs)", host, svc_name, age)
                    database.save_status(
                        host=host, service=svc_name, color="purple",
                        report_time=time.time(),
                        summary=f"No data received for {int(age)}s",
                        message="",
                    )


# ---------------------------------------------------------------------------
# Main server entry point
# ---------------------------------------------------------------------------

async def run_server() -> None:
    config.load_all()

    update_port = config.update_port()
    query_port = config.query_port()
    bb_port = config.bb_port()

    servers = []

    srv_update = await asyncio.start_server(handle_update, "", update_port, backlog=1024)
    servers.append(srv_update)
    log.info("Listening for updates on port %d", update_port)

    srv_query = await asyncio.start_server(handle_query, "", query_port, backlog=1024)
    servers.append(srv_query)
    log.info("Listening for queries on port %d", query_port)

    if bb_port:
        srv_bb = await asyncio.start_server(handle_bb_update, "", bb_port, backlog=1024)
        servers.append(srv_bb)
        log.info("Listening for BB updates on port %d", bb_port)

    scanner_task = asyncio.create_task(stale_data_scanner())

    loop = asyncio.get_running_loop()

    def _shutdown():
        log.info("Shutting down...")
        scanner_task.cancel()
        for s in servers:
            s.close()

    loop.add_signal_handler(signal.SIGTERM, _shutdown)
    loop.add_signal_handler(signal.SIGINT, _shutdown)

    async with asyncio.TaskGroup() as tg:
        for srv in servers:
            tg.create_task(srv.serve_forever())


def main():
    import argparse

    parser = argparse.ArgumentParser(description="SPONG server")
    parser.add_argument("--config", default=None)
    parser.add_argument("--nodaemonize", action="store_true")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not args.nodaemonize and not args.debug:
        from .daemon import daemonize, write_pid
        from . import config as cfg
        daemonize()
        cfg.load_all()
        write_pid(cfg.tmp_path() / "spong-server.pid")

    asyncio.run(run_server())


if __name__ == "__main__":
    main()
