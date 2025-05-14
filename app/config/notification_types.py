from dataclasses import dataclass
from enum import IntEnum
from typing import Dict


class NotificationCategoryType(IntEnum):
    BUY_BUNDLE = 1
    BUY_TOP_UP = 2
    REWARD_AVAILABLE = 3  # TODO: update notification text
    CASHBACK_REWARD = 4  # TODO: update notification text
    CONSUMPTION_80_BUNDLE_DETAIL = 5
    PLAN_STARTED = 6
    SHARE_BUNDLE_NOTIFICATION = 7  # TODO: update notification text
    CONSUMPTION_100_BUNDLE_DETAIL = 8  # TODO: update notification text
    WALLET_TOP_UP_SUCCEEDED = 9
    WALLET_TOP_UP_FAILED = 10


@dataclass
class NotificationContent:
    title: str
    message: str
    data: Dict[str, str]
    isSilent: bool = False


def get_notification_content(category: NotificationCategoryType, **kwargs) -> NotificationContent:
    """
    Get notification content based on category type and optional parameters.

    :param category: NotificationCategoryType enum value
    :param kwargs: Additional parameters needed for the notification
    :return: NotificationContent with title, message and data
    """

    notifications = {
        NotificationCategoryType.BUY_BUNDLE: NotificationContent(
            title="New Bundle Purchase",
            message=f"Your {kwargs.get('bundle_name', 'Bunlde')} data plan is here!",
            data={
                "category": str(NotificationCategoryType.BUY_BUNDLE.value),
                "message": "Bundle purchase successful",
                "iccid": kwargs.get('iccid', '0'),
            },
        ),

        NotificationCategoryType.BUY_TOP_UP: NotificationContent(
            title="Top Up Successful",
            message=f"Your {kwargs.get('bundle_name', 'Bundle')} data plan is here!",
            data={
                "category": str(NotificationCategoryType.BUY_TOP_UP.value),
                "message": "Top up successful",
                "iccid": kwargs.get('iccid', '0'),
            }
        ),

        NotificationCategoryType.REWARD_AVAILABLE: NotificationContent(
            title="New Reward Available",
            message="You have a new reward waiting for you!",
            data={
                "category": str(NotificationCategoryType.REWARD_AVAILABLE.value),
                "message": "New reward available",
            }
        ),

        NotificationCategoryType.CASHBACK_REWARD: NotificationContent(
            title="Cashback Reward",
            message=f"You received a cashback of {kwargs.get('cashback_percent', '0')}",
            data={
                "category": str(NotificationCategoryType.CASHBACK_REWARD.value),
                "message": "Cashback reward received",
                "cashback_percent": kwargs.get('cashback_percent', '0'),
            },
            isSilent=False
        ),

        NotificationCategoryType.CONSUMPTION_80_BUNDLE_DETAIL: NotificationContent(
            title=f"Bundle {kwargs.get('bundle_name', 'Bundle')} Usage Update",
            message=f"Dear {kwargs.get('user_name', '0')}, you have reached a significant level of consumption, please check available top-ups",

            data={
                "category": str(NotificationCategoryType.CONSUMPTION_80_BUNDLE_DETAIL.value),
                "message": "Bundle usage update",
                "iccid": kwargs.get('iccid', '0'),
            }
        ),

        NotificationCategoryType.CONSUMPTION_100_BUNDLE_DETAIL: NotificationContent(
            title=f"Bundle {kwargs.get('bundle_name', 'Bundle')} Expired",
            message=f"Dear {kwargs.get('user_name', '0')}, Your {kwargs.get('bundle_name', 'Bundle')} plan has expired. You have reached your consumption limit. You can check available top ups to activate your plan.",
            data={
                "category": str(NotificationCategoryType.CONSUMPTION_80_BUNDLE_DETAIL.value),
                # keep CONSUMPTION_80_BUNDLE_DETAIL to be handled by mobile
                "message": "Bundle usage update",
                "iccid": kwargs.get('iccid', '0'),
            }
        ),

        NotificationCategoryType.PLAN_STARTED: NotificationContent(
            title="Plan Activated",
            message=f"Your {kwargs.get('bundle_name', 'Bundle')} plan is now active and valid until {kwargs.get('validity_date', '')}.",
            data={
                "category": str(NotificationCategoryType.PLAN_STARTED.value),
                "message": "Plan activated",
            },
        ),

        NotificationCategoryType.SHARE_BUNDLE_NOTIFICATION: NotificationContent(
            title="Bundle Shared",
            message=f"{kwargs.get('shared_by', '0')} has been shared with you",
            data={
                "category": str(NotificationCategoryType.SHARE_BUNDLE_NOTIFICATION.value),
                "message": "Bundle shared",
            }
        ),
        NotificationCategoryType.WALLET_TOP_UP_SUCCEEDED: NotificationContent(
            title="Wallet Top-Up Succeeded",
            message=f"Your wallet was topped up successfully with {kwargs.get('amount', '')}!",
            data={
                "category": str(NotificationCategoryType.WALLET_TOP_UP_SUCCEEDED.value),
                "message": "Wallet Top Up Successful",
            }
        ),
        NotificationCategoryType.WALLET_TOP_UP_FAILED: NotificationContent(
            title="Wallet Top-Up Failed",
            message=f"Your wallet top-up failed!",
            data={
                "category": str(NotificationCategoryType.WALLET_TOP_UP_SUCCEEDED.value),
                "message": "Wallet Top Up Successful",
            }
        )
    }

    return notifications.get(category)


# Example usage:
def send_buy_bundle_notification(bundle_name: str, iccid: str) -> NotificationContent:
    return get_notification_content(
        category=NotificationCategoryType.BUY_BUNDLE,
        iccid=iccid,
        bundle_name=bundle_name,
    )


def send_buy_topup_notification(bundle_name: str, iccid: str) -> NotificationContent:
    return get_notification_content(
        category=NotificationCategoryType.BUY_TOP_UP,
        bundle_name=bundle_name,
        iccid=iccid,
    )


def send_reward_available_notification(cashback_percent: str) -> NotificationContent:
    return get_notification_content(
        category=NotificationCategoryType.REWARD_AVAILABLE,
    )


def send_cashback_reward_notification(cashback_percent: str) -> NotificationContent:
    return get_notification_content(
        category=NotificationCategoryType.CASHBACK_REWARD,
        cashback_percent=cashback_percent
    )


def send_consumption_80_bundle_notification(user_name: str, bundle_name: str, iccid: str) -> NotificationContent:
    return get_notification_content(
        user_name=user_name,
        bundle_name=bundle_name,
        category=NotificationCategoryType.CONSUMPTION_80_BUNDLE_DETAIL,
        iccid=iccid,
    )


def send_consumption_100_bundle_notification(user_name: str, bundle_name: str, iccid: str) -> NotificationContent:
    return get_notification_content(
        user_name=user_name,
        bundle_name=bundle_name,
        category=NotificationCategoryType.CONSUMPTION_80_BUNDLE_DETAIL,
        iccid=iccid,
    )


def send_plan_started_notification(bundle_name: str, validity_date: str) -> NotificationContent:
    return get_notification_content(
        category=NotificationCategoryType.PLAN_STARTED,
        bundle_name=bundle_name,
        validity_date=validity_date
    )


def send_share_bundle_notification(shared_by: str) -> NotificationContent:
    return get_notification_content(
        category=NotificationCategoryType.SHARE_BUNDLE_NOTIFICATION,
        shared_by=shared_by
    )


def send_wallet_top_up_succeeded_notification(amount: str) -> NotificationContent:
    return get_notification_content(
        category=NotificationCategoryType.WALLET_TOP_UP_SUCCEEDED,
        amount=amount
    )


def send_wallet_top_up_failed_notification() -> NotificationContent:
    return get_notification_content(
        category=NotificationCategoryType.WALLET_TOP_UP_FAILED
    )
