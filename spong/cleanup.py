"""SPONG database maintenance - removes stale services and archives old history."""

import argparse
import logging
import time
from . import config, database

log = logging.getLogger(__name__)


def run_cleanup(
    old_service_days: int | None = None,
    old_history_days: int | None = None,
) -> None:
    if old_service_days is None:
        old_service_days = config.get("cleanup.old_service_days", 20)
    if old_history_days is None:
        old_history_days = config.get("cleanup.old_history_days", 30)

    hosts = database.list_hosts()
    total_services = 0
    total_history = 0
    total_acks = 0

    for host in hosts:
        # Remove stale services
        n = database.remove_stale_services(host, max_days=old_service_days)
        if n:
            log.info("Removed %d stale service(s) for %s", n, host)
            total_services += n

        # Archive old history
        n = database.archive_old_history(host, max_days=old_history_days)
        if n:
            log.info("Archived %d history entries for %s", n, host)
            total_history += n

        # Expired acks are cleaned up on load_acks(), force it
        database.load_acks(host)

    log.info(
        "Cleanup complete: %d services, %d history entries",
        total_services, total_history,
    )


def main():
    parser = argparse.ArgumentParser(description="SPONG database cleanup")
    parser.add_argument("--config", default=None)
    parser.add_argument("--old-service", type=int, default=None)
    parser.add_argument("--old-history", type=int, default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config.load_all(config_file=args.config)
    run_cleanup(args.old_service, args.old_history)


if __name__ == "__main__":
    main()
