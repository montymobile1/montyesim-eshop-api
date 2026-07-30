#!/usr/bin/env python3

import sys
import os
import traceback

# Add project root to path
ROOT_PATH = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT_PATH)

from dotenv import load_dotenv


def load_environment():
    """Load environment variables from .env file"""
    env_file = os.path.join(ROOT_PATH, '.env')
    if os.path.exists(env_file):
        load_dotenv(env_file)
        print(f"✓ Environment variables loaded from {env_file}")
    else:
        print(f"⚠ Warning: .env file not found at {env_file}")
        print("  Attempting to use existing environment variables...")


def test_email_sender():
    """Test the existing email sender function"""

    print("\n" + "="*70)
    print("FastAPI Email Sender Test")
    print("="*70 + "\n")

    # Load environment
    load_environment()

    try:
        # Import the existing email sender
        print("\n► Importing the email sender from app.config.config...")
        from app.config.config import send_email
        print("✓ Successfully imported send_email function")

        # Test recipients
        recipients = "mohammad.tokko@montyholding.com, samah.jamal@montymobile.com"
        subject = "FastAPI Email Sender Test"

        # Simple HTML body
        html_body = """
        <html>
            <body>
                <h2>Email Sender Test</h2>
                <p>This is a test email to confirm that the email sender is working correctly.</p>
                <p>Test timestamp: {timestamp}</p>
                <hr/>
                <p>Best regards,<br/>FastAPI Test Script</p>
            </body>
        </html>
        """.format(timestamp=__import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

        print(f"\n► Preparing to send test email...")
        print(f"  Subject: {subject}")
        print(f"  Recipients: {recipients}")
        print(f"  ")

        # Call the existing email sender
        send_email(
            subject=subject,
            html_content=html_body,
            recipients=recipients,
            attachment=None
        )

        print("\n" + "="*70)
        print("✓ SUCCESS: Email sent successfully!")
        print("="*70)
        print(f"\n  ✓ Email delivered to:")
        print(f"    - mohammad.tokko@montyholding.com")
        print(f"    - samah.jamal@montymobile.com")
        print(f"\n  Subject: {subject}")
        print("="*70 + "\n")

        return True

    except FileNotFoundError as e:
        print("\n" + "="*70)
        print("✗ ERROR: File not found")
        print("="*70)
        print(f"\nError Details:")
        print(f"  {str(e)}\n")
        print("Full Traceback:")
        traceback.print_exc()
        print("="*70 + "\n")
        return False

    except ValueError as e:
        print("\n" + "="*70)
        print("✗ ERROR: Configuration Error")
        print("="*70)
        print(f"\nError Details:")
        print(f"  {str(e)}\n")
        print("Possible causes:")
        print("  - Missing SMTP configuration in .env file")
        print("  - Missing required environment variables:")
        print("    * SMTP_SERVER")
        print("    * SMTP_PORT")
        print("    * SMTP_USERNAME")
        print("    * SMTP_PASSWORD\n")
        print("Full Traceback:")
        traceback.print_exc()
        print("="*70 + "\n")
        return False

    except Exception as e:
        print("\n" + "="*70)
        print("✗ ERROR: Failed to send email")
        print("="*70)
        print(f"\nError Type: {type(e).__name__}")
        print(f"Error Details:")
        print(f"  {str(e)}\n")
        print("Full Traceback:")
        traceback.print_exc()
        print("="*70 + "\n")
        return False


if __name__ == "__main__":
    """Main entry point"""
    success = test_email_sender()
    sys.exit(0 if success else 1)

