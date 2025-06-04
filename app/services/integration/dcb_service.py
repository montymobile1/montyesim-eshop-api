from loguru import logger


class DCBService:

    def __init__(self, send_otp_url: str, charge_url: str, verify_otp_url: str, api_key: str):
        self.__send_otp_url = send_otp_url
        self.__charge_url = charge_url
        self.__verify_otp_url = verify_otp_url
        self.__api_key = api_key

    async def send_otp(self, msisdn: str, otp: str) -> bool:
        logger.info(f"Sending OTP to DCB {self.__send_otp_url=} {msisdn=} {otp=} {self.__api_key=}")
        return True

    async def verify_otp(self, msisdn: str, otp: str, order_id: str) -> bool:
        logger.info(f"verifying OTP to DCB  {self.__verify_otp_url=} {msisdn=} {otp=} {order_id=} {self.__api_key=}")
        return True

    async def deduct_balance(self, msisdn: str, amount: float) -> bool:
        logger.info(f"deduct balance to DCB {self.__charge_url=} {msisdn=} {amount=} {self.__api_key=}")
        return True
