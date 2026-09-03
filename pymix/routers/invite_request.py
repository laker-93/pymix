"""Beta-invite capture — the one write route with no caller identity.

Everything else in pymix resolves its caller through `require_user`. This route can't:
the whole point is to capture someone who has *no* account, from the demo session or the
landing page (subbox-app#69). That makes it the only unauthenticated write in the API, so
the abuse controls that would normally be the session cookie's job live here instead —
a per-IP cap and a body-size cap, both deliberately minimal (no captcha until it's
actually abused).
"""

import json
import logging
import time
from collections import defaultdict, deque

from dependency_injector.wiring import Provide, inject
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError

from pymix.containers import Container
from pymix.controllers.db_controller import DbController
from pymix.model.api.invite_requests import CreateInviteRequest
from pymix.services import metrics

router = APIRouter()

logger = logging.getLogger(__name__)

# A single form submission is a couple of hundred bytes; anything approaching this is
# not a person filling in a form. Enforced before the body is parsed.
MAX_BODY_BYTES = 4096

# Per-IP cap. Generous enough that a real user correcting their answer a few times never
# hits it, low enough that scripted signups aren't free.
RATE_LIMIT_MAX_REQUESTS = 5
RATE_LIMIT_WINDOW_S = 3600

# In-process only, so it resets on restart and isn't shared across replicas. That is
# accepted: this is a speed bump, not a security boundary, and pymix runs as one process.
_recent_requests: dict[str, deque] = defaultdict(deque)


def _client_ip(request: Request) -> str:
    """The caller's IP as seen by the proxy in front of pymix.

    Take the *rightmost* X-Forwarded-For entry, not the leftmost: pymix is only reachable
    through Traefik, which appends the peer it actually saw to whatever the client sent.
    The leftmost entry is therefore attacker-controlled (it would let one host rotate the
    header to buy unlimited quota); the rightmost is Traefik's own observation.
    """
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        entries = [entry.strip() for entry in forwarded_for.split(",") if entry.strip()]
        if entries:
            return entries[-1]
    return request.client.host if request.client else "unknown"


def check_rate_limit(request: Request) -> None:
    """Per-IP cap, wired as a route-level dependency so it runs before the body is read."""
    ip = _client_ip(request)
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_S

    # Drop every key that has fully aged out, not just this caller's — otherwise the map
    # grows once per distinct IP forever on a route anyone can reach.
    for key in [k for k, hits in _recent_requests.items() if not hits or hits[-1] <= cutoff]:
        del _recent_requests[key]

    hits = _recent_requests[ip]
    while hits and hits[0] <= cutoff:
        hits.popleft()

    if len(hits) >= RATE_LIMIT_MAX_REQUESTS:
        logger.warning("Rate-limited invite requests from %s", ip)
        metrics.observe_invite_submission("rate_limited")
        raise HTTPException(status_code=429, detail="Too many invite requests. Try again later.")

    hits.append(now)


async def parse_invite_request(request: Request) -> CreateInviteRequest:
    """Read and validate the body, rejecting bad input with 400 rather than FastAPI's 422.

    Declared as a dependency rather than a `body:` parameter so the size cap can run
    before anything is parsed, and so the client gets one consistent 400 for "we couldn't
    use what you sent" — it renders that inline against the email field.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_BODY_BYTES:
        metrics.observe_invite_submission("too_large")
        raise HTTPException(status_code=413, detail="Request body too large")

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        metrics.observe_invite_submission("too_large")
        raise HTTPException(status_code=413, detail="Request body too large")

    try:
        payload = json.loads(raw)
    except ValueError:
        metrics.observe_invite_submission("invalid")
        raise HTTPException(status_code=400, detail="Invalid request body")

    try:
        return CreateInviteRequest.model_validate(payload)
    except ValidationError as e:
        # The one rejection a real person can trip: a typo'd address, or a dj_software
        # the client offered that pymix does not accept. If this rises after a client
        # release, the two ends have drifted and every one of those is a lost signup.
        logger.info("Rejected invite request: %s", e.errors())
        metrics.observe_invite_submission("invalid")
        raise HTTPException(status_code=400, detail="Invalid email address or DJ software")


@router.post("/invite-request", dependencies=[Depends(check_rate_limit)], tags=["invite"])
@inject
async def create_invite_request(
    body: CreateInviteRequest = Depends(parse_invite_request),
    db_controller: DbController = Depends(Provide[Container.db_controller]),
) -> dict:
    """Capture interest in the private beta. Unauthenticated by design.

    The response is a flat `{"status": "ok"}` whether the address was new or already on
    the list: telling the caller which would turn this into an address-membership oracle
    for anyone curious who has signed up.
    """
    try:
        outcome = db_controller.create_invite_request(
            email=str(body.email),
            dj_software=body.dj_software,
            dj_software_other=body.dj_software_other,
        )
    except Exception:
        # Counted before re-raising. Left to surface as a 500, unchanged -- the point
        # of the counter is that the failure is legible on a dashboard, not that it is
        # swallowed. A submission lost here is a person lost, so it must be the loudest
        # outcome in the set.
        logger.exception("failed to record invite request")
        metrics.observe_invite_submission("error")
        raise

    metrics.observe_invite_submission(outcome)

    return {"status": "ok"}
