"""
ROBUST SOLUTION: Patch Firebase BEFORE any app imports.

This conftest sets up mocks at module load time, before pytest even starts
collecting tests. This ensures no real Firebase initialization can happen.
"""

import os
import sys
import base64
from unittest.mock import MagicMock, patch, mock_open

# ============================================================================
# CRITICAL: Environment setup FIRST
# ============================================================================

os.environ["SUPABASE_URL"] = "https://dummy.supabase.co"
os.environ["SUPABASE_KEY"] = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.dummy.dummy"
os.environ["STRIPE_PUBLIC_KEY"] = "dummy"
os.environ["STRIPE_WEBHOOK_SECRET"] = "dummy"
os.environ["STRIPE_SECRET_KEY"] = "dummy"

# Set this to prevent Firebase from trying to load a real file during imports
os.environ["FCM_CONFIG_FILE"] = "/tmp/test-firebase-config.json"


# ============================================================================
# CRITICAL: Start patching IMMEDIATELY (before pytest imports)
# ============================================================================

# These patchers start IMMEDIATELY when conftest is loaded
_firebase_cert_patcher = patch("firebase_admin.credentials.Certificate")
_firebase_init_patcher = patch("firebase_admin.initialize_app")
_file_open_patcher = patch("builtins.open", mock_open())

# Start all patchers NOW (before any test collection)
_mock_cert = _firebase_cert_patcher.start()
_mock_init_app = _firebase_init_patcher.start()
_mock_file_open = _file_open_patcher.start()

# Configure the mocks with proper return values
_mock_credential_obj = MagicMock()
_mock_credential_obj.project_id = "test-project-id"
_mock_cert.return_value = _mock_credential_obj

_mock_app_obj = MagicMock()
_mock_app_obj.name = "[DEFAULT]"
_mock_init_app.return_value = _mock_app_obj


# Now it's safe to import firebase_admin
import firebase_admin
import pytest


# ============================================================================
# SESSION-SCOPED FIXTURE (for FCM and other mocking)
# ============================================================================

@pytest.fixture(autouse=True, scope="session")
def mock_fcm_service():
    """
    Mock FCM service operations for entire test session.

    Firebase core operations (Certificate, initialize_app) are already mocked
    at module level. This fixture adds FCM-specific mocking.
    """
    # Try to patch FCM service - might fail if module doesn't exist yet
    fcm_patches = []

    try:
        fcm_init_patch = patch("app.config.push_notification_manager.FCMService.initialize_firebase")
        fcm_patches.append(fcm_init_patch)
        mock_fcm_init = fcm_init_patch.start()
    except (ImportError, AttributeError):
        mock_fcm_init = None

    try:
        fcm_send_patch = patch(
            "app.config.push_notification_manager.fcm_service.send_notification_to_user_from_template",
            MagicMock(return_value=["mocked_id"])
        )
        fcm_patches.append(fcm_send_patch)
        mock_fcm_send = fcm_send_patch.start()
    except (ImportError, AttributeError):
        mock_fcm_send = MagicMock(return_value=["mocked_id"])

    yield {
        "fcm_initialize": mock_fcm_init,
        "fcm_send": mock_fcm_send
    }

    # Stop FCM patches
    for patcher in fcm_patches:
        try:
            patcher.stop()
        except:
            pass


# ============================================================================
# FUNCTION-SCOPED CLEANUP
# ============================================================================

@pytest.fixture(autouse=True)
def reset_firebase():
    """
    Reset Firebase apps before and after each test.

    This ensures test isolation.
    """
    # Clear before test
    if hasattr(firebase_admin, '_apps') and firebase_admin._apps:
        firebase_admin._apps.clear()

    yield

    # Clear after test
    if hasattr(firebase_admin, '_apps') and firebase_admin._apps:
        firebase_admin._apps.clear()


# ============================================================================
# PYTEST HOOKS
# ============================================================================

def pytest_configure(config):
    """
    Configure pytest - add custom markers.
    """
    config.addinivalue_line("markers", "firebase: Firebase-related tests")
    config.addinivalue_line("markers", "fcm: FCM push notification tests")


def pytest_sessionfinish(session, exitstatus):
    """
    Clean up at the end of the test session.

    Stop all module-level patches.
    """
    try:
        _firebase_cert_patcher.stop()
        _firebase_init_patcher.stop()
        _file_open_patcher.stop()
    except:
        pass


# ============================================================================
# HELPER FIXTURES
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


@pytest.fixture
def firebase_mocks():
    """
    Provide access to Firebase mocks for verification in tests.

    Usage:
        def test_something(firebase_mocks):
            assert firebase_mocks['cert'].called
    """
    return {
        "cert": _mock_cert,
        "init_app": _mock_init_app,
        "file_open": _mock_file_open
    }