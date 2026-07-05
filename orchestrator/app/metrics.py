"""Prometheus metrics, served at GET /metrics (mounted in main.py).

Incremented from access_log.py (single choke point for flow/LLM accounting)
and from the concurrency guard in main.py. Scraped by the lab Prometheus
(job `orchestrator`); dashboard in monitoring/grafana/dashboards/.
"""

from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

from . import config

asgi_app = make_asgi_app()

FLOWS_TOTAL = Counter(
    "acm_flows_total", "Orchestrator requests by flow/channel/outcome",
    ["flow_id", "channel", "status"])
FLOW_DURATION = Histogram(
    "acm_flow_duration_seconds", "End-to-end request duration",
    ["flow_id"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600))
FLOWS_INFLIGHT = Gauge(
    "acm_flows_inflight", "Requests currently being processed")
FLOWS_CAPACITY = Gauge(
    "acm_flows_capacity", "Concurrency cap (MAX_CONCURRENT_FLOWS)")
FLOWS_REJECTED = Counter(
    "acm_flows_rejected_total", "Requests shed before running",
    ["reason", "channel"])

LLM_CALLS = Counter(
    "acm_llm_calls_total", "LLM API calls", ["model", "kind", "ok"])
LLM_TOKENS = Counter(
    "acm_llm_tokens_total", "LLM tokens", ["model", "direction"])
LLM_DURATION = Histogram(
    "acm_llm_call_duration_seconds", "LLM call duration", ["model"],
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 120, 300))

FLOWS_CAPACITY.set(config.MAX_CONCURRENT_FLOWS)
