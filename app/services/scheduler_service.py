import asyncio
import os
import time

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv
from loguru import logger

from app.config.config import esim_hub_service_instance
from app.repo.currency_repo import CurrencyRepo

load_dotenv()


class SchedulerService:
    def __init__(self):
        self.__currency_repo = CurrencyRepo()
        self.scheduler = BackgroundScheduler()
        self.__esim_hub_service = esim_hub_service_instance()
        self.loop = asyncio.get_event_loop()

    async def _async_scheduled_task(self):
        logger.info(f"Scheduled task executed at {time.strftime('%X')}")
        currencies = await self.__currency_repo.list(where={"default_currency": "USD"})
        if not currencies:
            return
        names = [currency.name for currency in currencies]
        rates = await self.__esim_hub_service.get_exchange_rates(currency_codes=names)
        logger.info(f"exchange from esim hub: {rates}")
        for rate in rates:
            await self.__currency_repo.update_by(
                {"name": rate.currency_code, "default_currency": "USD"},
                data={'rate': rate.new_rate}
            )

    def scheduled_task(self):
        future = asyncio.run_coroutine_threadsafe(self._async_scheduled_task(), self.loop)
        try:
            future.result()
        except Exception as e:
            logger.error(f"Error in scheduled task: {e}")

    def start_scheduler(self):
        interval_seconds = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", 86400))
        self.scheduler.add_job(
            self.scheduled_task,
            trigger=IntervalTrigger(seconds=interval_seconds),
            id="my_task",
            name="Update exchange rates",
            replace_existing=True
        )
        self.scheduler.start()
        logger.info("Scheduler started")

    def shutdown_scheduler(self):
        self.scheduler.shutdown(wait=False)
        logger.info("Scheduler shut down")
