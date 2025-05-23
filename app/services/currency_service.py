import os
from typing import List

from app.repo.currency_repo import CurrencyRepo
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import CurrencyDto
from app.schemas.response import ResponseHelper, Response


class CurrencyService:
    def __init__(self):
        self.__currency_repo = CurrencyRepo()

    def get_rate_by_currency(self, currency_name: str) -> float:
        if currency_name == os.getenv("SYSTEM_CURRENCY", "USD"):
            return 1.0

        currency = self.__currency_repo.get_first_by(
            where={"name": currency_name, "default_currency": os.getenv("SYSTEM_CURRENCY", "USD")})

        if not currency:
            return 1.0

        return currency.rate

    def get_all_currency(self) -> Response[List[CurrencyDto]]:
        currency_list = self.__currency_repo.list(where={})
        currency_dto = []
        for currency in currency_list:
            currency_dto.append(DtoMapper.to_currency_dto(currency))
        return ResponseHelper.success_data_response(currency_dto, len(currency_list))
