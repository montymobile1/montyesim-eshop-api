import base64
import json
import os
from typing import List, Dict, Optional, Union

import firebase_admin
from firebase_admin import messaging, credentials
from firebase_admin.exceptions import InvalidArgumentError
from loguru import logger

from app.config.notification_types import NotificationContent
from app.models.notification import NotificationModel
from app.repo.device_repo import DeviceRepo
from app.repo.notification_repo import NotificationRepo


def initialize_firebase():
    """
    Initialize Firebase Admin SDK with credentials
    """
    try:
        # Get credentials from environment
        fcm_base_64 = os.getenv("FCM_BASE_64")
        if fcm_base_64:
            config_json = base64.b64decode(fcm_base_64)
            path = "esim-app.json"
            with open(path, "w") as f:
                f.write(config_json.decode())

        fcm_config = os.getenv("FCM_CONFIG_FILE", "esim-app.json")

        # Check if any app is already initialized
        if not firebase_admin._apps:
            # Initialize the default app without a name
            cred = credentials.Certificate(fcm_config)
            firebase_admin.initialize_app(credential=cred)
            logger.info("Firebase default app initialized successfully.")
        else:
            logger.info("Firebase already initialized.")
    except Exception as e:
        logger.error(f"Failed to initialize Firebase: {e}")
        raise


class FCMService:
    """
    A service class for handling Firebase Cloud Messaging (FCM) notifications.
    Includes advanced features like batch messaging, topic management, and message scheduling.
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FCMService, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not hasattr(self, '_initialized') or not self._initialized:
            # Initialize Firebase when service is instantiated
            initialize_firebase()
            self._initialized = True
            self.__notification_repo = NotificationRepo()
            self.__device_repo = DeviceRepo()

    def get_user_fcm_tokens(self, user_id: str) -> List[str]:
        """
        Retrieves FCM tokens from the logged-in user devices.
        :param user_id: User ID.
        :return: List of FCM tokens.
        """
        devices = self.__device_repo.list(where={"user_id": user_id, "is_logged_in": True})
        return [device.fcm_token for device in devices if device.fcm_token is not None]

    def get_device_fcm_token(self, device_id: str) -> List[str]:
        """
        Retrieves FCM tokens from the logged-in user devices.
        :param device_id: Device Id
        :return: List of FCM tokens.
        """
        devices = self.__device_repo.list(where={"device_id": device_id})
        return [device.fcm_token for device in devices if device.fcm_token is not None]

    def get_device_user_id(self, device_id: str) -> str:
        devices = self.__device_repo.list(where={"device_id": device_id, "is_logged_in": True})
        if len(devices) > 0:
            return devices[0].user_id
        return ""

    def send_notification_to_user_from_template(self, content_template: NotificationContent, user_id: str) -> List[str]:
        """Send notification using a registered template."""
        notification = content_template

        if not notification:
            raise ValueError(f"Template {notification} not found")

        if notification.isSilent:
            return self.send_data_message_to_user(user_id, notification.data)
        notification_data = NotificationModel.model_validate({
            "user_id": user_id,
            "title": notification.title,
            "content": notification.message,
            "status": False,
            "data": json.dumps(notification.data),
            "image_url": ""
        }).model_dump(exclude={"id", "created_at", "updated_at"})
        self.__notification_repo.create(notification_data)

        return self.send_notification_to_user(user_id, notification.title, notification.message, None,
                                              notification.data)

    def send_notification_to_device_from_template(self, content_template: NotificationContent, device_id: str) -> List[
        str]:
        """Send notification using a registered template."""
        notification = content_template

        if not notification:
            raise ValueError(f"Template {notification} not found")

        if notification.isSilent:
            return self.send_data_message_to_device(device_id, notification.data)

        notification_data = NotificationModel.model_validate({
            "user_id": self.get_device_user_id(device_id),
            "title": notification.title,
            "content": notification.message,
            "status": False,
            "data": json.dumps(notification.data),
            "image_url": ""
        }).model_dump(exclude={"id", "created_at", "updated_at"})
        self.__notification_repo.create(notification_data)

        return self.send_notification_to_device(device_id, notification.title, notification.message, None,
                                                notification.data)

    def send_notification_to_user(self, user_id: str, title: str, body: str,
                                  image: Optional[str] = None,
                                  data: Optional[Dict[str, str]] = None) -> List[str] | None:
        """
        Sends a rich push notification to all logged-in devices of a user.
        :param user_id: User ID.
        :param title: Notification title.
        :param body: Notification body.
        :param image: Optional URL for notification image.
        :param data: Optional data payload to include with notification.
        :return: List of responses.
        """
        tokens = self.get_user_fcm_tokens(user_id)
        if not tokens:
            logger.warning(f"No FCM tokens found for user_id: {user_id}")
            return []

        try:
            self.send_multicast_notification(tokens, title, body, image, data)
        except Exception as e:
            logger.error(f"Error sending notifications: {e}")
            return [str(e)]

    def send_notification_to_device(self, device_id: str, title: str, body: str,
                                    image: Optional[str] = None,
                                    data: Optional[Dict[str, str]] = None) -> List[str] | None:
        """
        Sends a rich push notification to all logged-in devices of a user.
        :param device_id: Device ID.
        :param title: Notification title.
        :param body: Notification body.
        :param image: Optional URL for notification image.
        :param data: Optional data payload to include with notification.
        :return: List of responses.
        """
        tokens = self.get_device_fcm_token(device_id)
        if not tokens:
            logger.warning(f"No FCM tokens found for device_id: {device_id}")
            return []

        try:
            self.send_multicast_notification(tokens, title, body, image, data)
        except Exception as e:
            logger.error(f"Error sending notifications: {e}")
            return [str(e)]

    def send_data_message_to_user(self, user_id: str, data: dict) -> List[str] | None:
        """
        Sends a data message (silent notification) to all logged-in devices of a user.
        :param user_id: User ID.
        :param data: Dictionary containing the data payload.
        :return: List of responses.
        """
        tokens = self.get_user_fcm_tokens(user_id)
        if not tokens:
            logger.warning(f"No FCM tokens found for user_id: {user_id}")
            return []

        try:
            self.send_multicast_notification(tokens, "", "", None, data, True)
        except Exception as e:
            logger.error(f"Error sending data messages: {e}")
            return [str(e)]

    def send_data_message_to_device(self, device_id: str, data: dict) -> List[str] | None:
        """
        Sends a data message (silent notification) to all logged-in devices of a user.
        :param device_id: User ID.
        :param data: Dictionary containing the data payload.
        :return: List of responses.
        """
        tokens = self.get_device_fcm_token(device_id)
        if not tokens:
            logger.warning(f"No FCM tokens found for user_id: {device_id}")
            return []

        try:
            self.send_multicast_notification(tokens, "", "", None, data, True)
        except Exception as e:
            logger.error(f"Error sending data messages: {e}")
            return [str(e)]

    def send_multicast_notification(self, tokens: List[str], title: str, body: str,
                                    image: Optional[str] = None,
                                    data: Optional[Dict[str, str]] = None,
                                    isSilent: bool = False) -> messaging.BatchResponse | None:
        """
        Sends a notification to multiple devices efficiently using FCM's multicast messaging.
        :param tokens: List of FCM tokens.
        :param title: Notification title.
        :param body: Notification body.
        :param image: Optional URL for notification image.
        :param data: Optional data payload.
        :return: BatchResponse containing the results.
        """
        if not tokens:
            logger.warning("No tokens provided for multicast notification")
            return None

        try:
            notification = messaging.Notification(
                title=title,
                body=body,
                image=image
            )
            print(tokens)

            message = messaging.MulticastMessage(
                notification=notification,
                data=data or {},
                tokens=tokens,
            )
            if isSilent:
                message.notification = None

            batch_response = messaging.send_each_for_multicast(message)
            logger.info(f"Multicast sent. Success: {batch_response.success_count}/{len(tokens)}")
            return batch_response
        except Exception as e:
            logger.error(f"Error sending multicast: {e}")
            raise

    def send_topic_notification(self, topic: str, title: str, body: str,
                                image: Optional[str] = None,
                                data: Optional[Dict[str, str]] = None) -> str:
        """
        Sends a notification to all users subscribed to a specific topic.
        :param topic: Topic name.
        :param title: Notification title.
        :param body: Notification body.
        :param image: Optional URL for notification image.
        :param data: Optional data payload.
        :return: Message ID if successful.
        """
        try:
            notification = messaging.Notification(
                title=title,
                body=body,
                image=image
            )

            message = messaging.Message(
                notification=notification,
                data=data or {},
                topic=topic,
            )

            response = messaging.send(message)
            logger.info(f"Topic notification sent successfully: {response}")
            return response
        except Exception as e:
            logger.error(f"Error sending topic notification: {e}")
            raise

    def subscribe_to_topic(self, tokens: Union[str, List[str]], topic: str) -> messaging.TopicManagementResponse:
        """
        Subscribes one or more devices to a topic.
        :param tokens: Single token or list of FCM tokens.
        :param topic: Topic name to subscribe to.
        :return: TopicManagementResponse containing results.
        """
        try:
            tokens_list = [tokens] if isinstance(tokens, str) else tokens
            response = messaging.subscribe_to_topic(tokens_list, topic)
            logger.info(f"Topic subscription successful. Success: {response.success_count}/{len(tokens_list)}")
            return response
        except Exception as e:
            logger.error(f"Error subscribing to topic: {e}")
            raise

    def unsubscribe_from_topic(self, tokens: Union[str, List[str]], topic: str) -> messaging.TopicManagementResponse:
        """
        Unsubscribes one or more devices from a topic.
        :param tokens: Single token or list of FCM tokens.
        :param topic: Topic name to unsubscribe from.
        :return: TopicManagementResponse containing results.
        """
        try:
            tokens_list = [tokens] if isinstance(tokens, str) else tokens
            response = messaging.unsubscribe_from_topic(tokens_list, topic)
            logger.info(f"Topic unsubscription successful. Success: {response.success_count}/{len(tokens_list)}")
            return response
        except Exception as e:
            logger.error(f"Error unsubscribing from topic: {e}")
            raise

    def validate_token(self, token: str) -> bool:
        """
        Validates if an FCM token is still valid by attempting to send a dummy message.
        :param token: FCM token to validate.
        :return: Boolean indicating if token is valid.
        """
        try:
            # Create a minimal message just to test token validity
            message = messaging.Message(
                data={'validate': 'true'},
                token=token
            )
            messaging.send(message)
            return True
        except InvalidArgumentError as e:
            logger.warning(f"Invalid token format: {token}")
            return False
        except messaging.UnregisteredError as e:
            logger.warning(f"Token no longer valid: {token}")
            return False
        except Exception as e:
            logger.error(f"Error validating token: {e}")
            return False


fcm_service: FCMService = FCMService()
initialize_firebase()
