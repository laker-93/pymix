import logging

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, HTTPException

from pymix.containers import Container


router = APIRouter()

logger = logging.getLogger(__name__)

@router.get("/healthcheck", tags=["Maintenance"])
@inject
async def healthcheck(
        config: dict = Depends(Provide[Container.config]),
)-> dict:
    logger.info(f'Getting healthcheck')
    config_to_return = config  # todo hide my secrets
    try:
        # todo call out to modules to verify things are good
        resp = {
            "is_healthy": True
        }
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
