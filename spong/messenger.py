"""SPONG message dispatcher - sends notifications when service status changes."""

from __future__ import annotations
import argparse
import importlib
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from . import config, database

log = logging.getLogger(__name__)

PLUGIN_PKG = "spong.plugins.message"


def _format_template(template: str, **vars_) -> str:
    """Substitute {key} placeholders in template."""
    result = template
    for k, v in vars_.items():
        result = result.replace(f"{{{k}}}", str(v))
    return result


def _match_rule(rule: dict, host: str, service: str) -> bool:
    """Check if a rule matches the given host/service."""
    # Host patterns
    host_patterns = rule.get("hosts", [])
    if host_patterns:
        if not any(re.fullmatch(p, host) for p in host_patterns):
            return False

    # Exclude hosts
    excl_hosts = rule.get("exclude_hosts", [])
    if any(re.fullmatch(p, host) for p in excl_hosts):
        return False

    # Service patterns
    svc_patterns = rule.get("services", [])
    if svc_patterns:
        if not any(re.fullmatch(p, service) for p in svc_patterns):
            return False

    # Exclude services
    excl_svcs = rule.get("exclude_services", [])
    if any(re.fullmatch(p, service) for p in excl_svcs):
        return False

    # Host groups
    host_groups = rule.get("host_groups", [])
    if host_groups:
        groups = config.get_groups()
        in_group = False
        for gname in host_groups:
            if host in groups.get(gname, {}).get("members", []):
                in_group = True
                break
        if not in_group:
            return False

    # Time windows
    time_windows = rule.get("times", [])
    if time_windows and not _in_time_windows(time_windows):
        return False

    return True


def _in_time_windows(windows: list[dict]) -> bool:
    """Return True if current time is within any of the given windows."""
    now = datetime.now()
    dow = str(now.weekday())   # 0=Mon ... 6=Sun
    hhmm = now.strftime("%H:%M")

    for window in windows:
        # Days filter (0-6, or ranges like "1-3")
        days = window.get("days", [])
        if days:
            day_match = False
            for d in days:
                if "-" in str(d):
                    lo, hi = str(d).split("-", 1)
                    if int(lo) <= int(dow) <= int(hi):
                        day_match = True
                        break
                elif str(d) == dow:
                    day_match = True
                    break
            if not day_match:
                continue

        # Time ranges
        times = window.get("times", [])
        if times:
            time_match = False
            for t in times:
                lo, hi = t.split("-")
                if lo <= hhmm <= hi:
                    time_match = True
                    break
            if not time_match:
                continue

        return True
    return False


def _resolve_contact(contact_str: str, contacts: dict) -> dict | None:
    """Resolve a contact string to a contact dict."""
    method = None
    if ":" in contact_str:
        name, method = contact_str.split(":", 1)
    else:
        name = contact_str
    human = contacts.get(name)
    if not human:
        log.warning("Unknown contact: %s", name)
        return None
    result = dict(human)
    if method:
        result["method"] = method
    return result


def _find_template(templates: dict, contact_name: str, method: str | None) -> dict:
    """Find the best matching template."""
    if method and contact_name:
        key = f"{contact_name}:{method}"
        if key in templates:
            return templates[key]
    if method and method in templates:
        return templates[method]
    if contact_name in templates:
        return templates[contact_name]
    return templates.get("DEFAULT", {
        "subject": "spong - {color} {host} {service}",
        "body": "{datetime}\n{color} {host} {service}\n{summary}",
    })


def _send_notification(
    contact: dict, method: str, subject: str, body: str
) -> None:
    """Dispatch notification via appropriate plugin."""
    mod_name = f"{PLUGIN_PKG}.{method}"
    try:
        mod = importlib.import_module(mod_name)
        func = getattr(mod, "send_message", None)
        if func:
            func(contact, subject, body)
        else:
            log.error("No send_message in %s", mod_name)
    except ImportError:
        # Fallback to email
        try:
            from .plugins.message import email_plugin
            email_plugin.send_message(contact, subject, body)
        except Exception as e:
            log.error("Failed to send notification: %s", e)


def notify(
    color: str, host: str, service: str, timestamp: float,
    summary: str, message: str = "",
) -> None:
    """Main notification dispatcher."""
    msg_cfg = config.get_message_config()
    rules = msg_cfg.get("rules", [])
    templates = msg_cfg.get("templates", {})
    rules_match = msg_cfg.get("rules_match", "FIRST-MATCH")
    contacts = config.get_contacts()
    wwwspong = config.get("web.spong_url", "/cgi-bin/www-spong")

    now = datetime.fromtimestamp(timestamp)
    fmt = config.get("date_format", "%d/%m/%y")
    tfmt = config.get("time_format", "%H:%M:%S")
    dtfmt = config.get("datetime_format", "%c")

    tpl_vars = dict(
        host=host,
        shorthost=host.split(".")[0],
        service=service,
        color=color,
        status=color,
        summary=summary,
        detailed=message,
        datetime=now.strftime(dtfmt),
        date=now.strftime(fmt),
        time=now.strftime(tfmt),
        wwwspong=wwwspong,
    )

    matched = False
    for rule in rules:
        if not _match_rule(rule, host, service):
            continue
        matched = True

        rule_contacts = rule.get("contacts", [])
        if not rule_contacts:
            # Rule with no contacts = silence (suppress)
            log.debug("Matched silence rule for %s/%s", host, service)
            if rules_match == "FIRST-MATCH":
                return
            continue

        for contact_entry in rule_contacts:
            delay = 0
            repeat = 0
            contact_str = contact_entry
            if isinstance(contact_entry, dict):
                contact_str = contact_entry.get("rcpt", "")
                delay = contact_entry.get("delay", 0)
                repeat = contact_entry.get("repeat", 0)

            method = None
            name = contact_str
            if ":" in contact_str:
                name, method = contact_str.split(":", 1)

            # Check ack
            if database.is_acknowledged(host, service):
                log.debug("Acked, skipping notification for %s/%s", host, service)
                continue

            contact = _resolve_contact(contact_str, contacts)
            if not contact:
                continue

            effective_method = method or "email"
            tmpl = _find_template(templates, name, method)
            subject = _format_template(tmpl.get("subject", ""), **tpl_vars)
            body = _format_template(tmpl.get("body", ""), **tpl_vars)

            log.info("Notifying %s via %s for %s/%s (%s)",
                     name, effective_method, host, service, color)
            _send_notification(contact, effective_method, subject, body)

        if rules_match == "FIRST-MATCH":
            break

    if not matched:
        log.debug("No messaging rule matched for %s/%s", host, service)


def main():
    parser = argparse.ArgumentParser(description="SPONG message dispatcher")
    parser.add_argument("color")
    parser.add_argument("host")
    parser.add_argument("service")
    parser.add_argument("timestamp", type=float)
    parser.add_argument("summary")
    parser.add_argument("message", nargs="?", default="")
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config.load_all(config_file=args.config)

    send_mode = config.get("messaging.send_mode", "RED-CHANGE")
    if send_mode == "NONE":
        sys.exit(0)
    if send_mode == "RED" and args.color != "red":
        sys.exit(0)

    notify(
        color=args.color,
        host=args.host,
        service=args.service,
        timestamp=args.timestamp,
        summary=args.summary,
        message=args.message,
    )


if __name__ == "__main__":
    main()
