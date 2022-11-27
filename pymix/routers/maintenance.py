import datetime
import logging

from toredocore.providers.healthcheck.async_healthcheck_provider import AsyncHealthcheckProvider
from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, HTTPException

from pymix.containers import Container


router = APIRouter()

logger = logging.getLogger(__name__)

@router.get("/healthcheck", tags=["Maintenance"])
@inject
async def healthcheck(
        config: dict = Depends(Provide[Container.config]),
        healthcheck_provider: AsyncHealthcheckProvider = Depends(Provide[Container.healthcheck_provider])
)-> dict:
    logger.info(f'Getting healthcheck')
    config_to_return = config  # todo hide my secrets
    try:
        resp = await healthcheck_provider.health_check()
    except Exception as exc:
        logger.exception(exc)
        raise HTTPException(status_code=500, detail=str(exc))
    else:
        logger.info("Returning healthcheck")
        resp.update(
            {
                "app_config": config_to_return
            }
        )
        return resp
