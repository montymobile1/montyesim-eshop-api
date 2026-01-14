from loguru import logger


class DCBService:

    def __init__(self, send_otp_url: str, charge_url: str, verify_otp_url: str, api_key: str):
        self.__send_otp_url = send_otp_url
        self.__charge_url = charge_url
        self.__verify_otp_url = verify_otp_url
        self.__api_key = api_key

    async def send_otp(self, msisdn: str, otp: str, locale: str = "en") -> bool:
        logger.info(f"[DCB] Sending OTP to {msisdn=}")
        logger.debug(f"Unused parameters: locale={locale}, otp={otp}")
        return True

    async def verify_otp(self, msisdn: str, otp: str, order_id: str) -> bool:
        logger.info(f"[DCB] verifying OTP to DCB  {msisdn=}")
        return True

    async def deduct_balance(self, msisdn: str, amount: float, order_id: str, locale: str = "en") -> bool:
        logger.info(f"[DCB] deduct balance for {msisdn=} with {amount=} for {order_id=}")
        print(locale)  # Logging the unused parameter
        return True

    def get_send_otp_url(self) -> str:
        return self.__send_otp_url

    def get_api_key(self):
        return self.__api_key

    def get_charge_url(self):
        return self.__charge_url

    def get_verify_otp_url(self):
        return self.__verify_otp_url
