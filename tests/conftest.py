import os
os.environ["SUPABASE_URL"] = "https://dummy.supabase.co"
os.environ["SUPABASE_KEY"] = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy.dummy"
os.environ["STRIPE_PUBLIC_KEY"] = "dummy"
os.environ["STRIPE_WEBHOOK_SECRET"] = "dummy"
os.environ["STRIPE_SECRET_KEY"] = "dummy"
os.environ["OTP_HMAC_SECRET"] = "test-otp-hmac-secret"
os.environ.setdefault("ESIM_HUB_API_KEY", "test-esim-hub-api-key")

import pytest
from unittest.mock import MagicMock, patch

@pytest.fixture(autouse=True, scope="session")
def patch_fcm_service_send_notification():
    with patch("app.config.push_notification_manager.fcm_service.send_notification_to_user_from_template", MagicMock(return_value=["mocked_id"])):
        yield
