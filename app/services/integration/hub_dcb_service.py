import os

import httpx
from loguru import logger

from app.config.helper import get_config
from app.services.integration.dcb_service import DCBService


class HubDcbService(DCBService):

    def __init__(self):
        send_otp_url = get_config("DCB_HUB_SEND_SMS_URL", "")
        charge_url = get_config("DCB_HUB_CHARGE_URL", "")
        verify_otp_url = get_config("DCB_HUB_VERIFY_OTP_URL", "")
        api_key = get_config("DCB_HUB_API_KEY", "")
        self.__source_msisdn = get_config("DCB_HUB_SOURCE_MSISDN", "")
        super().__init__(send_otp_url=send_otp_url, charge_url=charge_url, verify_otp_url=verify_otp_url,
                         api_key=api_key)

    async def send_otp(self, msisdn: str, otp: str) -> bool:
        url = self.get_send_otp_url()
        logger.info(f"[DCB_HUB] Sending OTP to {msisdn=} via {url=}")
        try:
            with httpx.Client() as client:
                body = {
                    "sourceMsisdn": self.__source_msisdn,
                    "destinationMsisdn": msisdn.replace("+", ""),
                    "message": f"Your OTP code is {otp}",
                    "smsType": "NORMAL",
                    "deliveryReceipt": False
                }
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Api-Key": self.get_api_key(),
                    "Tenant": os.getenv("ESIM_HUB_TENANT_KEY")
                }
                response = client.request(method="POST",
                                          url=url,
                                          headers=headers,
                                          json=body,
                                          timeout=120)
                try:
                    json_response = response.json()
                    if json_response.get("message") == "Success":
                        return True
                    else:
                        logger.error(f"[DCB_HUB] Failed to send OTP: {json_response}")
                        return False
                except Exception as e:
                    logger.error(f"[DCB_HUB] Invalid response while sending OTP: {response.status_code}, error: {e}")
                    return False


        except Exception as e:
            logger.error(f"[DCB_HUB] Error sending OTP to {msisdn}: {e}")
            return False

    async def deduct_balance(self, msisdn: str, amount: float, order_id: str) -> bool:
        url = self.get_charge_url()
        logger.info(f"[DCB_HUB] deducting balance for  {msisdn=}")

        try:
            with httpx.Client() as client:
                body = {
                    "SerialNo": order_id,
                    "Msisdn": msisdn.replace("+", ""),
                    "MsisdnExtension": get_config("DCB_HUB_MSISDN_EXTENSION", ""),
                    "ChargeSeq": "DCB",
                    "ChargeCode": get_config("DCB_HUB_CHARGE_CODE", "CC_OTC"),
                    "Amount": amount,
                    "CurrencyId": int(get_config("DCB_HUB_CURRENCY_ID", "1098")),
                    "TaxCode": get_config("DCB_HUB_TAX_CODE", "C_TAX_CODE"),
                    "TaxAmount": float(get_config("DCB_HUB_TAX_AMOUNT", 0)),
                    "BusinessType": get_config("DCB_HUB_BUSINESS_TYPE", "CO019")
                }
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Api-Key": self.get_api_key(),
                    "Tenant": os.getenv("ESIM_HUB_TENANT_KEY")
                }
                response = client.request(method="POST",
                                          url=url,
                                          headers=headers,
                                          json=body,
                                          timeout=120)
                try:
                    json_response = response.json()
                    if json_response.get("message") == "Success":
                        return True
                    else:
                        logger.error(f"[DCB_HUB] Failed to deduct balance: {json_response}")
                        return False
                except Exception as e:
                    logger.error(
                        f"[DCB_HUB] Invalid response while deducting balance: {response.status_code}, error: {e}")
                    return False
        except Exception as e:
            logger.error(f"[DCB_HUB] Error deducting balance for {msisdn}: {e}")
            return False
