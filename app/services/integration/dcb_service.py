import os
from typing import Literal, Optional, Dict, Any, Union, List

import httpx
from loguru import logger

from app.exceptions import DCBException


class DCBService:

    def __init__(self, send_otp_url: str, charge_url: str, verify_otp_url: str, api_key: str):
        self.__send_otp_url = send_otp_url
        self.__charge_url = charge_url
        self.__verify_otp_url = verify_otp_url
        self.__api_key = api_key
        self.__merchant_msisdn = os.getenv("DCB_MERCHANT_MSISDN", "0937192488")

    async def send_sms_template(self, msisdn: str, message: str):
        logger.info(f"calling send_sms_template with {msisdn=} {message=}")

    async def resend_otp(self, msisdn: str, transaction_id: str):
        logger.info(f"calling resend_otp{msisdn=} {transaction_id=}")

    async def verify_otp(self, msisdn: str, otp: str, order_id: str):
        logger.info(f"calling verify_otp {msisdn=} {otp=} {order_id=}")
        pass

    async def payment_request(self, user_msisdn: str, merchant_msisdn, amount: float, order_id: str):
        logger.info(f"calling payment request {user_msisdn=} {merchant_msisdn=} {amount=} {order_id=}")
        pass

    async def __do_request(self,
                           method: Literal["GET", "OPTIONS", "HEAD", "POST", "PUT", "PATCH", "DELETE"],
                           url: str,
                           headers: Optional[Dict[str, str]] = None,
                           params: Optional[Dict[str, str]] | Optional[Dict[str, List[str]]] = None,
                           body: Optional[Any] = None) -> Union[Dict[str, Any], List[Any], DCBException]:
        if headers is None:
            headers = {}
        try:
            with httpx.Client() as client:
                headers["Content-Type"] = "application/json"
                headers["Accept"] = "application/json"
                headers["Api-Key"] = self.__api_key
                response = client.request(method=method, url=url, headers=headers, params=params,
                                          json=body, timeout=120)
                logger.debug("Request: curl -X {} {} {} -d '{}' Response: {}".format(method, response.url, " ".join(
                    [f'--header "{key}: {value}"' for key, value in headers.items()]), body, response))
                if response.status_code != httpx.codes.OK:
                    try:
                        json_response = response.json()
                        raise DCBException(
                            json_response["message"] if "message" in json_response else str(json_response))
                    except Exception as e:
                        raise DCBException(f"DCB API request failed: {response.status_code}")
                return response.json()
        except Exception as e:
            if type(e).__name__ == "CustomException":
                raise e
            raise DCBException(str(e))
