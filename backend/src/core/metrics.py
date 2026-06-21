"""
Prometheus metrics: auto-instrumented HTTP + custom gauges/counters/histograms.

`/metrics` endpoint вешается через Instrumentator.expose() в main.py.

Кастомные метрики:
- gym_posting_runs_active (gauge) — текущие активные run-ы
- gym_posting_xmlrpc_requests_total{outcome} (counter) — публикации по исходу
- gym_posting_xmlrpc_duration_seconds (histogram) — время одного XML-RPC поста
- gym_wp_credentials_valid (gauge) — валидных WP-credentials в пуле
- gym_proxies_active (gauge) — активных proxy в пуле

Gauge-ы (run_active / cred_valid / proxies_active) обновляются background-таской
TaskIQ scheduler-а; counter/histogram пишутся прямо из воркера постинга.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from prometheus_fastapi_instrumentator import Instrumentator

# ─── Custom metrics ──────────────────────────────────────────────────

gym_posting_runs_active = Gauge(
    "gym_posting_runs_active",
    "Number of posting_runs in active statuses (unpacking/queued/running/paused/scheduled)",
)

gym_posting_xmlrpc_requests_total = Counter(
    "gym_posting_xmlrpc_requests_total",
    "Total XML-RPC wp.newPost requests by outcome",
    ["outcome"],
)

gym_posting_xmlrpc_duration_seconds = Histogram(
    "gym_posting_xmlrpc_duration_seconds",
    "Duration of one XML-RPC wp.newPost call (seconds)",
    buckets=(0.5, 1, 2, 5, 10, 20, 30, 60, 120),
)

gym_wp_credentials_valid = Gauge(
    "gym_wp_credentials_valid",
    "Number of WP credentials with is_valid=true",
)

gym_proxies_active = Gauge(
    "gym_proxies_active",
    "Number of proxies with is_active=true and status=active",
)


# ─── Instrumentator wrapper ──────────────────────────────────────────


def setup_instrumentator(app) -> None:
    """
    Подключаем prometheus-fastapi-instrumentator: HTTP request count/latency
    автоматом + наш /admin/api/system/metrics.
    """
    instrumentator = Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        should_respect_env_var=False,
        should_instrument_requests_inprogress=True,
        excluded_handlers=["/health", "/ready", "/admin/api/system/metrics"],
        env_var_name="ENABLE_METRICS",
        inprogress_name="gym_http_requests_inprogress",
        inprogress_labels=True,
    )
    instrumentator.instrument(app).expose(
        app, endpoint="/admin/api/system/metrics", include_in_schema=False
    )


__all__ = [
    "CONTENT_TYPE_LATEST",
    "generate_latest",
    "gym_posting_runs_active",
    "gym_posting_xmlrpc_requests_total",
    "gym_posting_xmlrpc_duration_seconds",
    "gym_wp_credentials_valid",
    "gym_proxies_active",
    "setup_instrumentator",
]
