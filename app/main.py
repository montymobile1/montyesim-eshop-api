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
from app.exceptions import CustomException
from app.schemas.response import ResponseHelper
from app.services.scheduler_service import SchedulerService

scheduler_service = SchedulerService()


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler_service.start_scheduler()
    yield
    scheduler_service.shutdown_scheduler()


esim_app = FastAPI(lifespan=lifespan, title="eSIM Reseller Backend Open Source",
                   description="eSIM Reseller Backend Open Source using FAST API Framework",
                   version="1.0")
logger.add("esim_opensource.log", rotation="10 MB", level="INFO", compression="zip")
logger.info("Application started")


def load_messages(lang):
    ROOT_PATH = os.path.abspath(os.curdir)
    path = f"{ROOT_PATH}/locales/{lang}.json"
    if not os.path.exists(path):
        path = f"{ROOT_PATH}/locales/en.json"
    with open(path, "r") as f:
        return json.load(f)


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
    try:
        logger.error(f"CustomException: {exc} {request.url.path}")
        lang_header = request.headers.get('accept-language', 'en')
        lang = lang_header.split('-')[0].lower()
        messages = load_messages(lang)
        title = messages.get(exc.name, exc.name)

        # Create a simple dictionary structure that's guaranteed to be JSON-serializable
        error_response = {
            "status_code": exc.code,
            "title": str(title),  # Ensure title is a string
            "error": str(exc.name),  # Ensure error is a string
            "developer_message": str(exc.details) if exc.details else None  # Handle potential None
        }

        # Create response using the simplified structure
        response_data = ResponseHelper.error_response(**error_response)

        # Log the response data for debugging
        logger.debug(f"Response data before encoding: {response_data}")

        # Encode with custom handling
        encoded_content = jsonable_encoder(
            response_data,
            exclude_none=True,  # Remove None values
            custom_encoder={
                datetime: lambda dt: dt.isoformat(),  # Handle datetime objects
                bytes: lambda b: b.decode(),  # Handle byte strings
            }
        )

        return JSONResponse(
            status_code=exc.code,
            content=encoded_content,
        )
    except Exception as e:
        # If JSON encoding fails, return a basic error response
        logger.error(f"Error in exception handler: {str(e)}")
        return JSONResponse(
            status_code=500,
            content={
                "status_code": 500,
                "title": "Internal Server Error",
                "error": "JSON_ENCODING_ERROR",
                "developer_message": "Could not encode error response"
            }
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
    logger.error(f"RequestValidationError: {exc} {request.url.path}")
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
esim_app.include_router(prefix=api_version, router=router)
