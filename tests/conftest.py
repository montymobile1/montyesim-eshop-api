import os
os.environ["SUPABASE_URL"] = "https://dummy.supabase.co"
os.environ["SUPABASE_KEY"] = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy.dummy"
os.environ["STRIPE_PUBLIC_KEY"] = "dummy"
os.environ["STRIPE_WEBHOOK_SECRET"] = "dummy"
os.environ["STRIPE_SECRET_KEY"] = "dummy"
os.environ["OTP_HMAC_SECRET"] = "test-otp-hmac-secret"
os.environ.setdefault("ESIM_HUB_API_KEY", "test-esim-hub-api-key")
# Store-review account as it was hardcoded before it moved to the environment (existing tests use these).
os.environ["REVIEW_ACCOUNT_EMAIL"] = "test.apple@example.com"
os.environ["REVIEW_ACCOUNT_PIN"] = "123123"
os.environ["REVIEW_ACCOUNT_PASSWORD"] = "esim_oss@2025"

import pytest
from unittest.mock import MagicMock, patch

@pytest.fixture(autouse=True, scope="session")
def patch_fcm_service_send_notification():
    with patch("app.config.push_notification_manager.fcm_service.send_notification_to_user_from_template", MagicMock(return_value=["mocked_id"])):
        yield
