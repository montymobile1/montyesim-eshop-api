import os
from math import ceil
from typing import Literal, Optional, Dict, Any, Union, List

import httpx
from loguru import logger

from app.exceptions import DCBException, BadRequestException


class DCBService:

    def __init__(self, send_otp_url: str, charge_url: str, verify_otp_url: str, api_key: str):
        self.__send_otp_url = send_otp_url
        self.__charge_url = charge_url
        self.__verify_otp_url = verify_otp_url
        self.__api_key = api_key
        self.__merchant_msisdn = os.getenv("DCB_MERCHANT_MSISDN", "0937192488")

    async def send_sms_template(self, msisdn: str, message: str):
        body_request = {
            "ParamList": message,
            "Sender": "TRVSYRIATEL",
            "To": [
                msisdn.replace("+", ""),
            ]
        }
        try:
            logger.info(f"sending sms template: {msisdn} {message}")
            response = await self.__do_request(method="POST", url=os.getenv("DCB_SEND_SMS_API", "http://localhost"),
                                               body=body_request)
            logger.info(f"received sms template: {msisdn} {response}")
            return response
        except Exception as e:
            raise DCBException(str(e))

    async def resend_otp(self, msisdn: str, transaction_id: str):
        body_request = {
            "MerchantMSISDN": self.__merchant_msisdn,
            "TransactionID": transaction_id,
        }
        try:
            logger.info(f"sending new otp for msisdn: {msisdn}")
            response = await self.__do_request(method="POST", url=self.__send_otp_url, body=body_request)
            logger.info(f"received response for send_otp request: {response}")
            error_code = response.get("data", {}).get("errorCode")
            if error_code == "0":
                return response
            if error_code == "-97":
                raise DCBException("Invalid or expired transaction")
            elif error_code == "-100":
                raise DCBException("Technical error")
            else:
                raise BadRequestException(f"ErrorCode: {error_code}")
        except Exception as e:
            logger.error(f"failed to send_otp: {e}")
            if isinstance(e, DCBException):
                raise e
            raise DCBException(f"failed to send_otp: {e}")

    async def verify_otp(self, msisdn: str, otp: str, order_id: str):
        logger.info(f"verifying otp for msisdn: {msisdn}")
        body_request = {
            "OTP": otp,
            "MerchantMSISDN": self.__merchant_msisdn,
            "TransactionID": order_id
        }
        try:
            response = await self.__do_request(method="POST", url=self.__verify_otp_url, body=body_request)
            logger.info(f"received response for verify_otp: {response}")
            error_code = response.get("data", {}).get("errorCode")
            if error_code == "0":
                return response
            elif error_code == "-13":
                raise DCBException("Customer MSISDN doesn’t have enough balance")
            elif error_code == "-17":
                raise DCBException("Customer MSISDN will exceed the expenditure limit per day which is 550,000 SYP")
            elif error_code == "-96":
                raise DCBException("Invalid OTP")
            elif error_code == "-98":
                raise DCBException("Expired transaction (1 day has been passed), this case occurs in retry process")
            elif error_code == "-100":
                raise DCBException("Technical error")
            elif error_code == "-104":
                raise DCBException("Expired OTP")
            else:
                raise BadRequestException(f"ErrorCode: {error_code}")
        except Exception as e:
            logger.error(f"failed to verify_otp: {e}")
            if isinstance(e, DCBException):
                raise e
            raise DCBException(f"failed to verify_otp: {e}")

    async def payment_request(self, user_msisdn: str, merchant_msisdn, amount: float, order_id: str):
        body_request = {
            "CustomerMSISDN": user_msisdn.replace("+", ""),
            "MerchantMSISDN": self.__merchant_msisdn,
            "Amount": int(ceil(amount)),
            "TransactionID": order_id
        }
        try:
            logger.info(f"calling payment_request for msisdn: {user_msisdn} with amount: {amount}")
            response = await self.__do_request(method="POST", url=self.__charge_url, body=body_request)
            logger.info(f"received response for payment_request: {response}")
            error_code = response.get("data", {}).get("errorCode")
            if error_code == "0":
                return response
            elif error_code == "-100":
                raise DCBException("Technical error")
            else:
                raise BadRequestException(f"ErrorCode: {error_code}")
        except Exception as e:
            logger.error(f"failed to send_otp: {e}")
            if isinstance(e, DCBException):
                raise e
            raise DCBException(f"failed to send_otp: {e}")

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
                # headers["Tenant"] = self.__tenant_key
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
