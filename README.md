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

## Database Schema

This project uses **Supabase** as the database provider. The database schema is managed under the **public** schema. You
can find the Data Definition Language (DDL) file that contains all required table definitions in the repository:

[Database DDL - supabase_ddl.sql](./supabase_ddl.sql)

To set up the database, run the following command:

```sh
  psql -h <supabase-host> -U <username> -d <database-name> -f supabase_ddl.sql
```

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

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature-name`)
3. Commit changes (`git commit -m "Added feature X"`)
4. Push to the branch (`git push origin feature-name`)
5. Open a pull request

## License

This project is licensed under the MIT License. See `LICENSE` for details.

