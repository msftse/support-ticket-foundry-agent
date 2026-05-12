# Copyright (c) Microsoft. All rights reserved.

"""OpenTelemetry tracing helpers for the support-ticket workflow.

Usage
-----
All executor nodes import ``wf_tracer`` and use it as a context manager:

    from pocs.support_ticket_foundry_workflow_retry_controller.tracing import wf_tracer, span_attrs

    with wf_tracer.start_span("executor.my_node", attrs) as span:
        ...

Attribute helpers keep attribute keys consistent across every node so KQL
queries in App Insights can filter on a single attribute name.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, Span, StatusCode

# Single tracer instance shared by the whole workflow package.
_tracer = trace.get_tracer("support_ticket_workflow", schema_url="https://opentelemetry.io/schemas/1.24.0")

# ── Attribute key constants ─────────────────────────────────────────────────
# Keeping these as string literals (not an enum) so they stay lightweight and
# importable without pulling in extra packages.

A_WORKFLOW   = "workflow.name"
A_EXECUTOR   = "workflow.executor"
A_TICKET_ID  = "ticket.id"
A_SEVERITY   = "ticket.severity"
A_TIER       = "ticket.customer_tier"
A_CATEGORY   = "ticket.category"
A_RETRY_PASS = "workflow.retry_pass"
A_RETRY_TGT  = "workflow.retry_targets"
A_ACTION     = "workflow.validator_action"
A_CONFIDENCE = "workflow.confidence"
A_CACHED     = "workflow.branch_cached"
A_SCORE      = "business.impact_score"
A_SLA        = "business.sla_minutes"
A_PAGE       = "business.should_page"
A_ERROR      = "workflow.error"


def span_attrs(**kwargs: Any) -> dict[str, Any]:
    """Return a flat dict of attributes, skipping None values."""
    return {k: v for k, v in kwargs.items() if v is not None}


@contextmanager
def start_executor_span(executor_name: str, attributes: dict[str, Any] | None = None) -> Iterator[Span]:
    """Start a span for a workflow executor node.

    The span is named ``executor.<executor_name>`` and tagged with
    ``workflow.executor`` so every node is trivially filterable in KQL:

        dependencies | where customDimensions["workflow.executor"] == "normalize_ticket"

    On exception the span status is set to ERROR and the exception is recorded
    before being re-raised so the caller's error handling is unaffected.
    """
    attrs: dict[str, Any] = {A_EXECUTOR: executor_name}
    if attributes:
        attrs.update(attributes)

    with _tracer.start_as_current_span(f"executor.{executor_name}", attributes=attrs) as span:
        try:
            yield span
        except Exception as exc:
            if not isinstance(span, NonRecordingSpan):
                span.set_status(StatusCode.ERROR, str(exc))
                span.record_exception(exc)
            raise


def set_ok(span: Span) -> None:
    """Mark a span as OK (call at the end of a successful handler)."""
    if not isinstance(span, NonRecordingSpan):
        span.set_status(StatusCode.OK)
