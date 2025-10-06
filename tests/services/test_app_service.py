import json
import os
import unittest
from typing import Literal, List
from unittest.mock import patch, AsyncMock

from httpx import Headers
from starlette.requests import Request
from starlette.types import Scope

from app.exceptions import CustomException
from app.models.user import UserModel
from app.schemas.app import PageContentResponse, DeviceRequest, DeleteDeviceRequest, ContactUsRequest
from app.schemas.esim_hub import ContentResponse
from app.schemas.response import Response
from app.services.app_service import AppService


def create_mock_request() -> Request:
    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": Headers({"X-Forwarded-For": "192.168.1.1"}).raw,
        "query_string": b"",
        "client": ("192.168.1.1", 5000),  # Fixed: Use IP address instead of "testclient"
        "server": ("testserver", 80),
        "scheme": "http",
        "http_version": "1.1",
    }
    return Request(scope)


async def get_content_tag_mock(tag: Literal["TERM_CONDITION", "ABOUT_US", "ADS", "FAQ", "PRIVACY_POLICY"],
                               accept_language: str):
    root = os.path.abspath(os.curdir)
    if tag == "FAQ":
        with open(f"{root}/tests/services/mock/content_tag_faq_response.json") as file:
            data = json.load(file)
            return [ContentResponse.model_validate(item) for item in data["data"]["items"]]
    elif tag == "ABOUT_US":
        with open(f"{root}/tests/services/mock/content_tag_about_us_response.json") as file:
            data = json.load(file)
            return ContentResponse.model_validate(data["data"]["item"])
    elif tag == "TERM_CONDITION":
        with open(f"{root}/tests/services/mock/content_tag_terms_response.json") as file:
            data = json.load(file)
            return ContentResponse.model_validate(data["data"]["item"])
    elif tag == "PRIVACY_POLICY":
        with open(f"{root}/tests/services/mock/content_tag_terms_response.json") as file:
            data = json.load(file)
            return ContentResponse.model_validate(data["data"]["item"])


async def get_content_tags_mock(tag: Literal["TERM_CONDITION", "ABOUT_US", "ADS", "FAQ", "PRIVACY_POLICY"],
                                lang_code: str):
    root = os.path.abspath(os.curdir)
    if tag == "FAQ":
        with open(f"{root}/tests/services/mock/content_tag_faq_response.json") as file:
            data = json.load(file)
            return [ContentResponse.model_validate(item) for item in data["data"]["items"]]
    elif tag == "ABOUT_US":
        with open(f"{root}/tests/services/mock/content_tag_about_us_response.json") as file:
            data = json.load(file)
            return ContentResponse.model_validate(data["data"]["item"])
    elif tag == "TERM_CONDITION":
        with open(f"{root}/tests/services/mock/content_tag_terms_response.json") as file:
            data = json.load(file)
            return ContentResponse.model_validate(data["data"]["item"])


class TestAppService(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["DEFAULT_CURRENCY"] = "EUR"
        os.environ["SUPABASE_URL"] = "URL"
        os.environ["SUPABASE_KEY"] = "KEY"
        os.environ["STRIPE_PUBLIC_KEY"] = "PK"
        os.environ["STRIPE_WEBHOOK_SECRET"] = "WEBHOOK_SECRET"
        os.environ["STRIPE_SECRET_KEY"] = "SECRET_KEY"

    @patch('app.services.app_service.BannerRepo')
    @patch('app.services.app_service.ConfigRepo')
    @patch('app.services.app_service.esim_hub_service_instance')
    @patch('app.services.app_service.ContactUsRepo')
    @patch('app.services.app_service.DeviceRepo')
    def setUp(self, mock_device_repo, mock_contact_us_repo, mock_esim_hub_service, mock_config_repo, mock_banner_repo):
        self.mock_esim_hub_service = mock_esim_hub_service.return_value
        self.mock_device_repo = mock_device_repo.return_value
        self.mock_contact_us_repo = mock_contact_us_repo.return_value
        self.mock_config_repo = mock_config_repo.return_value
        self.mock_banner_repo = mock_banner_repo.return_value

        self.mock_device_repo.upsert.return_value = {"data": "success"}
        self.mock_device_repo.update_by.return_value = [{"data": "success"}]
        self.mock_device_repo.get_first_by.return_value = None

        self.mock_esim_hub_service.get_content_tag = AsyncMock(side_effect=get_content_tag_mock)
        self.mock_esim_hub_service.get_content_tags = AsyncMock(side_effect=get_content_tags_mock)

        self.app_service = AppService()
        self.app_service._AppService__esim_hub_service = self.mock_esim_hub_service
        self.app_service._AppService__contact_us_repo = self.mock_contact_us_repo
        self.app_service._AppService__device_repo = self.mock_device_repo
        self.app_service._AppService__config_repo = self.mock_config_repo
        self.app_service._AppService__banner_repo = self.mock_banner_repo

    async def test_about_us(self):
        response = await self.app_service.about_us(accepted_language="en")
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)
        self.assertIsInstance(response.data, PageContentResponse)

    async def test_terms_and_conditions(self):
        response = await self.app_service.terms_and_conditions(accepted_language="en")
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)
        self.assertIsInstance(response.data, PageContentResponse)

    async def test_faq(self):
        response = await self.app_service.faq(accepted_language="en")
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)
        self.assertIsInstance(response.data, List)

    async def test_add_device_no_user(self):
        http_request = create_mock_request()
        request = DeviceRequest(device_model="android", fcm_token="", os="", os_version="", app_version="as",
                                ram_size="as", screen_resolution="a", is_rooted=False, manufacturer="")
        response = await self.app_service.add_device(user=None, device_id="123", device_request=request,
                                                     request=http_request)
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)

    async def test_add_device_with_user(self):
        http_request = create_mock_request()
        request = DeviceRequest(device_model="android", fcm_token="", os="", os_version="", app_version="as",
                                ram_size="as", screen_resolution="a", is_rooted=False, manufacturer="")
        user = UserModel(id="123", email="<EMAIL>", token="token", msisdn="", is_verified=True)
        response = await self.app_service.add_device(user=user, device_id="123", device_request=request,
                                                     request=http_request)
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)

    async def test_delete_device(self):
        delete_request = DeleteDeviceRequest(email="test@example.com")
        response = await self.app_service.delete_device(delete_device_request=delete_request)
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)

    @patch('app.services.app_service.send_email')  # Mock send_email function
    async def test_contact_us(self, mock_send_email):
        contact_request = ContactUsRequest(email="test@example.com", content="Test message")
        self.mock_contact_us_repo.create.return_value = {"id": 1, "email": "test@example.com"}

        response = await self.app_service.contact_us(contact_us_request=contact_request)

        expected_content = (
            "<h1>Received New Email Message</h1>\n"
            "<p><b>From</b>: test@example.com</p>\n"
            "<p><b>Content</b>: Test message</p>\n"
        )
        mock_send_email.assert_called_once_with(
            subject="New Email Received",
            html_content=expected_content,
            recipients='support@example.com'
        )

        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)

    @patch('app.services.app_service.send_email')  # Mock send_email function
    async def test_contact_us_failure(self, mock_send_email):
        contact_request = ContactUsRequest(email="test@example.com", content="Test message")
        self.mock_contact_us_repo.create.return_value = None
        expected_content = (
            "<h1>Received New Email Message</h1>\n"
            "<p><b>From</b>: test@example.com</p>\n"
            "<p><b>Content</b>: Test message</p>\n"
        )
        with self.assertRaises(CustomException):
            await self.app_service.contact_us(contact_us_request=contact_request)
        mock_send_email.assert_not_called()

    @patch('app.services.app_service.send_email')  # Mock send_email function
    @patch('os.getenv', return_value='support@example.com')
    async def test_contact_us(self, mock_getenv, mock_send_email):
        contact_request = ContactUsRequest(email="test@example.com", content="Test message")
        self.mock_contact_us_repo.create.return_value = {"id": 1, "email": "test@example.com"}

        response = await self.app_service.contact_us(contact_us_request=contact_request)

        # Normalize whitespace for comparison
        actual_args = mock_send_email.call_args.kwargs
        actual_html = actual_args.get('html_content', '').replace(' ', '').replace('\n', '')
        expected_html = ("<h1>Received New Email Message</h1>"
                         "<p><b>From</b>:test@example.com</p>"
                         "<p><b>Content</b>:Test message</p>").replace(' ', '').replace('\n', '')
        assert actual_html == expected_html
        assert actual_args.get('subject') == "New Email Received"
        assert actual_args.get('recipients') == 'support@example.com'

        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)

    @patch('app.services.app_service.send_email')  # Mock send_email function that raises exception
    async def test_contact_us_email_failure(self, mock_send_email):
        mock_send_email.side_effect = Exception("Email service unavailable")
        contact_request = ContactUsRequest(email="test@example.com", content="Test message")
        self.mock_contact_us_repo.create.return_value = {"id": 1, "email": "test@example.com"}

        # Should still succeed even if email fails
        response = await self.app_service.contact_us(contact_us_request=contact_request)

        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)
        mock_send_email.assert_called_once()

    async def test_privacy_policy(self):
        response = await self.app_service.privacy_policy(accepted_language="en")
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)
        self.assertIsInstance(response.data, PageContentResponse)

    async def test_user_guide(self):
        response = await self.app_service.user_guide()
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)

    @patch('app.services.app_service.os.getenv')
    async def test_configurations(self, mock_getenv):
        mock_getenv.side_effect = lambda key, default=None: {
            "WHATSAPP_NUMBER": "123456789",
            "SUPABASE_URL": "https://test.supabase.co",
            "SUPABASE_ANON_KEY": "test_anon_key",
            "DEFAULT_CURRENCY": "USD"
        }.get(key, default)

        self.mock_config_repo.list.return_value = [
            type('Config', (), {'key': 'test_key', 'value': 'test_value'})()
        ]
        self.mock_config_repo.get_first_by.return_value = type('Config', (), {'value': 'v1.0'})()

        response = await self.app_service.configurations()
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)
        self.assertIsInstance(response.data, List)

    def test_banners(self):
        mock_banner = type('Banner', (), {
            'id': 1,
            'title': 'Test Banner',
            'image_url': 'https://test.com/banner.jpg',
            'platform': 'web',
            'description': 'Banner description',
            'image': 'https://test.com/banner.jpg',
            'action': 'view'
        })()
        mock_banner.model_dump = lambda: {
            'id': 1,
            'title': 'Test Banner',
            'image_url': 'https://test.com/banner.jpg',
            'description': 'Banner description',
            'image': 'https://test.com/banner.jpg',
            'action': 'view'
        }
        self.mock_banner_repo.list.return_value = [mock_banner]

        response = self.app_service.banners(x_currency="USD", locale="en", x_platform="web")
        self.assertIsInstance(response, Response)
        self.assertEqual(response.status, "success")
        self.assertEqual(response.responseCode, 200)
        self.assertIsInstance(response.data, List)
