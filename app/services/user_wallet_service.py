import os
import threading

from loguru import logger

from app.config.config import create_wallet_top_up_intent, create_payment_ephemeral
from app.config.db import UserOrderType
from app.config.notification_types import send_wallet_top_up_succeeded_notification
from app.config.push_notification_manager import fcm_service
from app.exceptions import CustomException
from app.models.user import UserWalletModel, UserModel
from app.repo import UserWalletRepo, UserOrderRepo, UserWalletTransactionRepo
from app.schemas.bundle import PaymentIntentResponse
from app.schemas.dto_mapper import DtoMapper
from app.schemas.response import Response, ResponseHelper
from app.schemas.user_wallet import UserWalletRequestDto, TopUpWalletRequest
from app.schemas.user_wallet import UserWalletResponse


class UserWalletService:
    def __init__(self):
        self.__user_wallet_repo = UserWalletRepo()
        self.__user_order_repo = UserOrderRepo()
        self.__user_wallet_transaction_repo = UserWalletTransactionRepo()

    async def get_user_wallet_by_id(self, user_wallet_id: str) -> UserWalletResponse | None:
        wallet: UserWalletModel = self.__user_wallet_repo.get_first_by({"id": user_wallet_id})
        if not wallet:
            return None
        return DtoMapper.to_user_wallet_response(wallet)

    async def create_wallet(self, user_wallet_request_dto: UserWalletRequestDto) -> UserWalletResponse:
        wallet = self.__create_wallet(user_id=user_wallet_request_dto.user_id, amount=user_wallet_request_dto.amount,
                                      currency=user_wallet_request_dto.currency)
        return DtoMapper.to_user_wallet_response(wallet)

    async def get_user_wallet_by_user_id(self, user_id: str) -> UserWalletResponse | None:
        wallet: UserWalletModel = self.__user_wallet_repo.get_first_by({"user_id": user_id})
        if not wallet:
            return None
        return DtoMapper.to_user_wallet_response(wallet)

    async def add_wallet_transaction(self, amount: float, user_id: str, source: str = "TopUp") -> Response[
        UserWalletResponse]:
        try:
            user_wallet: UserWalletModel = self.__user_wallet_repo.get_first_by(where={"user_id": user_id})
            if user_wallet is None:
                raise CustomException(code=400, name="wallet not found", details="user wallet not found")

            # if amount < 0:
            #     raise CustomException(code=400, name="amount cannot be negative", details="amount cannot be negative")

            user_wallet.amount += amount
            self.__user_wallet_repo.update_by(where={"user_id": user_id},
                                              data=user_wallet.model_dump())

            self.__user_wallet_transaction_repo.create(data={
                "wallet_id": user_wallet.id,
                "amount": amount,
                "source": source,
                "status" : "success"
            })
            thread = threading.Thread(target=self.__send_push, args=(amount,user_wallet.currency,user_id,))
            thread.start()
            dto = DtoMapper.to_user_wallet_response(user_wallet)
            return ResponseHelper.success_data_response(dto, 1)
        except Exception as e:
            logger.error(str(e))
            raise CustomException(code=400, name="wallet not found", details="user wallet not found")

    async def top_up_wallet(self, top_up_request: TopUpWalletRequest, user: UserModel) -> Response[
        PaymentIntentResponse]:
        currency = os.getenv("DEFAULT_CURRENCY")
        user_wallet = self.__user_wallet_repo.get_first_by(where={"user_id": user.id})
        if not user_wallet:
            user_wallet = self.__create_wallet(user_id=user.user_id, amount=0, currency=currency)
        order = self.__user_order_repo.create(data={
            "user_id": user.id,
            "bundle_id": None,
            "order_type": UserOrderType.WALLET_TOP_UP,
            "amount": round(top_up_request.amount * 100),
            "currency": currency,
            "bundle_data": "-",
            "searched_countries": "-",
            "anonymous_user_id": None,
        })

        intent = create_wallet_top_up_intent(user_email=user.email, amount=round(top_up_request.amount * 100),
                                             currency=currency,
                                             metadata={
                                                 "user_id": user.id,
                                                 "user_wallet_id": user_wallet.id,
                                                 "order_id": order.id,
                                                 "env": os.environ.get("ENVIRONMENT", "DEV"),
                                             })

        order.payment_intent_code = intent.id
        self.__user_order_repo.update_by({"id": order.id}, data=order.model_dump(exclude={"id"}))
        ephemeral = create_payment_ephemeral(intent.customer)
        response = PaymentIntentResponse(publishable_key=os.getenv("STRIPE_PUBLIC_KEY"),
                                         merchant_identifier=os.getenv("MERCHANT_ID"),
                                         payment_intent_client_secret=intent.client_secret,
                                         customer_id=intent.customer,
                                         customer_ephemeral_key_secret=ephemeral.secret,
                                         test_env=not intent.livemode,
                                         merchant_display_name=os.getenv("MERCHANT_DISPLAY_NAME"),
                                         billing_country_code="GB",
                                         order_id=order.id)
        return ResponseHelper.success_data_response(response, 0)

    def __create_wallet(self, user_id: str, amount: float, currency: str):
        wallet = self.__user_wallet_repo.create(data={
            "user_id": user_id,
            "amount": amount,
            "currency": currency
        })
        logger.info(f"creating wallet for user {user_id}")
        return wallet


    def __send_push(self,amount:float,currency:str,user_id:str):
        content = send_wallet_top_up_succeeded_notification(f"{amount} {currency}")
        fcm_service.send_notification_to_user_from_template(content, user_id=user_id)
