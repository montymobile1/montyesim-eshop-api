import datetime

from fastapi import APIRouter
from loguru import logger

from app.config.config import supabase_client, esim_hub_service_instance

router = APIRouter()


@router.get("/")
async def health_check():
    supabase_status = await __check_supabase_connection()
    esim_hub_status = await __check_esim_hub_connection()
    response = {
        "status": "ok",
        "server_time": datetime.datetime.strftime(datetime.datetime.now(), "%Y-%m-%d %H:%M:%S"),
        "supabase": supabase_status,
        "esim_hub": esim_hub_status
    }
    return response


async def __check_supabase_connection():
    try:
        client = supabase_client()
        response = (client.table("users_copy").select("*").limit(1).execute())
        logger.info(response)
        return "ok"
    except Exception as e:
        logger.error(f"error on healthcheck for supabase connection: {e}")
        return "failed"


async def __check_esim_hub_connection():
    try:
        esim_hub_service = esim_hub_service_instance()
        return await esim_hub_service.health_check()
    except Exception as e:
        logger.error(f"error on healthcheck for esimhub connection: {e}")
        return "failed"
