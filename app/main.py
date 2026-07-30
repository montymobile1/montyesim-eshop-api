import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError, ValidationException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from loguru import logger
from pydantic import ValidationError
from starlette.responses import JSONResponse

from app.api.v1 import router
from app.config.settings import validate_settings
from app.exceptions import CustomException
from app.schemas.response import ResponseHelper
from app.i18n import translate, get_locale, set_locale
from app.services.scheduler_service import SchedulerService


@asynccontextmanager
async def lifespan(app: FastAPI):
    # fail fast when the typed settings hold invalid values
    app.state.settings = validate_settings()
    app.state.scheduler_service = SchedulerService()
    app.state.scheduler_service.start_scheduler()
    try:
        yield
    finally:
        app.state.scheduler_service.shutdown_scheduler()


esim_app = FastAPI(lifespan=lifespan, title="eSIM Reseller Backend Open Source",
                   description="eSIM Reseller Backend Open Source using FAST API Framework",
                   version="1.0")

logger.add("esim_opensource.log", rotation="10 MB", level="INFO", compression="zip")
logger.info("Application started")


@esim_app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Exception: {exc} {request.url.path}")
    response_data = ResponseHelper.error_response(status_code=500, title="Exception", error="Internal Server Exception",
                                                  developer_message=str(exc))
    return JSONResponse(
        status_code=500,
        content=jsonable_encoder(response_data),
    )


@esim_app.exception_handler(HTTPException)
async def custom_unauthorized_handler(request: Request, exc: HTTPException):
    logger.error(f"Http Exception: {exc} {request.url.path}")
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
    logger.error(f"CustomException: {exc} {request.url.path}")
    try:
        # middleware already sets the locale for this request; use translate() to fetch message
        title = translate(exc.name, params=getattr(exc, "params", None))
        response_data = ResponseHelper.error_response(status_code=exc.code, title=title,
                                                      error=title, developer_message=exc.details)
        return JSONResponse(
            status_code=exc.code,
            content=jsonable_encoder(response_data),
        )
    except Exception as e:
        logger.error(f"Error in CustomException handler: {e}")
        response_data = ResponseHelper.error_response(status_code=500, title="INTERNAL_SERVER_ERROR",
                                                      error="INTERNAL_SERVER_ERROR",
                                                      developer_message="INTERNAL_SERVER_ERROR")
        return JSONResponse(
            status_code=exc.code,
            content=jsonable_encoder(response_data),
        )


@esim_app.exception_handler(RequestValidationError)
async def handle_request_validation_exception(request: Request, exc: ValidationException):
    return handle_validations(request, exc)


@esim_app.exception_handler(ValidationError)
async def handle_validation_error(request: Request, exc: ValidationException):
    return handle_validations(request, exc)


@esim_app.exception_handler(ValidationException)
async def handle_validation_exception(request: Request, exc: ValidationException):
    return handle_validations(request, exc)


def handle_validations(request: Request, exc):
    logger.error(f"RequestValidationError: {exc} {request.url.path}")
    errors = exc.errors()
    formatted_errors = []
    title = ""
    for error in errors:
        field_location = " → ".join(map(str, error["loc"]))
        formatted_errors.append(f"{field_location}: {error['msg']}")
        title = error["msg"].split(",")[1].strip() if len(error["msg"].split(",")) > 1 else error["msg"]
    # translate title using the current request locale
    title = translate(title)
    error = f"Validation error: {', '.join(formatted_errors)}"
    response_data = ResponseHelper.error_response(status_code=422, error=error, title=title,
                                                  developer_message=error)
    return JSONResponse(
        status_code=400,
        content=jsonable_encoder(response_data),
    )


@esim_app.middleware("http")
async def add_cors_headers(request, call_next):
    # set locale for this request so anywhere in request handling can use app.i18n.translate()
    try:
        lang_header = request.headers.get('accept-language', 'en')
        set_locale(lang_header)
    except Exception:
        set_locale('en')
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
esim_app.include_router(prefix=api_version, router=router)
