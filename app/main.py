import logging
import signal
import sys

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import AppConfig
from .database import Database
from .sync import sync_all


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stdout,
    )


def main() -> None:
    config = AppConfig.load()

    _setup_logging(config.log_level)
    logger = logging.getLogger(__name__)

    logger.info("Starting amazon-orders sync service")
    logger.info("Accounts configured: %d", len(config.accounts))
    logger.info("Sync schedule (UTC): %s", config.sync_schedule)
    logger.info("Database path: %s", config.db_path)

    db = Database(config.db_path)

    def run_sync() -> None:
        sync_all(config, db)

    parts = config.sync_schedule.split()
    if len(parts) != 5:
        logger.error(
            "SYNC_SCHEDULE must be a 5-part cron expression (e.g. '0 2 * * *'), got: %r",
            config.sync_schedule,
        )
        sys.exit(1)

    minute, hour, day, month, day_of_week = parts
    trigger = CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
        timezone="UTC",
    )

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(run_sync, trigger, id="sync_orders", name="Sync Amazon Orders")

    def _shutdown(signum, frame) -> None:  # type: ignore[type-arg]
        logger.info("Received signal %s, shutting down scheduler", signum)
        scheduler.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info("Scheduler running. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped")


if __name__ == "__main__":
    main()
