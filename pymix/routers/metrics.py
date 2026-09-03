"""The Prometheus scrape endpoint.

**This route is authenticated, and that is not optional.** pymix is published at
pymix.sub-box.net, so anything mounted here is reachable from the open internet --
unlike the per-user Navidrome `/metrics` endpoints the existing dashboard scrapes, which
are only addressable on the Docker network. Unauthenticated, this would hand a stranger
the user count, the invite backlog, the job failure rate and the memory headroom of the
whole platform.

The credential is its own env var rather than `PYMIX_ADMIN_TOKEN`, because the thing
holding it is a scraper on the host that only ever needs to read this one page.
`PYMIX_ADMIN_TOKEN` can recreate any user's containers; a scrape config is a bad place
to keep it, and rotating one should not force rotating the other.

`Authorization: Bearer` rather than a custom header (the shape `admin.py` uses) because
both Prometheus and vmagent support bearer auth natively in a scrape config, so wiring
this up needs no vendor-specific `headers:` block.
"""
import logging
import os
import secrets
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from pymix.services import metrics as metrics_service

router = APIRouter(tags=["Metrics"])

logger = logging.getLogger(__name__)

_UNAUTHORIZED = "Not authorized: a valid Authorization: Bearer token is required."


def require_metrics_token(authorization: str = Header(default=None)) -> None:
    expected = os.environ.get("PYMIX_METRICS_TOKEN")
    if not expected:
        # Fail closed, exactly as `require_admin_token` does. An unset token must never
        # read as "no auth required" -- that is how a public endpoint gets created by
        # forgetting an env var, which is the single most likely way to get this wrong.
        logger.error("PYMIX_METRICS_TOKEN is not set; refusing to serve /metrics")
        raise HTTPException(status_code=503, detail="Metrics are not configured.")

    scheme, _, credential = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not credential:
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED)
    # compare_digest, not ==, so a wrong token cannot be recovered a byte at a time by
    # timing the response.
    if not secrets.compare_digest(credential.strip(), expected):
        raise HTTPException(status_code=401, detail=_UNAUTHORIZED)


@router.get("/metrics", dependencies=[Depends(require_metrics_token)])
async def metrics() -> Response:
    """Prometheus exposition of everything in `services/metrics.py`.

    Returned as a plain `Response` with the exposition content type -- a JSONResponse
    would be unparseable to a scraper.
    """
    return Response(
        content=generate_latest(metrics_service.REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )


async def metrics_middleware(request: Request, call_next):
    """Time every request and record it under its route template.

    Wrapped in try/finally so a handler that raises is still counted -- an endpoint that
    only fails would otherwise be *absent* from the metrics rather than visibly at 100%
    5xx, which is the opposite of what an alert needs.

    The scrape itself is excluded. Including it would add a request every scrape
    interval to every rate and latency panel, in a service that is otherwise quiet
    enough for that to dominate the graph.
    """
    if request.url.path.endswith("/metrics"):
        return await call_next(request)

    started_at = time.monotonic()
    status_code = 500
    metrics_service.http_requests_in_flight.inc()
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        metrics_service.http_requests_in_flight.dec()
        # Read after call_next: Starlette resolves the route during the call, so before
        # it `request.scope["route"]` is not set and every label would be "<unmatched>".
        route = metrics_service.route_label(request)
        metrics_service.observe_request(request.method, route, status_code, started_at)
