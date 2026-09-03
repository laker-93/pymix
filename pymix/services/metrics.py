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
import threading
import time

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram
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

http_requests_in_flight = Gauge(
    "pymix_http_requests_in_flight",
    "Requests being handled right now. The saturation signal the duration histogram "
    "cannot give: on a 1 vCPU droplet a slow route does not just get slower, it queues "
    "everything behind it, and this is where that shows up first.",
    registry=REGISTRY,
)

# ---------------------------------------------------------------------------
# The signup funnel
# ---------------------------------------------------------------------------
#
# The sampled gauges below (`pymix_invite_requests`, `pymix_signup_tokens`,
# `pymix_users_total`) show the funnel's *levels* -- how many people are sitting at
# each stage right now. They cannot show the drop between two stages, because a
# submission that never became a row leaves nothing behind to sample. These counters
# are that missing half: every submission is counted at the point it is decided, so
# `submissions - created` is exactly the set of people who pressed the button and did
# not end up in the table.
#
# One thing pymix genuinely cannot see: a click that never became a request at all
# (the browser blocked it, the network dropped it, client-side validation stopped it).
# The nearest available proxy is the CORS preflight -- browsers send `OPTIONS
# /invite-request` before the POST, and both land in `pymix_http_requests_total`, so
# OPTIONS materially exceeding POST means the form is reaching the server and the
# submission is not. Anything earlier than that needs telemetry in subbox-app.

# Every outcome a submission can reach. Kept as a tuple so the label values can be
# pre-created below: a counter that first appears when it is non-zero cannot be
# alerted on, and shows in Grafana as a gap rather than as a zero.
INVITE_SUBMISSION_OUTCOMES = (
    # A new row in invite_request_table. This is the only outcome that moves
    # `pymix_invite_requests`.
    "created",
    # A known address re-submitting. Deliberately *not* an error -- the write is an
    # upsert precisely so a second submission never fails -- but it is the largest
    # honest source of drift between submissions and rows, and without it a
    # dashboard reads a re-submission as a lost signup.
    "refreshed",
    # 400: the body, email or dj_software did not validate.
    "invalid",
    # 413: body over MAX_BODY_BYTES.
    "too_large",
    # 429: per-IP cap. A real person correcting their answer should never reach this,
    # so a rise here is either a script or a cap that is too tight to launch behind.
    "rate_limited",
    # The write itself raised. The only outcome that is unambiguously our fault.
    "error",
)

invite_request_submissions_total = Counter(
    "pymix_invite_request_submissions_total",
    "Invite-form submissions by what became of them. `created` is the only outcome "
    "that adds a row; every other outcome is a person who pressed the button and is "
    "not in the table.",
    ["outcome"],
    registry=REGISTRY,
)
for _outcome in INVITE_SUBMISSION_OUTCOMES:
    invite_request_submissions_total.labels(outcome=_outcome)

# `created` / `resumed` / `invalid_credentials` / `error`.
user_logins_total = Counter(
    "pymix_user_logins_total",
    "Login attempts by outcome. `resumed` is a client presenting a session_id that "
    "was still valid, so it never reached the password check.",
    ["outcome"],
    registry=REGISTRY,
)
for _outcome in ("created", "resumed", "invalid_credentials", "error"):
    user_logins_total.labels(outcome=_outcome)

# `created` / `rejected` / `error`. `rejected` is the deliberate refusals -- a bad or
# spent invite token, or the user cap -- as opposed to something breaking.
user_signups_total = Counter(
    "pymix_user_signups_total",
    "Account-creation attempts by outcome. The last hop of the funnel: an invite that "
    "was sent, opened, and redeemed.",
    ["outcome"],
    registry=REGISTRY,
)
for _outcome in ("created", "rejected", "error"):
    user_signups_total.labels(outcome=_outcome)


def observe_invite_submission(outcome: str) -> None:
    """Record one invite-form submission.

    Anything outside INVITE_SUBMISSION_OUTCOMES is dropped rather than recorded. The
    bounded label set is the whole cardinality defence on a route the open internet
    can reach, and it is worth nothing if a caller can widen it by passing a new
    string; losing one sample is much cheaper than minting a permanent series.
    """
    if outcome not in INVITE_SUBMISSION_OUTCOMES:
        logger.error("metrics: refusing unknown invite submission outcome %r", outcome)
        return
    invite_request_submissions_total.labels(outcome=outcome).inc()


def observe_login(outcome: str) -> None:
    user_logins_total.labels(outcome=outcome).inc()


def observe_signup(outcome: str) -> None:
    user_signups_total.labels(outcome=outcome).inc()


# ---------------------------------------------------------------------------
# Engagement
# ---------------------------------------------------------------------------
#
# Per-user, and that is a deliberate cardinality decision rather than an oversight.
# `max_number_of_users` is 10, so one series per user is bounded by config, and during
# a beta "which of the ten is actually using it" is the question the whole exercise
# exists to answer -- an aggregate request rate cannot distinguish ten active testers
# from one active tester and nine who signed up and left.
#
# Two caveats worth knowing before reading a graph built on these:
#
#   * A deleted user's series persists until pymix restarts. The label set is
#     append-only in-process; nothing prunes it.
#   * These count *API* activity, not listening. Playback goes straight from the
#     client to the user's Navidrome and never touches pymix, so a user who streams
#     all day but never syncs looks idle here. `http_request_count` on the Navidrome
#     side of the dashboard (../subbox-workspace/docs/monitoring.md §6) is where
#     listening shows up; the two answer different questions.

user_requests_total = Counter(
    "pymix_user_requests_total",
    "Authenticated pymix requests per user. API activity only -- playback never "
    "reaches pymix, so read this beside Navidrome's own request count.",
    ["username"],
    registry=REGISTRY,
)

user_last_request_timestamp_seconds = Gauge(
    "pymix_user_last_request_timestamp_seconds",
    "Unix time of each user's most recent authenticated request. `time() - this` is "
    "how long a beta tester has been quiet.",
    ["username"],
    registry=REGISTRY,
)


def observe_user_request(username: str) -> None:
    """Record that `username` made an authenticated request.

    Called from `require_user`, which every user-scoped route resolves through, so it
    covers the whole authenticated API without a decorator on each handler.
    """
    user_requests_total.labels(username=username).inc()
    user_last_request_timestamp_seconds.labels(username=username).set(time.time())


# ---------------------------------------------------------------------------
# Bottlenecks
# ---------------------------------------------------------------------------
#
# `pymix_http_request_duration_seconds` times the *request*, which for the expensive
# routes is close to meaningless: an import returns a job id in milliseconds and then
# does ten minutes of work on a background task. The three families below time the
# work itself, at the three places it actually goes -- the job, the per-user Navidrome
# and Subsonic APIs, and beets.

# Jobs run in minutes, not seconds. Sharing _DURATION_BUCKETS would put almost every
# real import in the top two buckets and make the quantiles useless.
_JOB_BUCKETS = (
    1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0, float("inf"),
)

jobs_completed_total = Counter(
    "pymix_jobs_completed_total",
    "Jobs that reached a terminal state, by kind and outcome. Compare with "
    "`pymix_jobs{state=\"in_progress\"}`: jobs started that never appear here are the "
    "ones stuck forever, which is the failure mode the client cannot see.",
    ["job", "outcome"],
    registry=REGISTRY,
)

job_duration_seconds = Histogram(
    "pymix_job_duration_seconds",
    "Wall-clock time from job creation to completion, by kind and outcome. Measured "
    "in-process, so a job interrupted by a restart is never observed.",
    ["job", "outcome"],
    buckets=_JOB_BUCKETS,
    registry=REGISTRY,
)

dependency_request_duration_seconds = Histogram(
    "pymix_dependency_request_duration_seconds",
    "Time for one outbound HTTP call to a per-user container. This is what "
    "distinguishes 'pymix is slow' from 'this user's Navidrome is slow', which the "
    "request-level histogram alone cannot.",
    ["dependency", "method", "outcome"],
    buckets=_DURATION_BUCKETS,
    registry=REGISTRY,
)

beets_exec_duration_seconds = Histogram(
    "pymix_beets_exec_duration_seconds",
    "Time for one `docker exec` against a beets container, by beets subcommand. The "
    "single heaviest thing pymix does, and the first place to look when an import is "
    "slow.",
    ["command", "outcome"],
    buckets=_JOB_BUCKETS,
    registry=REGISTRY,
)

beets_write_lock_wait_seconds = Histogram(
    "pymix_beets_write_lock_wait_seconds",
    "Time a beets write spent blocked on another write to the same container. Time "
    "here is pure contention -- an import waiting on a watch-directory cycle, or on "
    "another import -- and it is invisible in every other metric.",
    buckets=_DURATION_BUCKETS,
    registry=REGISTRY,
)

# job_id -> (kind, start time). In-process, because JobRow carries no timestamp: there
# is nowhere in the database to read a duration from, and adding a column would need a
# migration for a number only the metrics want. The cost of keeping it here is that a
# job spanning a restart is never observed -- which is honest, since such a job is
# dead anyway (the background task died with the process; the row is left in_progress
# forever, and `pymix_jobs{state="in_progress"}` is what catches that).
_job_started_at: dict[str, tuple[str, float]] = {}
_job_started_lock = threading.Lock()

# A job that never completes leaks one entry. Bounded so a systematic failure to
# complete costs a fixed amount of memory rather than an unbounded one -- on a service
# already running against a 768m cgroup limit, an unbounded dict keyed by request is
# not a diagnostic, it is the next incident.
_MAX_TRACKED_JOBS = 512


def job_started(job_id: str, job: str) -> None:
    """Note when a job began, so `job_finished` can time it."""
    with _job_started_lock:
        if len(_job_started_at) >= _MAX_TRACKED_JOBS:
            oldest = min(_job_started_at, key=lambda k: _job_started_at[k][1])
            del _job_started_at[oldest]
        _job_started_at[job_id] = (job, time.monotonic())


def job_finished(job_id: str, result: bool) -> None:
    """Record a job reaching a terminal state, and how long it took.

    A job whose start was not seen (started before a restart, or created by a path
    that does not call `job_started`) is still *counted* under `job="unknown"` -- the
    completion is real and losing it would understate the failure rate -- but it is
    not timed, because there is no honest number to report.
    """
    with _job_started_lock:
        started = _job_started_at.pop(job_id, None)

    outcome = "succeeded" if result else "failed"
    if started is None:
        jobs_completed_total.labels(job="unknown", outcome=outcome).inc()
        return

    job, started_at = started
    jobs_completed_total.labels(job=job, outcome=outcome).inc()
    job_duration_seconds.labels(job=job, outcome=outcome).observe(
        time.monotonic() - started_at
    )


def observe_dependency_request(
    dependency: str, method: str, started_at: float, ok: bool
) -> None:
    """Record one outbound call. `dependency` is the client kind (`subsonic`,
    `navidrome`), never the host -- the host embeds a per-user port, which would mint
    a series per user for no extra information."""
    dependency_request_duration_seconds.labels(
        dependency=dependency, method=method, outcome="ok" if ok else "error"
    ).observe(time.monotonic() - started_at)


def observe_beets_exec(command: str, started_at: float, ok: bool) -> None:
    """Record one beets exec. `command` is the beets subcommand only (`import`,
    `list`, `stats`, ...), so the label set is the size of beets' command list rather
    than the number of distinct command lines pymix builds."""
    beets_exec_duration_seconds.labels(
        command=command, outcome="ok" if ok else "error"
    ).observe(time.monotonic() - started_at)


def beets_command_label(command) -> str:
    """The beets subcommand from a command list/string, for use as a label.

    Everything pymix runs is `beet <subcommand> ...` inside the container. Anything
    that does not look like that collapses to `<other>` rather than becoming its own
    series -- the label has to stay bounded even if a future call site builds a
    command this does not anticipate.
    """
    if isinstance(command, str):
        parts = command.split()
    else:
        parts = list(command)
    for part in parts:
        if part in ("beet", "beets"):
            continue
        if part.startswith("-"):
            continue
        return part
    return "<other>"


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

        try:
            sessions = self._db.count_sessions()
        except Exception:
            logger.exception("metrics: could not count sessions")
            return
        gauge = GaugeMetricFamily(
            "pymix_sessions_total",
            "Rows in session_table. There is at most one per user and nothing expires "
            "them, so this is 'users who have logged in and not logged out' -- a "
            "cumulative reach figure, not a concurrency one. Read "
            "pymix_user_last_request_timestamp_seconds for who is actually active.",
        )
        gauge.add_metric([], sessions)
        yield gauge

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

        # The two timings of the funnel's manual step. Fulfilment is a human reading
        # the table, so the queue has a latency that nothing else measures, and the
        # cost of not measuring it is somebody who watched the video, signed up, and
        # heard nothing for a fortnight.
        try:
            timings = self._db.invite_request_timings()
        except Exception:
            logger.exception("metrics: could not read invite request timings")
            return

        if timings.get("oldest_new_created_at"):
            age = GaugeMetricFamily(
                "pymix_invite_request_oldest_new_age_seconds",
                "How long the oldest unworked (`new`) invite request has been waiting. "
                "Absent when the queue is empty -- deliberately, since 0 would read as "
                "'answered instantly' rather than 'nothing to answer'.",
            )
            age.add_metric([], time.time() - timings["oldest_new_created_at"])
            yield age

        if timings.get("last_created_at"):
            last = GaugeMetricFamily(
                "pymix_invite_request_last_arrival_timestamp_seconds",
                "Unix time of the newest row in invite_request_table. Named "
                "`arrival` rather than `created` so it is not mistaken for "
                "prometheus_client's `_created` series on a counter. `time() - this` "
                "going up while the video is being shared is the signal that the form "
                "has broken rather than that interest has stopped -- it is the one "
                "alert here that catches a client-side failure pymix cannot see.",
            )
            last.add_metric([], timings["last_created_at"])
            yield last

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
