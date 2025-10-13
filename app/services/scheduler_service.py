import asyncio
import os
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from loguru import logger

from app.config.config import esim_hub_service_instance
from app.repo.currency_repo import CurrencyRepo
from app.services.sync_service import SyncService

load_dotenv()


class SchedulerService:
    def __init__(self):
        self.__currency_repo = CurrencyRepo()
        self.scheduler = BackgroundScheduler()
        self.__esim_hub_service = esim_hub_service_instance()
        self.__started = False
        self.__sync_service = SyncService()

    async def _async_scheduled_task(self):
        logger.info(f"Scheduled task execution start at {time.strftime('%X')}")
        currencies = self.__currency_repo.list(where={"default_currency": "USD"})
        if not currencies:
            return
        names = [currency.name for currency in currencies]
        rates = await self.__esim_hub_service.get_exchange_rates(currency_codes=names)
        logger.info(f"exchange from esim hub: {rates}")
        for rate in rates:
            logger.info(
                f"updating currency {rate.currency_code=} to  {rate.current_rate=}")
            self.__currency_repo.update_by(
                {"name": rate.currency_code, "default_currency": "USD"},
                data={'rate': rate.current_rate}
            )
        self.__sync_service.update_sync_version()
        logger.info(f"Scheduled task execution ends at {time.strftime('%X')}")

    def scheduled_task(self):
        try:
            # Run the async task within the scheduler's background thread
            asyncio.run(self._async_scheduled_task())
        except Exception as e:
            logger.error(f"Error in scheduled task: {e}")

    def start_scheduler(self):
        if self.__started:
            logger.info("Scheduler already started; skipping duplicate start.")
            return
        interval_seconds = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", 60 * 60 * 12))  # default to 12 hours
        misfire_grace = int(os.getenv("SCHEDULER_MISFIRE_GRACE_SECONDS", 60))
        max_instances = int(os.getenv("SCHEDULER_MAX_INSTANCES", 1))
        self.scheduler.add_job(
            self.scheduled_task,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id="my_task",
            name="Update exchange rates",
            replace_existing=True,
            max_instances=max_instances,
            coalesce=True,
            misfire_grace_time=misfire_grace,
        )
        self.scheduler.start()
        self.__started = True
        logger.info("Scheduler started")

    def shutdown_scheduler(self):
        self.scheduler.shutdown(wait=False)
        self.__started = False
        logger.info("Scheduler shut down")
