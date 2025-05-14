from functools import cache
from sched import scheduler

from loguru import logger

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
import time
import os
from dotenv import load_dotenv

from app.repo.currency_repo import CurrencyRepo
import httpx

load_dotenv()

class SchedulerService:

    def __init__(self):
        self.__currency_repo = CurrencyRepo()
        self.scheduler = BackgroundScheduler()

    # Define the task to run
    def scheduled_task(self):
        logger.info(f"Scheduled task executed at {time.strftime('%X')}")
        currencies = self.__currency_repo.list(where={})
        if not currencies:
            print("No Currency Found")
            return
        currency_url=os.getenv("CURRENCY_URL")
        currencies_name:str = ""
        for currency in currencies:
            if currency.name == os.getenv("DEFAULT_CURRENCY"):
                continue
            currencies_name += currency.name + ","
        currency_url = currency_url.replace("currencies_value",currencies_name)
        currency_url = currency_url.replace("source_value",os.getenv("DEFAULT_CURRENCY"))
        with httpx.Client() as client:
            response = client.request(method="GET", url=currency_url ,timeout=120)
            quotes = response.json()["quotes"]
            source = response.json()["source"]
            for key, value in quotes.items():
                new_key = key[len(source):]
                updated_currency = {"rate" : value}
                self.__currency_repo.update_by({"name": new_key}, data=updated_currency)
            print(response.json())


    def start_scheduler(self):
        interval_seconds = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", 10000000))

        self.scheduler.add_job(
            self.scheduled_task,
            trigger=IntervalTrigger(seconds=interval_seconds),
            # trigger=CronTrigger(hour=0, minute=0),
            id="my_task",
            name="Run every 24 Hours",
            replace_existing=True
        )

        self.scheduler.start()
        logger.info("Scheduler Started")


    def shutdown_scheduler(self):
        self.scheduler.shutdown()
        logger.info("Scheduler shut down")
