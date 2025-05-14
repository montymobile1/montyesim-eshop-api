from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError, ValidationException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from loguru import logger
from pydantic import ValidationError
from starlette.responses import JSONResponse

from app.api.v1.application import router as app_routes
from app.api.v1.authentication import router as auth_routes
from app.api.v1.bundles import router as bundle_routes
from app.api.v1.callback import router as notification_routes
from app.api.v1.health_check import router as health_check_router
from app.api.v1.home import router as home_routes
from app.api.v1.promotion import router as promotion
from app.api.v2.home import router as home_routes_v2
from app.api.v1.user_bundle import router as user_bundle_routes
from app.api.v1.user_wallet import router as user_wallet_router
from app.api.v1.promotion import router as promotion_router
from app.api.v1.voucher import router as voucher_router
from app.api.v2.home import router as home_routes_v2
from app.exceptions import CustomException
from app.schemas.response import ResponseHelper
from app.services.scheduler_service import SchedulerService


scheduler_service = SchedulerService()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    scheduler_service.start_scheduler()
    yield
    # Shutdown
    scheduler_service.shutdown_scheduler()

esim_app = FastAPI(lifespan=lifespan,title="eSIM Reseller Backend Open Source",
                   description="eSIM Reseller Backend Open Source using FAST API Framework",
                   version="1.0")
logger.add("esim_opensource.log", rotation="10 MB", level="INFO")
logger.info("Application started")


@esim_app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Exception: {exc}")
    response_data = ResponseHelper.error_response(status_code=500, title="Exception", error="Internal Server Exception",
                                                  developer_message=str(exc))
    return JSONResponse(
        status_code=500,
        content=jsonable_encoder(response_data),
    )


@esim_app.exception_handler(HTTPException)
async def custom_unauthorized_handler(request: Request, exc: HTTPException):
    logger.error(f"Http Exception: {exc}")
    title = "Http Exception"
    if exc.status_code == 401:
        title = "401 Unauthorized"
    elif exc.status_code == 403:
        title = "401 Unauthorized"
        exc.status_code = 401
    response_data = ResponseHelper.error_response(status_code=exc.status_code, title=title, error=exc.detail,
                                                  developer_message=str(exc))
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(response_data),
    )


@esim_app.exception_handler(CustomException)
async def global_exception_handler(request: Request, exc: CustomException):
    logger.error(f"CustomException: {exc}")
    response_data = ResponseHelper.error_response(status_code=exc.code, title=exc.name,
                                                  error=exc.name, developer_message=exc.details)
    return JSONResponse(
        status_code=exc.code,
        content=jsonable_encoder(response_data),
    )


@esim_app.exception_handler(RequestValidationError)
async def handle_request_validation_exception(request: Request, exc: ValidationException):
    return await handle_validations(request, exc)


@esim_app.exception_handler(ValidationError)
async def handle_validation_error(request: Request, exc: ValidationException):
    return await handle_validations(request, exc)


@esim_app.exception_handler(ValidationException)
async def handle_validation_exception(request: Request, exc: ValidationException):
    return await handle_validations(request, exc)


async def handle_validations(request: Request, exc):
    logger.error(f"RequestValidationError: {exc}")
    errors = exc.errors()
    formatted_errors = []
    for error in errors:
        field_location = " → ".join(map(str, error["loc"]))
        formatted_errors.append(f"{field_location}: {error['msg']}")
    error = f"Validation error: {', '.join(formatted_errors)}"
    response_data = ResponseHelper.error_response(status_code=422, error=error, title="Validation Error",
                                                  developer_message=error)
    return JSONResponse(
        status_code=400,
        content=jsonable_encoder(response_data),
    )


@esim_app.middleware("http")
async def add_cors_headers(request, call_next):
    response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    return response





# add cors middleware
esim_app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# add gzip middleware to reduce response size
esim_app.add_middleware(GZipMiddleware, minimum_size=500)

api_version = "/api/v1"
api_version_2 = "/api/v2"
esim_app.include_router(health_check_router, tags=["healthcheck"])
esim_app.include_router(home_routes, prefix=f"{api_version}/home", tags=["Home"])
esim_app.include_router(home_routes_v2, prefix=f"{api_version_2}/home", tags=["Home"])
esim_app.include_router(app_routes, prefix=f"{api_version}/app", tags=["App"])
esim_app.include_router(auth_routes, prefix=f"{api_version}/auth", tags=["Auth"])
esim_app.include_router(bundle_routes, prefix=f"{api_version}/bundles", tags=["Bundles"])
esim_app.include_router(notification_routes, prefix=f"{api_version}/callback", tags=["Callback"])
esim_app.include_router(user_bundle_routes, prefix=f"{api_version}/user", tags=["User"])
esim_app.include_router(user_wallet_router, prefix=f"{api_version}/wallet", tags=["Wallet"])
esim_app.include_router(voucher_router, prefix=f"{api_version}/voucher", tags=["Voucher"])
esim_app.include_router(promotion_router, prefix=f"{api_version}/promotion", tags=["Promotion"])



