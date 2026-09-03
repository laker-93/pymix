"""Prometheus metrics for pymix itself.

The existing Grafana dashboard scrapes each per-user `navidrome{user}` container
(../subbox-workspace/docs/monitoring.md), which answers "is the user's music server up
and how big is their library". It says nothing about the service that actually does the
work: every import, export, upload and sync runs through pymix, so a slow or failing
conversion is invisible to that dashboard. This module is the pymix-side half.

Two sources, deliberately kept apart:

  * **Instrumented** -- HTTP request counts and latencies, recorded by middleware as
    requests happen. These accumulate; they are the ones that answer "what is slow"
    and "what is erroring".
  * **Sampled** -- a collector that reads the current state (users, invites, jobs,
    memory) at scrape time. Nothing accumulates, so there is no state to lose across a
    restart, and adding a gauge costs one query rather than a code path that has to
    remember to update it.

Everything registers into `REGISTRY` below rather than prometheus_client's global
default, so importing this module never mutates process-wide state and a test can build
its own registry without interference.
"""
import logging
import os
import time

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.metrics_core import InfoMetricFamily
from prometheus_client.process_collector import ProcessCollector
from prometheus_client.platform_collector import PlatformCollector

from pymix.utils import memdiag

logger = logging.getLogger(__name__)

REGISTRY = CollectorRegistry()

# RSS, CPU, open file descriptors, and the Python/platform info series. Free, standard
# names (`process_resident_memory_bytes` and friends), and the baseline any Grafana
# panel for a Python service expects to find.
ProcessCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)

# Buckets sized for this service, not for a web app. A pymix request is rarely a bare
# DB read -- imports shell out to beets, exports walk the filesystem and zip it -- so
# the interesting range runs from milliseconds to minutes. The default buckets top out
# at 10s, which would collapse every real import into one +Inf bucket and make the
# p99 unreadable exactly where it matters.
_DURATION_BUCKETS = (
    0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, float("inf"),
)

http_requests_total = Counter(
    "pymix_http_requests_total",
    "HTTP requests handled, by route template, method and status class.",
    ["method", "route", "status"],
    registry=REGISTRY,
)

http_request_duration_seconds = Histogram(
    "pymix_http_request_duration_seconds",
    "Wall-clock time to handle a request, by route template.",
    ["method", "route"],
    buckets=_DURATION_BUCKETS,
    registry=REGISTRY,
)


def route_label(request) -> str:
    """The matched route *template* (`/user/create`), never the raw path.

    Cardinality control, and the reason this is not just `request.url.path`. Paths like
    `/beets/{username}/status` would otherwise mint a new series per user, and -- far
    worse on an endpoint the public internet can reach -- every 404 from a vulnerability
    scanner would mint one too, permanently. An unmatched request is bucketed under a
    single `<unmatched>` label instead.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if path else "<unmatched>"


class PymixStateCollector:
    """Reads pymix's current state on each scrape.

    Registered as a collector rather than written as a set of gauges updated from
    business logic: a gauge that some code path is responsible for updating is a gauge
    that goes stale the first time someone adds a path and forgets. Sampling means the
    numbers cannot silently drift from the database.

    Every sample is individually guarded. A metrics endpoint that 500s during an
    incident is worse than useless -- it takes the dashboard down at the moment the
    dashboard is the thing you need -- so a failing sample is logged and omitted while
    the rest of the scrape still returns.
    """

    def __init__(self, db_controller, max_number_of_users: int, environment: str):
        self._db = db_controller
        self._max_number_of_users = max_number_of_users
        self._environment = environment

    def collect(self):
        yield InfoMetricFamily(
            "pymix_build",
            "Version and environment of the running pymix.",
            value={
                # Image tags are applied by hand at build time (docs/deployment.md), so
                # this is only as good as the compose file that sets it. Left as
                # "unknown" rather than guessed: a wrong version on a dashboard is worse
                # than an absent one when you are trying to confirm what a deploy shipped.
                "version": os.environ.get("PYMIX_VERSION", "unknown"),
                "environment": self._environment,
            },
        )

        yield from self._user_metrics()
        yield from self._invite_metrics()
        yield from self._job_metrics()
        yield from self._wishlist_metrics()
        yield from self._memory_metrics()

    def _user_metrics(self):
        cap = GaugeMetricFamily(
            "pymix_users_max",
            "Configured maximum number of users (`max_number_of_users`).",
        )
        cap.add_metric([], self._max_number_of_users)
        yield cap

        try:
            total = self._db.get_total_number_of_users()
        except Exception:
            logger.exception("metrics: could not count users")
            return
        users = GaugeMetricFamily(
            "pymix_users_total",
            "Users with a row in user_table. Counts toward pymix_users_max even if "
            "their containers have been removed -- removing containers does not remove "
            "the row, so this is the number that actually gates a signup.",
        )
        users.add_metric([], total)
        yield users

    def _invite_metrics(self):
        try:
            tokens = self._db.count_signup_tokens()
        except Exception:
            logger.exception("metrics: could not count signup tokens")
        else:
            gauge = GaugeMetricFamily(
                "pymix_signup_tokens",
                "Signup tokens by whether they have been redeemed.",
                labels=["state"],
            )
            for state, count in sorted(tokens.items()):
                gauge.add_metric([state], count)
            yield gauge

        try:
            by_status = self._db.count_invite_requests_by_status()
        except Exception:
            logger.exception("metrics: could not count invite requests by status")
        else:
            gauge = GaugeMetricFamily(
                "pymix_invite_requests",
                "Beta-invite requests by fulfilment status. `new` is the unworked queue.",
                labels=["status"],
            )
            for status, count in sorted(by_status.items()):
                gauge.add_metric([status], count)
            yield gauge

        try:
            by_software = self._db.count_invite_requests_by_dj_software()
        except Exception:
            logger.exception("metrics: could not count invite requests by dj software")
        else:
            gauge = GaugeMetricFamily(
                "pymix_invite_requests_by_dj_software",
                "Beta-invite requests by the software the requester DJs on.",
                labels=["dj_software"],
            )
            for software, count in sorted(by_software.items()):
                gauge.add_metric([software], count)
            yield gauge

    def _job_metrics(self):
        try:
            jobs = self._db.count_jobs_by_state()
        except Exception:
            logger.exception("metrics: could not count jobs")
            return
        gauge = GaugeMetricFamily(
            "pymix_jobs",
            "Import/export jobs by state. `in_progress` that does not fall is a stuck "
            "import: the client keeps polling and there is no other signal.",
            labels=["state"],
        )
        for state, count in sorted(jobs.items()):
            gauge.add_metric([state], count)
        yield gauge

    def _wishlist_metrics(self):
        try:
            items = self._db.count_wishlist_items_by_status()
        except Exception:
            logger.exception("metrics: could not count wishlist items")
            return
        gauge = GaugeMetricFamily(
            "pymix_wishlist_items",
            "Wishlist items by status.",
            labels=["status"],
        )
        for status, count in sorted(items.items()):
            gauge.add_metric([status], count)
        yield gauge

    def _memory_metrics(self):
        """The numbers `handlers/mem_watch_handler` already logs, as metrics.

        `process_resident_memory_bytes` from ProcessCollector is not enough on its own
        here. Prod runs under a 768m cgroup limit where the kernel SIGKILLs with no
        warning, so the useful quantity is the charge *as a fraction of the limit*, and
        the leak in #120 was only legible in the allocator's in-use figure -- RSS
        stayed high afterwards because glibc retains freed arenas.
        """
        try:
            proc = memdiag.process_memory()
            alloc = memdiag.allocator_stats()
            cgroup = memdiag.cgroup_memory()
        except Exception:
            logger.exception("metrics: could not sample memory")
            return

        peak = GaugeMetricFamily(
            "pymix_process_resident_memory_peak_bytes",
            "Peak RSS since the process started (VmHWM).",
        )
        peak.add_metric([], proc["rss_peak_mb"] * 1024 * 1024)
        yield peak

        if alloc.get("available"):
            in_use = GaugeMetricFamily(
                "pymix_allocator_in_use_bytes",
                "Heap actually in use, per the allocator. Watch this rather than RSS "
                "for a leak: glibc retains freed arenas, so RSS stays high after a "
                "genuine release (laker-93/pymix#120).",
            )
            in_use.add_metric([], alloc["in_use_mb"] * 1024 * 1024)
            yield in_use

            retained = GaugeMetricFamily(
                "pymix_allocator_retained_bytes",
                "Heap freed by the application but retained by the allocator.",
            )
            retained.add_metric([], alloc["retained_mb"] * 1024 * 1024)
            yield retained

        if cgroup.get("available"):
            current = GaugeMetricFamily(
                "pymix_cgroup_memory_bytes",
                "Current memory charged to the container's cgroup. This, not RSS, is "
                "what the kernel compares against the limit when it decides to OOM-kill.",
            )
            current.add_metric([], cgroup["current_mb"] * 1024 * 1024)
            yield current

            if cgroup.get("limit_mb"):
                limit = GaugeMetricFamily(
                    "pymix_cgroup_memory_limit_bytes",
                    "The container's memory limit. Alert on the ratio of "
                    "pymix_cgroup_memory_bytes to this, not on an absolute figure.",
                )
                limit.add_metric([], cgroup["limit_mb"] * 1024 * 1024)
                yield limit


def observe_request(method: str, route: str, status_code: int, started_at: float) -> None:
    """Record one finished request.

    `status` is the class (`2xx`, `5xx`), not the exact code. Three series per route
    instead of a dozen, which is all an alert on "is this route erroring" needs, and it
    keeps the label set bounded no matter what a handler decides to return.
    """
    http_requests_total.labels(
        method=method, route=route, status=f"{status_code // 100}xx"
    ).inc()
    http_request_duration_seconds.labels(method=method, route=route).observe(
        time.monotonic() - started_at
    )


# The live collector, if one has been registered. Module-level so registration can be
# idempotent: `create_app` runs more than once in a test session, and registering a
# second collector into the same registry raises on duplicate series.
_state_collector = None


def register_state_collector(db_controller, max_number_of_users: int, environment: str) -> None:
    """Point the registry at a database. Safe to call repeatedly.

    Called from `create_app` rather than at import time because the collector needs a
    wired DI container to get a db_controller, and because a module that opens a
    database connection on import is one that cannot be imported by a test.
    """
    global _state_collector
    if _state_collector is not None:
        REGISTRY.unregister(_state_collector)
    _state_collector = PymixStateCollector(db_controller, max_number_of_users, environment)
    REGISTRY.register(_state_collector)
