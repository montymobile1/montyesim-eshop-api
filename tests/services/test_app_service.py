import json
import os
import unittest
from typing import Literal, List
from unittest.mock import patch, AsyncMock

from httpx import Headers
from starlette.requests import Request
from starlette.types import Scope

from app.models.user import UserModel
from app.schemas.app import PageContentResponse, DeviceRequest
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
        "client": ("testclient", 5000),
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

    @patch('app.services.app_service.esim_hub_service_instance')
    @patch('app.services.app_service.ContactUsRepo')
    @patch('app.services.app_service.DeviceRepo')
    def setUp(self, mock_esim_hub_service, mock_device_repo, mock_contact_us_repo):
        self.mock_esim_hub_service = mock_esim_hub_service.return_value
        self.mock_device_repo = mock_device_repo.return_value
        self.mock_contact_us_repo = mock_contact_us_repo.return_value

        self.mock_device_repo.upsert.return_value = {"data": "success"}
        self.mock_device_repo.update_by.return_value = [{"data": "success"}]

        self.mock_esim_hub_service.get_content_tag = AsyncMock(side_effect=get_content_tag_mock)
        self.mock_esim_hub_service.get_content_tags = AsyncMock(side_effect=get_content_tags_mock)

        self.app_service = AppService()
        self.app_service._AppService__esim_hub_service = self.mock_esim_hub_service
        self.app_service._AppService__contact_us_repo = self.mock_esim_hub_service
        self.app_service._AppService___device_repo = self.mock_esim_hub_service

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
