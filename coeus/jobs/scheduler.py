from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from coeus import settings_store as ss
from coeus.config import Settings
from coeus.db import session_scope
from coeus.jobs import archive

log = logging.getLogger(__name__)


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=settings.timezone)
    with session_scope() as db:
        at = str(ss.get(db, "archive.schedule_time") or "01:30")
    hh, mm = (int(x) for x in at.split(":"))

    def daily():
        archive.plan_yesterday(settings)
        archive.run_worker(settings)

    sched.add_job(daily, CronTrigger(hour=hh, minute=mm), id="archive-daily",
                  replace_existing=True, misfire_grace_time=6 * 3600, coalesce=True)
    sched.add_job(archive.run_worker, "interval", minutes=10, args=[settings],
                  id="archive-worker", replace_existing=True, max_instances=1, coalesce=True)
    sched.add_job(archive.cleanup_events, CronTrigger(hour=4, minute=10), args=[settings],
                  id="events-cleanup", replace_existing=True)
    return sched


def reschedule_daily(sched: BackgroundScheduler, at: str) -> None:
    hh, mm = (int(x) for x in at.split(":"))
    sched.reschedule_job("archive-daily", trigger=CronTrigger(hour=hh, minute=mm))
