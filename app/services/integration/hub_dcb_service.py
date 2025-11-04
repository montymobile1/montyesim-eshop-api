import os

import httpx
from loguru import logger

from app.config.helper import get_config
from app.services.integration.dcb_service import DCBService


class HubDcbService(DCBService):

    def __init__(self):
        base_url = get_config("DCB_HUB_BASE_URL", "")
        charge_url = get_config("DCB_HUB_CHARGE_URL", "")
        verify_otp_url = get_config("DCB_HUB_VERIFY_OTP_URL", "")
        api_key = get_config("DCB_HUB_API_KEY", "")
        super().__init__(send_otp_url=base_url, charge_url=charge_url, verify_otp_url=verify_otp_url, api_key=api_key)

    async def send_otp(self, msisdn: str, otp: str) -> bool:
        url = self.get_send_otp_url()
        logger.info(f"[DCB_HUB] Sending OTP to {msisdn=} via {url=}")
        try:
            with httpx.Client() as client:
                body = {
                    "sourceMsisdn": "Chinguitel",
                    "destinationMsisdn": msisdn,
                    "message": f"Your OTP code is {otp}",
                    "smsType": "NORMAL",
                    "deliveryReceipt": False
                }
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "api-key": self.get_api_key(),
                    "Tenant": os.getenv("ESIM_HUB_TENANT_KEY")
                }
                response = client.request(method="POST",
                                          url=url,
                                          headers=headers,
                                          json=body,
                                          timeout=120)
                json_response = response.json()
                if json_response.get("status") == "success":
                    return True
                else:
                    logger.error(f"[DCB_HUB] Failed to send OTP: {json_response}")
                    return False
        except Exception as e:
            logger.error(f"Error sending OTP to {msisdn}: {e}")
            return False

    async def deduct_balance(self, msisdn: str, amount: float) -> bool:
        url = self.get_charge_url()
        logger.info(f"[DCB_HUB] deducting balance for  {msisdn=}")
        try:
            with httpx.Client() as client:
                body = {
                    "msisdn": msisdn,
                    "amount": amount,
                    "currency": "XOF",
                    "description": "eSIM Purchase"
                }
                headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "api-key": self.get_api_key()
                }
                response = client.request(method="POST",
                                          url=url,
                                          headers=headers,
                                          json=body,
                                          timeout=120)
                json_response = response.json()
                if json_response.get("status") == "success":
                    return True
                else:
                    logger.error(f"[DCB_HUB] Failed to deduct balance: {json_response}")
                    return False
        except Exception as e:
            logger.error(f"Error deducting balance for {msisdn}: {e}")
            return False
