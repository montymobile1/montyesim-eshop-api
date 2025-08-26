import os
from datetime import datetime
from typing import List, Literal

from loguru import logger

from app.config.constants import UserWalletTransactionSource
from app.config.db import PromotionRuleAction, Beneficiary, PromotionRuleEvent, ConfigKeysEnum, PromotionStatusEnum
from app.config.utils import get_config
from app.exceptions import CustomException
from app.models.promotion import PromotionModel, PromotionUsageModel
from app.models.promotion import PromotionRuleModel
from app.models.user import UsersCopyModel
from app.repo import PromotionRepo, PromotionRuleRepo, PromotionUsageRepo, UserRepo, UserProfileRepo
from app.repo.bundle_repo import BundleRepo
from app.schemas.dto_mapper import DtoMapper
from app.schemas.home import BundleDTO
from app.schemas.promotion import PromotionCodeDetailsResponse, PromotionValidationRequest, ReferralRewardRequest, \
    PromotionHistoryDto, PromotionValidationResponse, ReferralInfoDto
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
        self.__user_profile_repo = UserProfileRepo()

    async def referral_code_rewards(self, referral_reward_request: ReferralRewardRequest, user_id: str,
                                    device_id: str = None) -> Response:
        promotion_code_details = self.code_type_and_get_rule(referral_reward_request.referral_code,
                                                             user_id, device_id)
        await self.add_reward(rule_id=promotion_code_details.data.rule_id, user_id=user_id,
                              bundle_id=referral_reward_request.bundle_code, code=referral_reward_request.referral_code)
        return ResponseHelper.success_data_response_with_message(None,
                                                                 "Success",
                                                                 0)

    async def history(self, user_id: str, x_currency: str) -> Response[List[PromotionHistoryDto]]:
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        transactions = self.__user_wallet_service.get_wallet_transactions(user_id=user_id)
        history = []
        for transaction in transactions:
            promotion_history = PromotionHistoryDto(
                is_referral=transaction.source != UserWalletTransactionSource.CASHBACK,
                amount=f"{round(transaction.amount * rate, 2)} {x_currency}",
                name=transaction.source,
                promotion_name="",
                date=transaction.created_at)
            history.append(promotion_history)
        return ResponseHelper.success_data_response_with_message(history, "Success", len(history))

    async def validate_promotion_code(self, promotion_validation_request: PromotionValidationRequest, x_currency: str,
                                      user_id: str, device_id: str) -> Response[BundleDTO]:
        from app.services.bundle_service import BundleService
        bundle_service = BundleService()
        bundle_response = await bundle_service.get_bundle(bundle_id=promotion_validation_request.bundle_code,
                                                          currency_name=x_currency, locale="en")
        bundle: BundleDTO = bundle_response.data
        validation_response = await self.validate_promo_code(code=promotion_validation_request.promo_code,
                                                             bundle=bundle, user_id=user_id, device_id=device_id,
                                                             currency=x_currency)
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        return ResponseHelper.success_data_response_with_message(
            DtoMapper.bundle_currency_update(bundle=validation_response.bundle, rate=rate, currency=x_currency),
            validation_response.message, 1)

    async def validate_promo_code(self, code: str, user_id: str, bundle: BundleDTO, device_id: str,
                                  currency: str, apply_usage: bool = False) -> PromotionValidationResponse | None:
        # check if the code is promotion
        is_referral = self.is_referral_code(code)
        rate = self.__currency_service.get_rate_by_currency(currency)
        if is_referral:
            rule_id = get_config(ConfigKeysEnum.DEFAULT_REFERRAL_RULE_ID)
            percentage = float(get_config(ConfigKeysEnum.REFERRAL_CODE_PERCENTAGE))
            amount = float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT))
            rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": rule_id})
            self.__validate_referral(user_id=user_id, promotion_code=code, rule_id=rule_id, device_id=device_id)
            referrer_user: UsersCopyModel = self.__user_repo.get_by_id(record_id=user_id)
            referred_user: UsersCopyModel = self.__user_repo.get_first_by(where={},
                                                                          filters={
                                                                              self.__user_repo.referral_code_key(): code})
            if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_AMOUNT.value:
                bundle.original_price = max(bundle.original_price - amount, 0)
                bundle.price_display = f'{round(bundle.original_price, 2):.2f} {currency}'
                if 0.5 > bundle.original_price > 0:
                    raise CustomException(code=400, name="Promo Code Can not be used for this bundle",
                                          details="Promo Code Can not be used for this bundle")
                if apply_usage:
                    await self.__handle_cashback(amount=amount, beneficiary=str(rule.beneficiary),
                                                 user_id=referrer_user.id, referrer_user_id=referrer_user.id, code=code,
                                                 is_referral=True,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 device_id=device_id)
                return PromotionValidationResponse(bundle=bundle, rule_id=rule_id,
                                                   message=f"Discount Amount {amount * rate} {currency}")
            elif rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE.value:
                discounted = (bundle.original_price * percentage) / 100
                discounted = round(discounted, 2)
                bundle.original_price = max(bundle.original_price - discounted, 0)
                if 0.5 > bundle.original_price > 0:
                    raise CustomException(code=400, name="Promo Code Can not be used for this bundle",
                                          details="Promo Code Can not be used for this bundle")
                bundle.price_display = f'{round(bundle.original_price, 2):.2f} {currency}'
                if apply_usage:
                    await self.__handle_cashback(amount=amount, beneficiary=str(rule.beneficiary),
                                                 user_id=referrer_user.id, referrer_user_id=referrer_user.id, code=code,
                                                 is_referral=True,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 referred_to=referred_user.email, device_id=device_id)
                return PromotionValidationResponse(bundle=bundle,
                                                   rule_id=rule_id,
                                                   message=f"Discount Percentage {percentage} %")
            else:
                if apply_usage:
                    await self.__handle_cashback(amount=amount, beneficiary=str(rule.beneficiary),
                                                 user_id=referrer_user.id, referrer_user_id=referrer_user.id, code=code,
                                                 is_referral=True,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 referred_to=referred_user.email, device_id=device_id)
                    await self.__handle_cashback(amount=amount, beneficiary=str(rule.beneficiary),
                                                 user_id=referred_user.id, referrer_user_id=referrer_user.id, code=code,
                                                 is_referral=True,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 referred_to=referrer_user.email, device_id=device_id)
                return PromotionValidationResponse(bundle=bundle, rule_id=rule_id,
                                                   message=f"Cashback Amount {amount * rate} {currency}")

        else:
            promotion: PromotionModel = self.__promotion_repo.get_first_by(where={"code": code})
            if promotion is None:
                logger.error(f"promotion code {code} not found")
                raise CustomException(code=400, name="Promotion Not Found", details="Not Found")
            self.__validate_promotion(promotion=promotion, user_id=user_id, device_id=device_id)
            bundle_codes = promotion.bundle_code.split(",") if promotion.bundle_code else []
            if len(bundle_codes) > 0:
                logger.info(f"promotion model bundle code: {promotion.bundle_code}")
                if bundle.bundle_code not in bundle_codes:
                    logger.error(
                        f"Bundle code {bundle.bundle_code} does not match with promotion bundle code {promotion.bundle_code}")
                    raise CustomException(code=400, name="INVALID_BUNDLE_CODE",
                                          details="Bundle code does not match with promotion bundle code")
            rule = self.__promotion_rule_repo.get_first_by(where={"id": promotion.rule_id})
            if rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_AMOUNT.value:
                bundle.original_price = max(bundle.original_price - promotion.amount, 0)
                if 0.5 > bundle.original_price > 0:
                    raise CustomException(code=400, name="Promo Code Can not be used for this bundle",
                                          details="Promo Code Can not be used for this bundle")
                bundle.price_display = f'{round(bundle.original_price, 2):.2f} {currency}'
                if apply_usage:
                    self._insert_promotion_usage(user_id=user_id, amount=promotion.amount, status="pending", code=code,
                                                 is_referral=is_referral, bundle=bundle, device_id=device_id)
                return PromotionValidationResponse(bundle=bundle,
                                                   rule_id=rule.id,
                                                   message=f"Discount Amount {promotion.amount * rate} {currency}")
            elif rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE.value:
                discounted = bundle.original_price * promotion.amount / 100
                discounted = round(discounted, 2)
                bundle.original_price = max(bundle.original_price - discounted, 0)
                if 0.5 > bundle.original_price > 0:
                    raise CustomException(code=400, name="Promo Code Can not be used for this bundle",
                                          details="Promo Code Can not be used for this bundle")
                bundle.price_display = f'{round(bundle.original_price, 2):.2f} {currency}'
                if apply_usage:
                    self._insert_promotion_usage(user_id=user_id, amount=discounted, status="pending", code=code,
                                                 is_referral=is_referral, bundle=bundle, device_id=device_id)
                return PromotionValidationResponse(bundle=bundle,
                                                   rule_id=rule.id,
                                                   message=f"Discount Percentage {promotion.amount} %")
            else:
                if rule.promotion_rule_action_id == PromotionRuleAction.CASHBACK_PERCENTAGE.value:
                    amount = (bundle.original_price * promotion.amount) / 100
                    message = f"Cashback Percentage ({promotion.amount}%) {amount * rate} {currency}"
                else:
                    amount = promotion.amount
                    message = f"Cashback Amount {amount * rate} {currency}"
                if apply_usage:
                    await self.__handle_cashback(amount=amount, beneficiary=str(rule.beneficiary),
                                                 user_id=user_id, referrer_user_id="0", code=code, is_referral=False,
                                                 event_id=rule.promotion_rule_event_id, bundle=bundle,
                                                 device_id=device_id)
                return PromotionValidationResponse(bundle=bundle, rule_id=rule.id,
                                                   message=message)

    def code_type_and_get_rule(self, promotion_code: str, user_id: str, device_id: str = None) -> Response[
        PromotionCodeDetailsResponse]:
        if not self.is_referral_code(promotion_code):

            promotion: PromotionModel = self.__promotion_repo.get_first_by(where={"code": promotion_code})

            if promotion is not None:
                code_type = "PROMOTION"
                rule_id = promotion.rule_id
                self.__validate_promotion(promotion=promotion, user_id=user_id,
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
            amount = float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT))
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
                                code: str, is_referral: bool, event_id, bundle: BundleDTO, referred_to: str = None,
                                device_id: str = None):
        logger.info(
            f"handle_cashback {amount=} {beneficiary=} {user_id=} {referrer_user_id=} {code=} {event_id=} {bundle=}")
        self._insert_promotion_usage(user_id=user_id, amount=amount, status="pending", code=code,
                                     is_referral=is_referral, bundle=bundle, referred_to=referred_to,
                                     device_id=device_id)
        return amount

    async def __handle_cashback_after_success_create_order(self, amount: float, beneficiary: int, user_id: str,
                                                           referrer_user_id: str):
        if beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
            logger.info(f"Adding cashback for user {user_id} with amount {amount}")
            await self.__user_wallet_service.add_wallet_transaction(amount=amount, user_id=user_id,
                                                                    source=UserWalletTransactionSource.CASHBACK_REFERRAL)

        if beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            logger.info(f"Adding cashback for user {user_id} with amount {amount}")
            await self.__user_wallet_service.add_wallet_transaction(amount=amount, user_id=referrer_user_id,
                                                                    source=UserWalletTransactionSource.CASHBACK_REFERRAL)

    async def __handle_discount(self, original_price: float, discount: float, beneficiary: str,
                                user_id: str, referrer_user_id: str, code: str, is_referral: bool,
                                bundle: BundleDTO) -> float:
        if beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
            self._insert_promotion_usage(user_id, discount, "pending", code, is_referral, bundle)

        if beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            self._insert_promotion_usage(referrer_user_id, discount, "pending", code, is_referral, bundle)

        return max(original_price - discount, 0)

    def _insert_promotion_usage(self, user_id, amount, status, code, is_referral, bundle, referred_to: str = None,
                                device_id: str = None):
        bundle_id = None
        if bundle:
            bundle_id = bundle.bundle_code
        self.__promotion_usage_repo.create(data={
            "user_id": user_id,
            "amount": amount,
            "promotion_code": code if not is_referral else None,
            "referral_code": code if is_referral else None,
            "status": status,
            "bundle_id": bundle_id,
            "referred_to": referred_to,
            "device_id": device_id
        })

    async def update_promotion_usage(self, user_id: str, code: str, status: str, rule_id: str, paid_amount: float = 0):
        data = {"status": status}
        is_referral = self.is_referral_code(code)
        conditions = {"user_id": user_id, "promotion_code": code} if not is_referral else {"user_id": user_id,
                                                                                           "referral_code": code}
        referrer_user = self.__user_repo.get_first_by(where={},
                                                      filters={self.__user_repo.referral_code_key(): code})
        self.__promotion_usage_repo.update_by(where=conditions, data=data)
        if referrer_user:
            self.__promotion_usage_repo.update_by(where={"user_id": referrer_user.id, "referral_code": code}, data=data)

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

    def __validate_promotion(self, promotion: PromotionModel, user_id: str, device_id: str = None):
        rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": promotion.rule_id})

        if not promotion.is_active:
            raise CustomException(code=404, name="Promotion Not Active",
                                  details="promotion not active")
        if promotion.times_used >= rule.max_usage:
            raise CustomException(code=404, name="Promotion Reached Max Usage",
                                  details="times used is full")
        promotion_usage = self.__promotion_usage_repo.list(
            where={"user_id": user_id, "promotion_code": promotion.code, "status": "completed", "device_id": device_id})
        if promotion_usage:
            raise CustomException(code=404, name="Promotion Already Used",
                                  details="Promotion Already Used")

    def __validate_referral(self, user_id: str, promotion_code: str, rule_id: str, device_id: str = None):

        referred_user: UsersCopyModel = self.__user_repo.get_first_by(where={},
                                                                      filters={
                                                                          self.__user_repo.referral_code_key(): promotion_code})

        old_profiles = self.__user_profile_repo.list(where={"user_id": user_id})
        if len(old_profiles) > 0:
            raise CustomException(code=400, name="User Has Previous Esim",
                                  details="User already purchased esim before, cannot use referral code")
        user_model: UsersCopyModel = self.__user_repo.get_by_id(user_id)
        if user_model.metadata["referral_code"] and user_model.metadata["referral_code"] == promotion_code:
            raise CustomException(code=400, name="Own Referral Code Can not be used",
                                  details="Own Referral Code Can not be used")

        if referred_user:
            referred_usage = self.__promotion_usage_repo.list(
                where={"referred_to": referred_user.email, "user_id": user_id, "referral_code": promotion_code})
            if len(referred_usage) > 0:
                for usage in referred_usage:
                    if usage.device_id == device_id:
                        logger.error("Referral code already used on this device")
                        raise CustomException(code=400, name="Referral code already used on this device",
                                              details="Referral Code Already Used on this device")
                logger.error("Referral code already used by referred user")
                raise CustomException(code=400, name="Referral code already used",
                                      details="Referral Code Already Used by referred user")

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

    async def apply_promotion_code_after_purchase(self, user_id: str, code: str,
                                                  status: Literal["pending", "failed", "completed"],
                                                  rule_id: str,
                                                  paid_amount: float = 0):
        is_referral = self.is_referral_code(code)
        referrer_user = self.__user_repo.get_first_by(where={},
                                                      filters={self.__user_repo.referral_code_key(): code})
        logger.info(
            f"applying {'referral' if is_referral else 'promotion'} code {code} for user {user_id} with status {status}")
        condition = {"user_id": user_id, "referral_code": code} if is_referral else {"user_id": user_id,
                                                                                     "promotion_code": code}
        if status != "completed":
            self.__promotion_usage_repo.update_by(where=condition, data={"status": status})
            return
        promotion_rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": rule_id})

        if is_referral:
            referred_promotion_usage = self.__promotion_usage_repo.get_first_by(
                where={"user_id": user_id, "status": "pending"})
            referrer_promotion_usage = self.__promotion_usage_repo.get_first_by(
                where={"user_id": referrer_user.id, "status": "pending"})
            if referred_promotion_usage is None and referrer_promotion_usage is None:
                logger.error(f"No pending promotion found for user {user_id} with code {code}")
                self.__promotion_usage_repo.update_by(where=condition, data={"status": "failed"})
                return
            self.__promotion_usage_repo.update_by(where=condition, data={"status": status})
            return await self.__apply_referral_rewards(user_id=user_id, referral_code=code, paid_amount=paid_amount,
                                                       promotion_rule=promotion_rule)
        else:
            usage: PromotionUsageModel = self.__promotion_usage_repo.get_first_by(
                where={"promotion_code": code, "user_id": user_id, "status": "pending"})
            if usage:
                if promotion_rule.promotion_rule_action_id in [PromotionRuleAction.CASHBACK_AMOUNT.value,
                                                               PromotionRuleAction.CASHBACK_PERCENTAGE.value]:
                    rate = self.__currency_service.get_rate_by_currency(os.getenv("DEFAULT_CURRENCY"))
                    amount = round(float(usage.amount) * float(rate), 2)
                    await self.__user_wallet_service.add_wallet_transaction(amount=amount, user_id=user_id,
                                                                            source=UserWalletTransactionSource.CASHBACK_PROMO)
            old_usage = self.__promotion_usage_repo.list(where={"promotion_code": code})
            self.__promotion_repo.update_by(where={"code": code}, data={"times_used": len(old_usage)})
            self.__promotion_usage_repo.update_by(where=condition, data={"status": status})
            return None

    async def __apply_referral_rewards(self, user_id: str, referral_code: str, paid_amount: float,
                                       promotion_rule: PromotionRuleModel):
        referrer_user = self.__user_repo.get_first_by(where={},
                                                      filters={self.__user_repo.referral_code_key(): referral_code})
        rate = self.__currency_service.get_rate_by_currency(os.getenv("DEFAULT_CURRENCY"))
        amount = round(float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT)) * float(rate), 2)
        if referrer_user is None:
            logger.error(f"Referrer User not found for referral code {referral_code}")
            return
        referrer_user_id = referrer_user.id

        if promotion_rule.beneficiary in [Beneficiary.REFERRER.value, Beneficiary.BOTH.value]:
            logger.info(f"Adding cashback for REFERRER user {referrer_user_id} with amount {amount}")
            self.__promotion_usage_repo.update_by(where={"user_id": referrer_user_id, "referral_code": referral_code},
                                                  data={"status": "completed"})
            await self.__user_wallet_service.add_wallet_transaction(amount=amount, user_id=user_id,
                                                                    source=UserWalletTransactionSource.CASHBACK_REFERRAL)

        if promotion_rule.beneficiary in [Beneficiary.REFERRED.value, Beneficiary.BOTH.value]:
            usage = self.__promotion_usage_repo.get_first_by(where={"user_id": user_id, "referral_code": referral_code})
            if usage is None:
                logger.error(f"No pending promotion usage found for user {user_id} with referral code {referral_code}")
                return
            else:
                if promotion_rule.promotion_rule_action_id == PromotionRuleAction.CASHBACK_PERCENTAGE.value:
                    amount = round((paid_amount * float(get_config(ConfigKeysEnum.REFERRAL_CODE_PERCENTAGE, 20))) / 100,
                                   2)
                logger.info(f"Adding cashback for REFERRED user {user_id} with amount {amount}")
                await self.__user_wallet_service.add_wallet_transaction(amount=amount, user_id=user_id,
                                                                        source=UserWalletTransactionSource.CASHBACK_REFERRAL)
        return None

    def cancel_promotion_usage(self, user_id: str, referral_code: str | None, promo_code: str | None):
        if referral_code:
            self.__promotion_usage_repo.update_by(where={"user_id": user_id, "referral_code": referral_code},
                                                  data={"status": PromotionStatusEnum.FAILED.value})
            promotion_usage = self.__promotion_usage_repo.get_first_by(
                where={"user_id": user_id, "referral_code": referral_code})
            if promotion_usage:
                referrer_user = self.__user_repo.get_first_by(where={"email": promotion_usage.referred_to})
                referred_user = self.__user_repo.get_by_id(user_id)
                if referrer_user:
                    self.__promotion_usage_repo.update_by(
                        where={"user_id": referrer_user.id, "referral_code": referral_code,
                               "referred_to": referred_user.email},
                        data={"status": PromotionStatusEnum.FAILED.value})
        if promo_code:
            self.__promotion_usage_repo.update_by(where={"user_id": user_id, "promotion_code": promo_code},
                                                  data={"status": PromotionStatusEnum.FAILED.value})

    def referral_info(self, x_currency: str, locale: str = "en") -> Response[ReferralInfoDto]:
        rate = self.__currency_service.get_rate_by_currency(x_currency)
        rule_id = get_config(ConfigKeysEnum.DEFAULT_REFERRAL_RULE_ID)
        rule: PromotionRuleModel = self.__promotion_rule_repo.get_first_by(where={"id": rule_id})
        if not rule:
            raise CustomException(code=404, name="promotion rule not found",
                                  details="promotion rule not found")

        amount = float(get_config(ConfigKeysEnum.REFERRAL_CODE_AMOUNT)) * float(rate)
        percentage = float(get_config(ConfigKeysEnum.REFERRAL_CODE_PERCENTAGE))
        if rule.promotion_rule_action_id == PromotionRuleAction.CASHBACK_AMOUNT.value:
            message = f"Get {amount} {x_currency} credit for every friend that signs up and completes a purchase. Your friends get {amount} {x_currency} credit for their first purchase."
        elif rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_PERCENTAGE.value:
            message = f"Get {amount} {x_currency} credit for every friend that signs up and completes a purchase. Your friends get {percentage}% off their first purchase."
        elif rule.promotion_rule_action_id == PromotionRuleAction.DISCOUNT_AMOUNT.value:
            message = f"Get {amount} {x_currency} credit for every friend that signs up and completes a purchase. Your friends get {amount} {x_currency} off their first purchase."
        else:
            message = f"Get {amount} {x_currency} credit for every friend that signs up and completes a purchase"
        dto = ReferralInfoDto(amount=round(amount, 2), type=str(rule.promotion_rule_action_id), currency=x_currency,
                              message=message)
        return ResponseHelper.success_data_response(dto, 1)
