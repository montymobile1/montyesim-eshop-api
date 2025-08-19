import os
from datetime import datetime
from typing import List

from loguru import logger

from app.config.db import PromotionRuleAction, Beneficiary, PromotionRuleEvent, ConfigKeysEnum
from app.config.utils import get_config
from app.exceptions import CustomException
from app.models.promotion import PromotionModel
from app.models.promotion import PromotionRuleModel
from app.repo import PromotionRepo, PromotionRuleRepo, PromotionUsageRepo, UserRepo
from app.repo.bundle_repo import BundleRepo
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO
from app.schemas.promotion import PromotionCodeDetailsResponse, PromotionValidationRequest, PromotionCheck, \
    ReferralRewardRequest, PromotionHistoryDto
from app.schemas.response import Response, ResponseHelper
from app.services.currency_service import CurrencyService
from app.services.user_wallet_service import UserWalletService


class PromotionService:

    def __init__(self):
        self.__promotion_repo = PromotionRepo()
        self.__promotion_rule_repo = PromotionRuleRepo()
        self.__promotion_usage_repo = PromotionUsageRepo()
        self.__user_repo = UserRepo()
        self.__user_wallet_service = UserWalletService()
        self.__bundle_repo = BundleRepo()
        self.__currency_service = CurrencyService()

    async def referral_code_rewards(self, referral_reward_request: ReferralRewardRequest, user_id: str,
                                    device_id: str = None) -> Response:
        promotion_code_details = self.code_type_and_get_rule(referral_reward_request.referral_code,
                                                             user_id, device_id)
        await self.add_reward(rule_id=promotion_code_details.data.rule_id, user_id=user_id,
                              bundle_id=referral_reward_request.bundle_code, code=referral_reward_request.referral_code)
        return ResponseHelper.success_data_response_with_message(None,
                                                                 "Success",
                                                                 0)

    async def history(self, user_id: str) -> Response[List[PromotionHistoryDto]]:
        promotion_usages = self.__promotion_usage_repo.list(where={"user_id": user_id, "status": "completed"})

        promotion_history_dto = []

        for promotion_usage in promotion_usages:
            promotion_name = ""
            if promotion_usage.referral_code is not None:
                referral_user = self.__user_repo.get_first_by(where={}, filters={
                    self.__user_repo.referral_code_key(): promotion_usage.referral_code})
                name = referral_user.email
            else:
                from app.services.bundle_service import BundleService
                bundle_service = BundleService()
                bundle = await bundle_service.get_bundle(promotion_usage.bundle_id,
                                                         os.getenv("DEFAULT_CURRENCY"), "en")
                name = bundle.data.bundle_name
                promotion = self.__promotion_repo.get_first_by(where={"code": promotion_usage.promotion_code})
                promotion_name = promotion.name
            promotion_history_dto.append(DtoMapper.to_promotion_history_dto(promotion_usage=promotion_usage, name=name,
                                                                            promotion_name=promotion_name))

        return ResponseHelper.success_data_response(data=promotion_history_dto, total_count=len(promotion_history_dto))

    async def validate_promotion_code(self, promotion_validation_request: PromotionValidationRequest, x_currency: str,
                                      user_id: str, device_id: str) -> Response[BundleDTO]:
        response = self.code_type_and_get_rule(promotion_validation_request.promo_code, user_id, device_id)
        from app.services.bundle_service import BundleService
        bundle_service = BundleService()
        bundle_response = await bundle_service.get_bundle(bundle_id=promotion_validation_request.bundle_code,
                                                          currency_name=x_currency, locale="en")
        bundle: BundleDTO = bundle_response.data
        promotion_check = self.check_promotion_reward(rule_id=response.data.rule_id,
                                                      bundle_id=promotion_validation_request.bundle_code,
                                                      promo_code=promotion_validation_request.promo_code,
                                                      x_currency=x_currency)

        rate = self.__currency_service.get_rate_by_currency(x_currency)

        if promotion_check.amount < 0:
            promotion_check.amount = 0

        if promotion_check.amount >= 0 and promotion_check.type in [PromotionRuleAction.DISCOUNT_AMOUNT.value,
                                                                    PromotionRuleAction.DISCOUNT_PERCENTAGE.value]:
            bundle.original_price = promotion_check.amount
            bundle.price_display = f'{round(promotion_check.amount, 2):.2f} {x_currency}'
            logger.info(f"applying promotion code for bundle with {promotion_check.amount=} {promotion_check}")
        return ResponseHelper.success_data_response_with_message(
            DtoMapper.bundle_currency_update(bundle=bundle, rate=rate, currency=x_currency), promotion_check.message, 1)

    def code_type_and_get_rule(self, promotion_code: str, user_id: str, device_id: str = None) -> Response[
        PromotionCodeDetailsResponse]:
        if not self.is_referral_code(promotion_code):

            promotion: PromotionModel = self.__promotion_repo.get_first_by(where={"code": promotion_code})

            if promotion is not None:
                code_type = "PROMOTION"
                rule_id = promotion.rule_id
                self.__validate_promotion(promotion=promotion, user_id=user_id, promotion_code=promotion_code,
                                          device_id=device_id)
            else:
                logger.error("promotion code not found")
                raise CustomException(code=400, name="code not recorded", details="promotion code not found")
        else:
            code_type = "REFERRAL"
            rule_id = get_config(ConfigKeysEnum.DEFAULT_REFERRAL_RULE_ID)
            self.__validate_referral(user_id=user_id, promotion_code=promotion_code, rule_id=rule_id)

        response = PromotionCodeDetailsResponse(code_type=code_type, rule_id=rule_id)
        return ResponseHelper.success_data_response(response, 1)

    @staticmethod
    def convert_timestamp(date_str: str, date_format: str = "%Y-%m-%dT%H:%M:%S") -> datetime:
        return datetime.strptime(date_str, date_format)

    def check_promotion_reward(self, rule_id: str, bundle_id, promo_code: str,
                               x_currency: str) -> PromotionCheck | None:
        promotion_rule = self.__promotion_rule_repo.get_first_by({"id": rule_id})
        is_referral = self.is_referral_code(promo_code)
        if promotion_rule is None:
            raise CustomException(code=400, name="PROMOTION_RULE_MISSING", details="promotion rule is missing")

        action_id = promotion_rule.promotion_rule_action_id
        event_id = promotion_rule.promotion_rule_event_id
        beneficiary = promotion_rule.beneficiary

        bundle = self.__bundle_repo.get_bundle_by_id(bundle_id=bundle_id) if bundle_id else None
        self.__validate_rule_constraints(event_id, action_id, bundle, is_referral, beneficiary)
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        if not is_referral:
            promotion_model: PromotionModel = self.__promotion_repo.get_first_by({"code": promo_code})
            if promotion_model is None:
                raise CustomException(code=400, name="INVALID_INPUT",
                                      details="code is promotion code, should have promotion model")

            bundle_codes = promotion_model.bundle_code.split(",") if promotion_model.bundle_code else []

            if len(bundle_codes) > 0:
                logger.info(f"promotion model bundle code: {promotion_model.bundle_code}")
                if bundle_id not in bundle_codes:
                    logger.error(
                        f"Bundle code {bundle_id} does not match with promotion bundle code {promotion_model.bundle_code}")
                    raise CustomException(code=400, name="INVALID_BUNDLE_CODE",
                                          details="Bundle code does not match with promotion bundle code")

            amount = promotion_model.amount
        else:
            amount = float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT))

        if action_id == PromotionRuleAction.DISCOUNT_AMOUNT.value:
            response = PromotionCheck(amount=max(bundle.original_price - amount, 0),
                                      message=f"Discount Amount {rate * amount} {x_currency}")
            return response

        if action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE.value:
            discounted = bundle.original_price * amount / 100
            discounted = round(discounted, 2)
            response = PromotionCheck(amount=bundle.original_price - discounted,
                                      message=f"Discount Percentage {amount} %")
            return response

        if action_id in [PromotionRuleAction.CASHBACK_AMOUNT.value, PromotionRuleAction.CASHBACK_PERCENTAGE.value]:
            cashback_amount = amount
            if action_id == PromotionRuleAction.CASHBACK_PERCENTAGE.value:
                cashback_amount = (bundle.original_price * amount) / 100
                cashback_amount = round(cashback_amount, 2)
                response = PromotionCheck(amount=0, message=f"Cashback Percentage {cashback_amount}%", type=action_id)
                return response
            response = PromotionCheck(amount=0, message=f"Cashback Amount {(cashback_amount * rate)} {x_currency}",
                                      type=action_id)
            return response
        return None

    async def add_reward(self, rule_id: str, user_id: str, bundle_id, code: str) -> float:
        promotion_rule = self.__promotion_rule_repo.get_first_by({"id": rule_id})
        is_referral = self.is_referral_code(code)
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
                                                 filters={self.__user_repo.referral_code_key(): code})
            referrer_user_id = user.id
        else:
            promotion_model: PromotionModel = self.__promotion_repo.get_first_by({"rule_id": rule_id, "code": code})
            if promotion_model is None:
                raise CustomException(code=400, name="INVALID_INPUT",
                                      details="code is promotion code, should have promotion model")
            amount = promotion_model.amount

        if action_id == PromotionRuleAction.DISCOUNT_AMOUNT.value:
            return await self.__handle_discount(bundle.price, amount, beneficiary, user_id, referrer_user_id, code,
                                                is_referral, bundle)

        if action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE.value:
            discounted = bundle.price * amount / 100
            return await self.__handle_discount(bundle.price, discounted, beneficiary, user_id, referrer_user_id, code,
                                                is_referral, bundle)

        if action_id in [PromotionRuleAction.CASHBACK_AMOUNT.value, PromotionRuleAction.CASHBACK_PERCENTAGE.value]:
            cashback_amount = amount
            if action_id == PromotionRuleAction.CASHBACK_PERCENTAGE.value:
                cashback_amount = bundle.price * amount / 100
            return await self.__handle_cashback(cashback_amount, beneficiary, user_id, referrer_user_id, code,
                                                is_referral, promotion_rule.promotion_rule_event_id, bundle)

        return 0

    async def __handle_cashback(self, amount: float, beneficiary: str, user_id: str, referrer_user_id: str,
                                code: str, is_referral: bool, event_id, bundle: BundleDTO):
        logger.info(
            f"handle_cashback {amount=} {beneficiary=} {user_id=} {referrer_user_id=} {code=} {event_id=} {bundle=}")
        self._insert_promotion_usage(user_id, amount, "pending", code, is_referral, bundle)
        return amount

    async def __handle_cashback_after_success_create_order(self, amount: float, beneficiary: int, user_id: str,
                                                           referrer_user_id: str):
        if beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
            logger.info(f"Adding cashback for user {user_id} with amount {amount}")
            await self.__user_wallet_service.add_wallet_transaction(amount, user_id)

        if beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            logger.info(f"Adding cashback for user {user_id} with amount {amount}")
            await self.__user_wallet_service.add_wallet_transaction(amount, referrer_user_id)

    async def __handle_discount(self, original_price: float, discount: float, beneficiary: str,
                                user_id: str, referrer_user_id: str, code: str, is_referral: bool,
                                bundle: BundleDTO) -> float:
        if beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
            self._insert_promotion_usage(user_id, discount, "pending", code, is_referral, bundle)

        if beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            self._insert_promotion_usage(referrer_user_id, discount, "pending", code, is_referral, bundle)

        return max(original_price - discount, 0)

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
            "bundle_id": bundle_id
        })

    async def update_promotion_usage(self, user_id: str, code: str, status: str, rule_id: str, paid_amount: float = 0):
        data = {"status": status}
        is_referral = self.is_referral_code(code)
        conditions = {"user_id": user_id, "promotion_code": code} if not is_referral else {"user_id": user_id, "referral_code": code}
        self.__promotion_usage_repo.update_by(where=conditions, data=data)
        if status == "completed" and rule_id != "0" and is_referral:
            rule_promotion: PromotionRuleModel = self.__promotion_rule_repo.get_by_id(record_id=rule_id)
            usages = self.__promotion_usage_repo.list(where={"promotion_code": code, "status": "completed"})
            self.__promotion_repo.update_by(where={"code": code}, data={"times_used": len(usages)})
            if (rule_promotion.promotion_rule_action_id == PromotionRuleAction.CASHBACK_PERCENTAGE
                    or rule_promotion.promotion_rule_action_id == PromotionRuleAction.CASHBACK_AMOUNT):
                promotion: PromotionModel = self.__promotion_repo.get_first_by(where={"code": code})
                if rule_promotion.promotion_rule_action_id == PromotionRuleAction.CASHBACK_PERCENTAGE:
                    amount = (float(paid_amount) * float(promotion.amount)) / 100
                else:
                    if promotion:
                        amount = float(promotion.amount)
                    else:
                        amount = float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT))
                rate = self.__currency_service.get_rate_by_currency(os.getenv("DEFAULT_CURRENCY"))
                amount = round(amount * float(rate), 2)
                await self.__handle_cashback_after_success_create_order(amount, Beneficiary.REFERRER.value,
                                                                        user_id, "")

    async def check_referral_rewards_after_buy_bundle(self, user_id: str):
        promotion_usage = self.__promotion_usage_repo.get_first_by(where={"user_id": user_id, "status": "pending"})
        if promotion_usage:
            amount = float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT))
            rule_id = get_config(ConfigKeysEnum.DEFAULT_REFERRAL_RULE_ID)
            if promotion_usage.referral_code is None:
                logger.error("Referral code is missing in promotion usage")
                return
            user = self.__user_repo.get_first_by(where={},
                                                 filters={
                                                     self.__user_repo.referral_code_key(): promotion_usage.referral_code})
            if user is None:
                logger.error("User not found for the given referral code")
                return
            referrer_user_id = user.id

            promotion_rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by({"id": rule_id})
            beneficiary = promotion_rule.beneficiary

            if beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
                await self.__user_wallet_service.add_wallet_transaction(amount, user_id)

            action_id = promotion_rule.promotion_rule_action_id
            if action_id in [PromotionRuleAction.CASHBACK_AMOUNT.value, PromotionRuleAction.CASHBACK_PERCENTAGE.value]:
                await self.__user_wallet_service.add_wallet_transaction(amount, referrer_user_id)
            await self.update_promotion_usage(user_id, promotion_usage.referral_code, "completed", rule_id)
            self.__promotion_usage_repo.update_by(
                where={"user_id": user_id, "promotion_code": promotion_usage.referral_code},
                data={"status": "completed"})

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

    def __validate_promotion(self, promotion: PromotionModel, promotion_code: str, user_id: str, device_id: str = None):
        rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": promotion.rule_id})
        current_date = datetime.now()

        if not promotion.is_active:
            raise CustomException(code=404, name="Promotion Not Active",
                                  details="promotion not active")
        if promotion.times_used >= rule.max_usage:
            raise CustomException(code=404, name="Promotion Reached Max Usage",
                                  details="times used is full")
        if not self.convert_timestamp(promotion.valid_from) < current_date <= self.convert_timestamp(
                promotion.valid_to):
            raise CustomException(code=404, name="Promotion Expired",
                                  details="promotion not active")
        promotion_usage = self.__promotion_usage_repo.list(
            where={"user_id": user_id, "promotion_code": promotion_code, "status": "completed", "device_id": device_id})
        if promotion_usage:
            raise CustomException(code=404, name="Promotion Already Used",
                                  details="Promotion Already Used")

    def __validate_referral(self, user_id: str, promotion_code: str, rule_id: str):

        promotion_usage = self.__promotion_usage_repo.list(
            where={"user_id": user_id, "referral_code": promotion_code})

        if promotion_usage:
            logger.error("Referral code already used")
            raise CustomException(code=400, name="Referral code already used", details="Referral Code Already Used")

        rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": rule_id})
        if not rule:
            raise CustomException(code=404, name="promotion rule not found",
                                  details="promotion rule not found")

        promotion_referral_usage = self.__promotion_usage_repo.list(where={"referral_code": promotion_code})
        if len(promotion_referral_usage) > rule.max_usage:
            raise CustomException(code=404, name="promotion max usage validation",
                                  details="times used is full")

    def is_referral_code(self, referral_code: str) -> bool:
        return self.__user_repo.get_first_by(where={},
                                             filters={self.__user_repo.referral_code_key(): referral_code}) is not None
