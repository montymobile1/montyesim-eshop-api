import os

import httpx
from loguru import logger

from app.config.constants import ErrorMessages
from app.exceptions import DCBException
from app.services.integration.dcb_service import DCBService


class MontyDCBService(DCBService):
    def __init__(self, base_url: str):
        super().__init__(send_otp_url=base_url, charge_url="", verify_otp_url="", api_key="")
        self.__username = os.getenv("DCB_USERNAME", "")
        self.__password = os.getenv("DCB_PASSWORD", "")
        self.__sender_id = os.getenv("DCB_SENDER_ID", "")
        self.__data_coding = os.getenv("DCB_DATA_CODING", "0")

    def send_otp(self, msisdn: str, otp: str) -> bool:
        url = self.get_send_otp_url()
        logger.info(f"[MONTY] Sending OTP to {msisdn=} via {url=}")
        try:
            with httpx.Client() as client:
                params = {
                    "username": self.__username,
                    "password": self.__password,
                }
                body = {
                    "destination": msisdn,
                    "text": f"Your OTP code is {otp}",
                    "source": self.__sender_id,
                    "dataCoding": self.__data_coding
                }
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json"
                }
                response = client.request(method="POST",
                                          url=f"{url}?username{self.__username}&password={self.__password}",
                                          headers=headers,
                                          params=params,
                                          json=body,
                                          timeout=120)
                json_response = response.json()
                if json_response["ErrorCode"] == "0":
                    return True
                else:
                    logger.error(f"[MONTY] Failed to send OTP: {json_response}")
                    raise DCBException(
                        details=f"Failed to send OTP: {json_response['ErrorCode']} {json_response['ErrorDescription']}",
                        error=ErrorMessages.OTP_SEND_SMS_FAILED)
        except Exception as e:
            logger.error(f"Error sending OTP to {msisdn}: {e}")
            if isinstance(e, DCBException):
                raise e
            return False
