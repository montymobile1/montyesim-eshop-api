import os

from datetime import datetime
from typing import Any, Coroutine, List
from venv import logger
from app.models.promotion import PromotionModel, PromotionRuleModel
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO
from app.schemas.promotion import PromotionCodeDetailsResponse, PromotionValidationRequest, PromotionCheck, \
    ReferralRewardRequest, PromotionHistoryDto
from app.schemas.response import Response, ResponseHelper
import os

from app.config.config import esim_hub_service_instance
from app.config.db import PromotionRuleAction, Beneficiary, PromotionRuleEvent
from app.exceptions import CustomException
from app.models.promotion import PromotionModel
from app.repo import PromotionRepo, PromotionRuleRepo, PromotionUsageRepo, UserRepo, UserWalletRepo
from app.repo.bundle_repo import BundleRepo
from app.services.bundle_service import BundleService
from app.services.user_wallet_service import UserWalletService


class PromotionService:

    def __init__(self):
        self.__promotion_repo = PromotionRepo()
        self.__promotion_rule_repo = PromotionRuleRepo()
        self.__promotion_usage_repo = PromotionUsageRepo()
        self.__user_repo = UserRepo()
        self.__esim_hub_service = esim_hub_service_instance()
        self.__user_wallet_service = UserWalletService()
        self.__bundle_repo = BundleRepo()
        self.__bundle_service = BundleService()

    async def referral_code_rewards(self,referral_reward_request : ReferralRewardRequest, user_id :str):
        # try:
        promotion_code_details = self.code_type_and_get_rule(referral_reward_request.referral_code,
                                                                                     user_id)
        amount = await self.add_reward(promotion_code_details.data.rule_id, user_id,
                                                               None, referral_reward_request.referral_code, True)
        return ResponseHelper.success_data_response_with_message(None,

                                                                     "Success",
                                                                     0)

    async def history(self,user_id :str) -> Response[List[PromotionHistoryDto]]:
        promotion_usages = self.__promotion_usage_repo.list(where = {"user_id" : user_id, "status" : "completed"})

        promotion_history_dto= []

        for promotion_usage in promotion_usages:
            name = ""
            promotion_name = ""
            if promotion_usage.referral_code is not None:
                referral_user = self.__user_repo.get_first_by(where={},filters={"metadata ->> referral_code": promotion_usage.referral_code})
                name = referral_user.email
            else:
                bundle = await self.__bundle_service.get_bundle(promotion_usage.bundle_id,os.getenv("DEFAULT_CURRENCY"))
                name = bundle.data.bundle_name
                promotion = self.__promotion_repo.get_first_by(where={"code": promotion_usage.promotion_code})
                promotion_name = promotion.name
            promotion_history_dto.append(DtoMapper.to_promotion_history_dto(promotion_usage= promotion_usage, name = name, promotion_name= promotion_name))

        return ResponseHelper.success_data_response(data=promotion_history_dto,total_count= len(promotion_history_dto))

    async def validate_promotion_code(self, promotion_validation_request: PromotionValidationRequest, x_currency: str,user_id :str) -> Response[BundleDTO]:
        response = self.code_type_and_get_rule(promotion_validation_request.promo_code, user_id)
        bundle_response = await self.__bundle_service.get_bundle(bundle_id=promotion_validation_request.bundle_code,
                                                  currency_name=x_currency)
        bundle:BundleDTO = bundle_response.data
        promotion_check = self.__check_promotion_reward(response.data.rule_id,promotion_validation_request.bundle_code,False)

        if promotion_check.amount > 0:
            bundle.price = promotion_check.amount
            bundle.price_display =  f'{round(promotion_check.amount, 2):.2f} {x_currency}'

        return ResponseHelper.success_data_response_with_message(bundle,promotion_check.message,1)

    def code_type_and_get_rule(self, promotion_code: str, user_id: str) -> Response[PromotionCodeDetailsResponse]:
        if not self.__user_repo.get_first_by(where={},
                                             filters={"metadata ->> referral_code": promotion_code}):

            promotion: PromotionModel = self.__promotion_repo.get_first_by(where={"code": promotion_code})

            if promotion is not None:
                code_type = "PROMOTION"
                rule_id = promotion.rule_id
                rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": rule_id})
                current_date = datetime.now()

                if not promotion.is_active:
                    raise CustomException(code=404, name="promotion active validation",
                                          details="promotion not active")
                if promotion.times_used >= rule.max_usage:
                    raise CustomException(code=404, name="promotion max usage validation",
                                          details="times used is full")
                if not self.convert_timestamp(promotion.valid_from) < current_date <= self.convert_timestamp(
                        promotion.valid_to):
                    raise CustomException(code=404, name="promotion time validation error",
                                          details="promotion not active")
                promotion_usage = self.__promotion_usage_repo.list(
                    where={"user_id": user_id, "promotion_code": promotion_code , "status" : "completed"})
                if promotion_usage:
                    raise CustomException(code=404, name="Promotion Already Used",
                                          details="Promotion Already Used")
            else:
                logger.error("promotion code not found")
                raise CustomException(code=400, name="code not recorded", details="promotion code not found")
        else:
            code_type = "REFERRAL"
            rule_id = os.getenv("DEFAULT_REFERRAL_RULE_ID")

            promotion_usage = self.__promotion_usage_repo.list(
                where={"user_id": user_id, "referral_code": promotion_code})

            if promotion_usage:
                logger.error("Referral code already used")
                raise CustomException(code=400, name="Referral code already used", details="Referral Code Already Used")

            rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": rule_id})

            promotion_referral_usage = self.__promotion_usage_repo.list(where={"referral_code": promotion_code})
            if len(promotion_referral_usage) > rule.max_usage:
                raise CustomException(code=404, name="promotion max usage validation",
                                      details="times used is full")

        response = PromotionCodeDetailsResponse(code_type=code_type, rule_id=rule_id)
        return ResponseHelper.success_data_response(response, 1)

    @staticmethod
    def convert_timestamp(date_str: str, date_format: str = "%Y-%m-%dT%H:%M:%S") -> datetime:
        return datetime.strptime(date_str, date_format)

    def __check_promotion_reward(self, rule_id: str, bundle_id, is_referral: bool) -> PromotionCheck | None:
        promotion_rule = self.__promotion_rule_repo.get_first_by({"id": rule_id})
        if promotion_rule is None:
            raise CustomException(code=400, name="PROMOTION_RULE_MISSING", details="promotion rule is missing")

        action_id = promotion_rule.promotion_rule_action_id
        event_id = promotion_rule.promotion_rule_event_id
        beneficiary = promotion_rule.beneficiary

        bundle = self.__bundle_repo.get_bundle_by_id(bundle_id=bundle_id) if bundle_id else None
        self.__validate_rule_constraints(event_id, action_id, bundle, is_referral, beneficiary)

        promotion_model: PromotionModel = self.__promotion_repo.get_first_by({"rule_id": rule_id})
        if promotion_model is None:
            raise CustomException(code=400, name="INVALID_INPUT",
                                  details="code is promotion code, should have promotion model")

        amount = promotion_model.amount

        if action_id == PromotionRuleAction.DISCOUNT_AMOUNT.value:
             response = PromotionCheck(amount = bundle.price - amount,message=f"Discount Amount {amount}")
             return response

        if action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE.value:
            discounted = bundle.price * amount / 100
            discounted = round(discounted, 2)
            response = PromotionCheck(amount= bundle.price - discounted, message=f"Discount Amount {discounted}")
            return response

        if action_id in [PromotionRuleAction.CASHBACK_AMOUNT.value, PromotionRuleAction.CASHBACK_PERCENTAGE.value]:
            cashback_amount = amount
            if action_id == PromotionRuleAction.CASHBACK_PERCENTAGE.value:
                cashback_amount = bundle.price * amount / 100
                cashback_amount = round(cashback_amount, 2)
                response = PromotionCheck(amount=0, message=f"Cash Back Amount {cashback_amount}")
                return response
            response = PromotionCheck(amount=0, message=f"Cash Back Amount {cashback_amount}")
            return response
        return None


    async def add_reward(self, rule_id: str, user_id: str, bundle_id, code: str,
                         is_referral: bool) -> float:
        promotion_rule = self.__promotion_rule_repo.get_first_by({"id": rule_id})
        if promotion_rule is None:
            raise CustomException(code=400, name="PROMOTION_RULE_MISSING", details="promotion rule is missing")

        action_id = promotion_rule.promotion_rule_action_id
        event_id = promotion_rule.promotion_rule_event_id
        beneficiary = promotion_rule.beneficiary


        referrer_user_id = 0

        bundle = self.__bundle_repo.get_bundle_by_id(bundle_id=bundle_id) if bundle_id else None
        self.__validate_rule_constraints(event_id, action_id, bundle, is_referral, beneficiary)

        if is_referral:
            amount = float(os.getenv("REFERRAL_CODE_AMOUNT"))
            user = self.__user_repo.get_first_by(where={},
                                                 filters={"metadata ->> referral_code": code})
            referrer_user_id = user.id
        else:
            promotion_model: PromotionModel = self.__promotion_repo.get_first_by({"rule_id": rule_id})
            if promotion_model is None:
                raise CustomException(code=400, name="INVALID_INPUT",
                                      details="code is promotion code, should have promotion model")
            amount = promotion_model.amount

        if action_id == PromotionRuleAction.DISCOUNT_AMOUNT.value:
            return await self.__handle_discount(bundle.price, amount, beneficiary, user_id, referrer_user_id, code,
                                                is_referral,bundle)

        if action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE.value:
            discounted = bundle.price * amount / 100
            return await self.__handle_discount(bundle.price, discounted, beneficiary, user_id, referrer_user_id, code,
                                                is_referral,bundle)

        if action_id in [PromotionRuleAction.CASHBACK_AMOUNT.value, PromotionRuleAction.CASHBACK_PERCENTAGE.value]:
            cashback_amount = amount
            if action_id == PromotionRuleAction.CASHBACK_PERCENTAGE.value:
                cashback_amount = bundle.price * amount / 100
            return await self.__handle_cashback(cashback_amount, beneficiary, user_id, referrer_user_id, code,
                                                is_referral,promotion_rule.promotion_rule_event_id,bundle)

        return 0


    async def __handle_cashback(self, amount: float, beneficiary: str, user_id: str, referrer_user_id: str,
                                code: str, is_referral: bool,event_id, bundle: BundleDTO):
        self._insert_promotion_usage(user_id, amount, "pending", code, is_referral, bundle)
        # if beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
        #     if event_id == PromotionRuleEvent.CREATE_ORDER.value:
        #         self._insert_promotion_usage(user_id, amount, "pending", code, is_referral,bundle)
        #     else:
        #         # await self.__user_wallet_service.add_wallet_transaction(amount, user_id)
        #         self._insert_promotion_usage(user_id, amount, "pending", code, is_referral,bundle)
        #
        # if beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
        #     if event_id == PromotionRuleEvent.CREATE_ORDER.value:
        #         self._insert_promotion_usage(referrer_user_id, amount, "pending", code, is_referral,bundle)
        #     else:
        #         # await self.__user_wallet_service.add_wallet_transaction(amount, referrer_user_id)
        #         self._insert_promotion_usage(referrer_user_id, amount, "pending", code, is_referral,bundle)

        return amount

    async def __handle_cashback_after_success_create_order(self, amount: float, beneficiary: int, user_id: str, referrer_user_id: str):
        if beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
            await self.__user_wallet_service.add_wallet_transaction(amount, user_id)

        if beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            await self.__user_wallet_service.add_wallet_transaction(amount, referrer_user_id)


    async def __handle_discount(self, original_price: float, discount: float, beneficiary: str,
                                user_id: str, referrer_user_id: str, code: str, is_referral: bool,bundle:BundleDTO) -> float:
        if beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
            self._insert_promotion_usage(user_id, discount, "pending", code, is_referral,bundle)

        if beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            self._insert_promotion_usage(referrer_user_id, discount, "pending", code, is_referral,bundle)

        return original_price - discount


    def _insert_promotion_usage(self, user_id, amount, status, code, is_referral, bundle):
        bundle_id = None
        if bundle:
            bundle_id = bundle.bundle_code
        self.__promotion_usage_repo.create(data={
            "user_id": user_id,
            "amount": amount,
            "promotion_code": code if not is_referral else None,
            "referral_code": code if is_referral else None,
            "status": status,
            "bundle_id" : bundle_id
        })

    def update_promotion_usage(self,user_id :str,code :str,status: str,rule_id : str,amount:float):
        data = {"status":status}
        self.__promotion_usage_repo.update_by(where={"user_id":user_id,"promotion_code" : code},data = data)
        if status == "completed" and rule_id != "0":
            rule_promotion = self.__promotion_rule_repo.get_by_id(record_id=rule_id)
            if (rule_promotion.promotion_rule_event_id == PromotionRuleAction.CASHBACK_PERCENTAGE
                    or rule_promotion.promotion_rule_event_id == PromotionRuleAction.CASHBACK_AMOUNT):
                self.__handle_cashback_after_success_create_order(amount,Beneficiary.REFERRER.value,user_id,"")


    async def check_referral_rewards_after_buy_bundle(self,user_id: str):
        promotion_usage = self.__promotion_usage_repo.get_first_by(where={"user_id" : user_id , "status" : "pending"})
        if promotion_usage:
            amount = float(os.getenv("REFERRAL_CODE_AMOUNT"))
            rule_id = os.getenv("DEFAULT_REFERRAL_RULE_ID")
            user = self.__user_repo.get_first_by(where={},
                                                 filters={"metadata ->> referral_code": promotion_usage.referral_code})
            referrer_user_id = user.id

            promotion_rule = self.__promotion_rule_repo.get_first_by({"id": rule_id})
            beneficiary = promotion_rule.beneficiary

            if beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
                await self.__user_wallet_service.add_wallet_transaction(amount, user_id)

            if beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
                await self.__user_wallet_service.add_wallet_transaction(amount, referrer_user_id)
            self.update_promotion_usage(user_id,promotion_usage.referral_code,"completed",rule_id,amount)

    @staticmethod
    def __validate_rule_constraints(event_id, action_id, bundle, is_referral, beneficiary):
        if event_id == PromotionRuleEvent.CREATE_ORDER.value and not bundle:
            raise CustomException(code=400, name="BUNDLE_MISSING", details="bundle is missing")

        if action_id != PromotionRuleAction.CASHBACK_AMOUNT.value and not bundle:
            raise CustomException(code=400, name="BUNDLE_MISSING", details="bundle is missing")

        if event_id == PromotionRuleEvent.CREATE_ACCOUNT.value and action_id != PromotionRuleAction.CASHBACK_AMOUNT.value:
            raise CustomException(code=400, name="INVALID_ACTION", details="login event can have only cashback amount")

        if not is_referral and beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            raise CustomException(code=400, name="INVALID_INPUT",
                                  details="promotion rule for promotion can have beneficiary user only")
