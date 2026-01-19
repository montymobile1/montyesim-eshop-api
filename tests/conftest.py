"""
Corrected conftest.py with proper Firebase and FCM mocking.

The key issue: @patch decorators don't work directly on pytest fixtures.
Instead, we need to use context managers or stack the patches properly.
"""

import os
import base64
from unittest.mock import MagicMock, patch

import pytest
import firebase_admin


# ============================================================================
# ENVIRONMENT SETUP (Must be BEFORE imports)
# ============================================================================

os.environ["SUPABASE_URL"] = "https://dummy.supabase.co"
os.environ["SUPABASE_KEY"] = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy.dummy"
os.environ["STRIPE_PUBLIC_KEY"] = "dummy"
os.environ["STRIPE_WEBHOOK_SECRET"] = "dummy"
os.environ["STRIPE_SECRET_KEY"] = "dummy"


# ============================================================================
# CORRECTED SESSION-SCOPED FIREBASE + FCM MOCKING
# ============================================================================

@pytest.fixture(autouse=True, scope="session")
def mock_firebase_and_fcm():
    """
    Mock Firebase initialization AND FCM service for entire test session.

    This fixture:
    1. Mocks firebase_admin.credentials.Certificate (prevents deserialization)
    2. Mocks firebase_admin.initialize_app (prevents actual initialization)
    3. Mocks builtins.open (prevents file operations)
    4. Mocks FCM service send_notification
    5. Mocks initialize_firebase method

    All mocks persist for the entire test session for efficiency.
    """
    # Create a context manager stack for all patches
    with patch("firebase_admin.credentials.Certificate") as mock_cert, \
         patch("firebase_admin.initialize_app") as mock_init_app, \
         patch("builtins.open", create=True) as mock_file_open, \
         patch("app.config.push_notification_manager.initialize_firebase") as mock_fcm_init, \
         patch("app.config.push_notification_manager.fcm_service.send_notification_to_user_from_template",
               MagicMock(return_value=["mocked_id"])) as mock_send_notification:

        # Configure the Certificate mock to return a proper mock credential
        mock_credential = MagicMock()
        mock_credential.project_id = "test-project-id"
        mock_cert.return_value = mock_credential

        # Configure initialize_app to return a mock app
        mock_app = MagicMock()
        mock_app.name = "[DEFAULT]"
        mock_init_app.return_value = mock_app

        # Configure file open mock
        mock_file = MagicMock()
        mock_file.__enter__ = MagicMock(return_value=mock_file)
        mock_file.__exit__ = MagicMock(return_value=False)
        mock_file.write = MagicMock()
        mock_file_open.return_value = mock_file

        # Make mocks available to tests if needed
        pytest.mock_firebase_cert = mock_cert
        pytest.mock_firebase_init_app = mock_init_app
        pytest.mock_fcm_initialize = mock_fcm_init
        pytest.mock_fcm_send = mock_send_notification

        yield {
            "certificate": mock_cert,
            "initialize_app": mock_init_app,
            "file_open": mock_file_open,
            "fcm_initialize": mock_fcm_init,
            "fcm_send_notification": mock_send_notification
        }


# ============================================================================
# FUNCTION-SCOPED FIREBASE CLEANUP
# ============================================================================

@pytest.fixture(autouse=True)
def reset_firebase():
    """
    Reset Firebase apps before and after each test.

    Ensures test isolation even with session-scoped mocks.
    The session mocks prevent real operations, while this ensures
    each test starts with a clean Firebase state.
    """
    # Clear before test
    if hasattr(firebase_admin, '_apps') and firebase_admin._apps:
        firebase_admin._apps.clear()

    yield

    # Clear after test
    if hasattr(firebase_admin, '_apps') and firebase_admin._apps:
        firebase_admin._apps.clear()


# ============================================================================
# ALTERNATIVE: SEPARATE FIXTURES (If you prefer modularity)
# ============================================================================

@pytest.fixture(scope="session")
def mock_firebase_certificate():
    """Mock Firebase certificate for session."""
    with patch("firebase_admin.credentials.Certificate") as mock_cert:
        mock_credential = MagicMock()
        mock_credential.project_id = "test-project-id"
        mock_cert.return_value = mock_credential
        yield mock_cert


@pytest.fixture(scope="session")
def mock_firebase_initialize_app():
    """Mock Firebase initialize_app for session."""
    with patch("firebase_admin.initialize_app") as mock_init:
        mock_app = MagicMock()
        mock_app.name = "[DEFAULT]"
        mock_init.return_value = mock_app
        yield mock_init


@pytest.fixture(scope="session")
def mock_fcm_service():
    """Mock FCM service operations for session."""
    with patch("app.config.push_notification_manager.FCMService.initialize_firebase") as mock_fcm_init, \
         patch("app.config.push_notification_manager.fcm_service.send_notification_to_user_from_template",
               MagicMock(return_value=["mocked_id"])) as mock_send:
        yield {
            "initialize": mock_fcm_init,
            "send_notification": mock_send
        }


# ============================================================================
# HELPER FIXTURES FOR TESTS
# ============================================================================

@pytest.fixture
def firebase_credentials_base64():
    """Provide base64-encoded mock credentials for testing."""
    credentials = {
        "type": "service_account",
        "project_id": "test-project-id",
        "private_key_id": "test-key-id",
        "private_key": "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        "client_email": "test@test-project.iam.gserviceaccount.com",
        "client_id": "123456789",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    import json
    credentials_json = json.dumps(credentials)
    return base64.b64encode(credentials_json.encode()).decode()


@pytest.fixture
def firebase_config_file(tmp_path, firebase_credentials_base64):
    """Create temporary Firebase config file."""
    config_file = tmp_path / "test-firebase-config.json"
    decoded = base64.b64decode(firebase_credentials_base64).decode()
    config_file.write_text(decoded)
    return str(config_file)