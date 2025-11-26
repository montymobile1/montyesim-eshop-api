import asyncio
import os
from decimal import Decimal
from typing import List

from loguru import logger

from app.repo.currency_repo import CurrencyRepo
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import CurrencyDto
from app.schemas.response import ResponseHelper, Response


class CurrencyService:
    def __init__(self):
        self.__currency_repo = CurrencyRepo()

    async def aget_rate_by_currency(self, currency_name: str) -> float:
        if currency_name == os.getenv("SYSTEM_CURRENCY", "USD"):
            return 1.0

        currency = await self.__currency_repo.get_first_by(
            where={"name": currency_name, "default_currency": os.getenv("SYSTEM_CURRENCY", "USD")})

        if not currency:
            return 1.0

        return currency.rate

    def get_rate_by_currency(self, currency_name: str) -> float:
        return asyncio.run(self.aget_rate_by_currency(currency_name))

    def convert(self, from_currency: str, to_currency: str, amount: float) -> float:
        system_currency = os.getenv("SYSTEM_CURRENCY", "USD")
        if from_currency == system_currency:
            rate = self.get_rate_by_currency(to_currency)
            logger.info(f"Converting from {from_currency} to {to_currency} amount: {amount} with rate * {rate}")
            val = Decimal(amount) * Decimal(rate)
            return float(val)
        else:
            rate = self.get_rate_by_currency(from_currency)
            logger.info(f"Converting from {from_currency} to {to_currency} amount: {amount} with rate / {rate}")
            val = Decimal(amount) / Decimal(rate)
            return float(val)

    async def aget_currency_rate(self, from_currency: str, to_currency: str):
        currency = await self.__currency_repo.get_first_by(
            where={"name": to_currency, "default_currency": from_currency})
        if not currency:
            return 1.0
        return currency.rate

    def get_currency_rate(self, from_currency: str, to_currency: str):
        return asyncio.run(self.aget_currency_rate(from_currency, to_currency))

    def get_all_currency(self) -> Response[List[CurrencyDto]]:
        currency_list = asyncio.run(self.__currency_repo.list(where={"default_currency": "USD"}))
        currency_dto = []
        for currency in currency_list:
            currency_dto.append(DtoMapper.to_currency_dto(currency))
        return ResponseHelper.success_data_response(currency_dto, len(currency_list))
