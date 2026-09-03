"""The scrape endpoint: that it is shut to strangers, and that it exposes real numbers.

pymix is published at pymix.sub-box.net, so `/metrics` is reachable from the open
internet -- unlike the per-user Navidrome endpoints the existing dashboard scrapes,
which only exist on the Docker network. The auth tests below are the load-bearing ones.
"""
import sys
from unittest import mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pymix.controllers.db_controller import DbController
from pymix.model.db_tables import Base
from pymix.routers import metrics as metrics_router
from pymix.services import metrics as metrics_service


@pytest.fixture
def db_controller():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return DbController(
        session_factory=sessionmaker(bind=engine),
        app_env="test",
        max_library_size=0,
    )


@pytest.fixture
def client(db_controller, monkeypatch):
    monkeypatch.setenv("PYMIX_METRICS_TOKEN", "scrape-token")
    # The HTTP metrics are module-level singletons shared by every test in this file, so
    # without this each test would see the previous ones' requests still counted.
    metrics_service.http_requests_total.clear()
    metrics_service.http_request_duration_seconds.clear()
    metrics_service.register_state_collector(
        db_controller, max_number_of_users=10, environment="test"
    )
    app = FastAPI()
    app.include_router(metrics_router.router)
    app.middleware("http")(metrics_router.metrics_middleware)

    @app.get("/echo/{value}")
    async def echo(value: str):
        return {"value": value}

    @app.get("/boom")
    async def boom():
        raise HTTPException(status_code=503, detail="nope")

    return TestClient(app)


def _scrape(client) -> str:
    response = client.get("/metrics", headers={"Authorization": "Bearer scrape-token"})
    assert response.status_code == 200
    return response.text


# --- Auth -------------------------------------------------------------------------

def test_scrape_without_a_token_is_refused(client):
    assert client.get("/metrics").status_code == 401


def test_scrape_with_the_wrong_token_is_refused(client):
    response = client.get("/metrics", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_a_bare_token_without_the_bearer_scheme_is_refused(client):
    response = client.get("/metrics", headers={"Authorization": "scrape-token"})
    assert response.status_code == 401


def test_an_unset_token_fails_closed(client, monkeypatch):
    """The most likely way to get this wrong is deploying without setting the env var.
    That must not silently publish the endpoint."""
    monkeypatch.delenv("PYMIX_METRICS_TOKEN", raising=False)

    response = client.get("/metrics", headers={"Authorization": "Bearer scrape-token"})

    assert response.status_code == 503


def test_a_valid_token_is_served_in_the_exposition_format(client):
    response = client.get("/metrics", headers={"Authorization": "Bearer scrape-token"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


# --- State ------------------------------------------------------------------------

def test_the_user_count_and_cap_are_exposed(client, db_controller):
    db_controller.set_token("invite-1")
    db_controller.create_user("dj", "pw", "dj@example.com", "invite-1")

    body = _scrape(client)

    assert "pymix_users_total 1.0" in body
    assert "pymix_users_max 10.0" in body


def test_signup_tokens_are_split_by_whether_they_are_redeemed(client, db_controller):
    db_controller.set_token("claimed-1")
    db_controller.set_token("spare-1")
    db_controller.set_token("spare-2")
    db_controller.create_user("dj", "pw", "dj@example.com", "claimed-1")

    body = _scrape(client)

    assert 'pymix_signup_tokens{state="claimed"} 1.0' in body
    assert 'pymix_signup_tokens{state="unclaimed"} 2.0' in body


def test_invite_requests_are_exposed_by_status_and_software(client, db_controller):
    db_controller.create_invite_request(
        email="a@example.com", dj_software="rekordbox", dj_software_other=None
    )
    db_controller.create_invite_request(
        email="b@example.com", dj_software="serato", dj_software_other=None
    )

    body = _scrape(client)

    assert 'pymix_invite_requests{status="new"} 2.0' in body
    assert 'pymix_invite_requests_by_dj_software{dj_software="rekordbox"} 1.0' in body
    assert 'pymix_invite_requests_by_dj_software{dj_software="serato"} 1.0' in body


def test_every_known_status_has_a_series_even_at_zero(client, db_controller):
    """A series that only appears once it is non-zero cannot be alerted on, and reads
    as a gap in the graph rather than as 'none yet'."""
    body = _scrape(client)

    assert 'pymix_invite_requests{status="new"} 0.0' in body
    assert 'pymix_invite_requests{status="invited"} 0.0' in body
    assert 'pymix_invite_requests{status="declined"} 0.0' in body


def test_jobs_are_exposed_by_state(client, db_controller):
    db_controller.set_token("invite-1")
    db_controller.create_user("dj", "pw", "dj@example.com", "invite-1")
    db_controller.create_import_job("dj", number_of_tracks_to_import=5, total_n_imported_tracks=0)

    body = _scrape(client)

    assert 'pymix_jobs{state="in_progress"} 1.0' in body
    assert 'pymix_jobs{state="succeeded"} 0.0' in body


def test_build_info_is_exposed(client):
    body = _scrape(client)

    assert "pymix_build_info{" in body
    assert 'environment="test"' in body


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="ProcessCollector and memdiag both read /proc, which only exists on Linux. "
           "pymix only ever runs in a Linux container; this asserts what prod exposes.",
)
def test_memory_metrics_are_exposed(client):
    body = _scrape(client)

    # pymix's own, from memdiag -- the cgroup and allocator figures the standard
    # collector does not provide, and the ones the #120 leak was only legible in.
    assert "pymix_process_resident_memory_peak_bytes" in body
    assert "pymix_allocator_in_use_bytes" in body
    assert "pymix_cgroup_memory_bytes" in body
    # From prometheus_client's ProcessCollector, i.e. the standard names a Grafana
    # panel for a Python service expects to find.
    assert "process_resident_memory_bytes" in body


def test_a_failing_sample_does_not_fail_the_whole_scrape(client, db_controller):
    """A /metrics that 500s during an incident takes the dashboard down exactly when it
    is needed."""
    with mock.patch.object(
        db_controller, "count_jobs_by_state", side_effect=RuntimeError("db gone")
    ):
        body = _scrape(client)

    # Anchored to the start of a line: `pymix_jobs{` also appears mid-line in the
    # HELP text of pymix_jobs_completed_total, which cross-references it.
    assert "\npymix_jobs{" not in body
    # The rest of the scrape still came back.
    assert "pymix_users_total" in body


# --- HTTP instrumentation ----------------------------------------------------------

def test_requests_are_counted_under_their_route_template(client):
    client.get("/echo/alice")
    client.get("/echo/bob")

    body = _scrape(client)

    # One series for the template, not one per value -- otherwise every distinct path
    # segment mints a permanent series.
    assert 'pymix_http_requests_total{method="GET",route="/echo/{value}",status="2xx"} 2.0' in body
    assert "alice" not in body


def test_unmatched_paths_share_a_single_series(client):
    """An endpoint the public internet can reach gets scanned. Every 404 must not mint
    a permanent series."""
    client.get("/wp-admin.php")
    client.get("/.env")

    body = _scrape(client)

    assert 'route="<unmatched>"' in body
    assert "wp-admin" not in body


def test_a_failing_handler_is_still_counted(client):
    client.get("/boom")

    body = _scrape(client)

    assert 'pymix_http_requests_total{method="GET",route="/boom",status="5xx"} 1.0' in body


def test_request_duration_is_recorded(client):
    client.get("/echo/alice")

    body = _scrape(client)

    assert 'pymix_http_request_duration_seconds_count{method="GET",route="/echo/{value}"} 1.0' in body


def test_the_scrape_itself_is_not_counted(client):
    _scrape(client)
    body = _scrape(client)

    assert 'route="/metrics"' not in body


def test_in_flight_returns_to_zero_after_a_request(client):
    """A gauge that leaks on the error path is worse than no gauge: it climbs forever
    and eventually reads as permanent saturation."""
    client.get("/echo/alice")
    client.get("/boom")

    body = _scrape(client)
    assert "pymix_http_requests_in_flight 0.0" in body


def test_the_scrape_itself_is_not_counted_in_flight(client):
    """The scrape is excluded from the request metrics, so it must not touch the gauge
    either -- otherwise every scrape reports one request in flight: its own."""
    body = _scrape(client)

    assert "pymix_http_requests_in_flight 0.0" in body
