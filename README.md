# eSIM Open Source Project

## Overview

This project is a FastAPI-based backend service that integrates **Supabase** for authentication and database management,
**Stripe** for payment processing, and **E-Sim Hub** for eSIM-related APIs. The service provides APIs for user
authentication, payment handling, and eSIM bundle management.

## Features

- **FastAPI** for building high-performance APIs
- **Supabase** for authentication and database
- **Stripe** for payment processing
- **eSIM Hub** eSIM bundles and profiles provider
- **Environment-based configuration**
- **CORS Support** with configurable origins
- **GZip Compression** for optimized response sizes
- **Comprehensive Error Handling** with custom exception handlers
- **Logging** with rotation and level configuration
- **Scheduler Service** for background tasks
- **API Versioning** (v1 and v2 support)
- **Health Check Endpoints** for monitoring
- **Email Templates** for notifications
- **Wallet System** for managing user balances
- **Voucher System** for promotions
- **Bundle Management** for eSIM packages

## Project Structure

```
app/
├── api/
│   ├── v1/
│   │   ├── application.py
│   │   ├── authentication.py
│   │   ├── bundles.py
│   │   ├── callback.py
│   │   ├── health_check.py
│   │   ├── home.py
│   │   ├── promotion.py
│   │   ├── user_bundle.py
│   │   ├── user_wallet.py
│   │   └── voucher.py
│   └── v2/
├── config/
├── dependencies/
├── email_templates/
├── exceptions/
├── models/
├── repo/
├── schemas/
├── services/
└── main.py
```

## Prerequisites

Ensure you have the following:

- Python 3.11+
- pip (Python package manager)
- Virtual environment (optional but recommended)
- Supabase Account
- Stripe Account
- eSIM Hub API Key
- Firebase Account for Firebase Cloud Messaging

## Installation

1. Clone the repository:
   ```sh
   git clone <repository-url>
   cd <project-folder>
   ```
2. Create a virtual environment:
   ```sh
   python -m venv venv
   source venv/bin/activate  # On Windows use: venv\Scripts\activate
   ```
3. Install dependencies:
   ```sh
   pip install -r requirements.txt
   ```

## Configuration

1. Copy the example environment file and update the values:
   ```sh
   cp .env.example .env
   ```
2. Open the `.env` file and fill in the required keys.
3. Load the environment variables before running the application:
   ```sh
   export $(cat .env | xargs)  # On Windows, set variables manually or use dotenv
   ```

## Environment Variables (.env)

The project uses a `.env` file for configuration. Copy `.env.example` to `.env` and fill in the required values. Below is a detailed explanation of each variable and how to obtain the necessary keys:

### Supabase Configuration
- **SUPABASE_URL**: Your Supabase project URL.  
  Find it in your Supabase dashboard under Project Settings > API > Project URL.
- **SUPABASE_KEY**: Service role key for server-side access.  
  In Supabase dashboard, go to Project Settings > API > Service Role Key.
- **SUPABASE_ANON_KEY**: Public (anon) key for client-side access.  
  In Supabase dashboard, go to Project Settings > API > anon public.

### Stripe Configuration
- **STRIPE_SECRET_KEY**: Your Stripe secret key (test or live).  
  In Stripe dashboard, go to Developers > API keys > Secret key.
- **STRIPE_PUBLIC_KEY**: Your Stripe publishable key for frontend use.  
  In Stripe dashboard, go to Developers > API keys > Publishable key.
- **STRIPE_WEBHOOK_SECRET**: The webhook signing secret for Stripe.  
  In Stripe dashboard, go to Developers > Webhooks, select your webhook, and copy the Signing secret.
- **MERCHANT_ID**: Your Stripe merchant ID (optional, for transaction context).
- **MERCHANT_DISPLAY_NAME**: The display name for your merchant in Stripe transactions.

### eSIM Hub Configuration
- **ESIM_HUB_BASE_URL / ESIM_HUB_BASE_URL2**: The base URLs for the eSIM hub APIs.
- **ESIM_HUB_API_KEY**: The API key for accessing the eSIM hub (provided by your eSIM hub provider).
- **ESIM_HUB_TENANT_KEY**: The tenant key for eSIM hub integration (provided by your eSIM hub provider).

### Firebase Cloud Messaging
- **FCM_CONFIG_FILE**: Path to your Firebase service account JSON file.  
  Download this from Firebase Console > Project Settings > Service Accounts > Generate new private key.

### Email / SMTP Configuration
- **SMTP_SERVER, SMTP_PORT, SMTP_USERNAME, SMTP_PASSWORD, SMTP_SENDER, SUPPORT_EMAIL**:  
  These are your email server settings, provided by your email service provider (e.g., Gmail, SendGrid, etc.).

### App Defaults
- **DEFAULT_CURRENCY**: The default currency code (e.g., USD, GBP).
- **ENVIRONMENT**: The environment type (DEV, QA, PROD).
- **PAYMENT_METHODS**: Comma-separated list of enabled payment methods (e.g., otp, card).

#### Example: How to get Stripe and Supabase keys
- **Supabase**: Go to https://supabase.com/dashboard/project/{project_id}/settings/api-keys
- **Stripe**: Go to https://dashboard.stripe.com/apikeys for API keys, and https://dashboard.stripe.com/webhooks for webhook secrets.

## Database Schema

This project uses **Supabase** as the database provider. The database schema is managed under the **public** schema. You
can find the Data Definition Language (DDL) file that contains all required table definitions in the repository:

[Database DDL - supabase_ddl.sql](./supabase_ddl.sql)

To set up the database, run the following command:

```sh
  psql -h <supabase-host> -U <username> -d <database-name> -f supabase_ddl.sql
```

`supabase_ddl.sql` is the only schema file: there is no migrations folder. A change for existing databases is written as an idempotent section of that file (IF NOT EXISTS, CREATE OR REPLACE, guarded DO blocks); to apply it, run that section in one transaction, then `NOTIFY pgrst, 'reload schema';`.

## Running the Application

Run the FastAPI server:

```sh
  uvicorn app.main:esim_app --host 0.0.0.0 --port 8000 --reload
```

- The API documentation will be available at:
    - Swagger UI: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs)

## API Endpoints

### Authentication
| Method | Endpoint                     | Description                                                     |
|--------|------------------------------|-----------------------------------------------------------------|
| `POST` | `/api/v1/auth/login`         | Registers a new user / login current user / sends OTP via email |
| `POST` | `/api/v1/auth/verify_otp`    | Verify OTP and return access and refresh token                  |
| `POST` | `/api/v1/auth/refresh-token` | Generate new access and refresh token                           |

### Bundles
| Method | Endpoint                     | Description                                |
|--------|------------------------------|--------------------------------------------|
| `GET`  | `/api/v1/bundles`           | Get available eSIM bundles                 |
| `POST` | `/api/v1/bundles/purchase`  | Purchase an eSIM bundle                    |

### User Management
| Method | Endpoint                     | Description                                |
|--------|------------------------------|--------------------------------------------|
| `GET`  | `/api/v1/user/bundles`      | Get user's purchased bundles               |
| `POST` | `/api/v1/user/bundles`      | Activate a purchased bundle                |

### Wallet
| Method | Endpoint                     | Description                                |
|--------|------------------------------|--------------------------------------------|
| `GET`  | `/api/v1/wallet/balance`    | Get user's wallet balance                  |
| `POST` | `/api/v1/wallet/topup`      | Add funds to wallet                        |

### Vouchers
| Method | Endpoint                     | Description                                |
|--------|------------------------------|--------------------------------------------|
| `POST` | `/api/v1/voucher/redeem`    | Redeem a voucher code                      |

### Promotions
| Method | Endpoint                     | Description                                |
|--------|------------------------------|--------------------------------------------|
| `GET`  | `/api/v1/promotion/active`  | Get active promotions                      |

### Health Check
| Method | Endpoint                     | Description                                |
|--------|------------------------------|--------------------------------------------|
| `GET`  | `/health`                   | Check API health status                    |

## 🔄 Data Synchronization

**Sync all bundles from the eSIM hub to your database**

To keep your local database up-to-date with the latest bundles from the eSIM hub, you can trigger a full sync at any time. This is especially useful after initial setup, deployments, or when new bundles are added to the eSIM hub.

**How to sync:**

Send a POST request to:

    /api/v1/callback/bundle/sync-all

This will fetch all bundles from the eSIM hub and update your database accordingly.

You can use tools like `curl`, Postman, or your own integration to call this endpoint:

```sh
curl -X POST http://localhost:8000/api/v1/callback/bundle/sync-all
```

## Error Handling

The application implements comprehensive error handling for:
- Validation errors
- Authentication errors
- Custom business logic errors
- Global exception handling
- Request validation errors

All errors are logged using the configured logger and return standardized error responses.

## Deployment

For production deployment, use:

```sh
  uvicorn main:esim_app --host 0.0.0.0 --port 8000
```

Or use a process manager like **Gunicorn** with **Uvicorn**:

```sh
  gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:esim_app
```

## Admin API

The admin portal (admin-open-source) calls this backend under `/api/v1/admin`. The code lives only in admin folders (`app/api/v1/admin/`, `app/services/admin/`, `app/repo/admin/`, `app/schemas/admin/`, `tests/admin/`) and the admin section of `supabase_ddl.sql`; the existing endpoints are unchanged.

- **Auth:** `POST /api/v1/admin/auth/login` with `{email, password}` returns an access and a refresh token. Send `Authorization: Bearer <access_token>` on every other admin call; no `X-Device-ID`. The role is read from `app_metadata.role` of the Supabase user: `admin` or `super_admin`. `GET /api/v1/admin/auth/me` returns the role and the permission list.
- **Permissions:** each route needs one `admin.<area>.<action>` permission. The role-to-permission table is `app/config/admin_permissions.py`. 401 means no or invalid session, 403 means a missing permission.
- **Lists:** `page` (from 1) and `page_size` (default 20, max 100); `search` is literal. The response is the usual envelope with `data = {data: [...], total_count, facets?, totals?}`. An unknown query parameter returns 422.
- **Errors:** the usual failed envelope; `responseCode` is the HTTP status. For 409 the `developerMessage` is a JSON string with a machine code, for example `{"code":"duplicate_code"}`. Database or storage failures return 502.
- **Interactive docs:** `/docs`, routes under the admin tag.

### Set up the database for the admin API

Every SQL object the admin API needs is in `supabase_ddl.sql`, in the section that starts with `ADMIN API (MS-5017)` at the end of the file. That section is idempotent: on an existing database, run it alone, in one transaction, after review. Then give one account a role (the user must sign in again):

```sql
UPDATE auth.users
   SET raw_app_meta_data = coalesce(raw_app_meta_data, '{}'::jsonb) || '{"role":"super_admin"}'::jsonb
 WHERE email = 'user@example.com';   -- or "admin"
```

After applying, if the API still answers 502 with "Could not find the function", reload the PostgREST schema cache: `NOTIFY pgrst, 'reload schema';`.

### Admin settings (.env)

`ADMIN_MEDIA_BUCKET`, `ADMIN_MAX_UPLOAD_BYTES` (1 MB), `ADMIN_MAX_JSON_BYTES`, `ADMIN_EXPORT_ROW_CAP`, `ADMIN_LOGIN_RATE_LIMIT`, `ADMIN_TRUSTED_PROXY_HOPS`. Defaults and descriptions are in `.env.example`.

The login rate limit is counted in the database, shared by every worker: the `admin_login_attempt` table and functions (section "admin login rate limit" of `supabase_ddl.sql`, part of the ADMIN API section) must be applied before deploying (without it every admin login answers 502).

### Row level security on the per-user tables

A user JWT against PostgREST (supabase-js, or a hand-made request with the anon key from `/configurations`) reads only that user's rows of the per-user tables; anon reads none. The API keeps the service key and is not affected. Tables, policies, the rollout checklist and how to run the live isolation tests: [docs/rls-user-tables.md](docs/rls-user-tables.md).

### Run the admin tests

```powershell
$env:PYTHONPATH="."; .\.venv\Scripts\python.exe -m pytest tests/admin -q -p no:cacheprovider
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature-name`)
3. Commit changes (`git commit -m "Added feature X"`)
4. Push to the branch (`git push origin feature-name`)
5. Open a pull request

## License

This project is licensed under the MIT License. See `LICENSE` for details.
