# Copyright (c) Microsoft. All rights reserved.

from __future__ import annotations

import json
import os
from dataclasses import fields, is_dataclass, replace
from typing import Any

from agent_framework import Agent, Case, Default, Executor, Message, WorkflowBuilder, WorkflowContext, handler
from agent_framework.foundry import FoundryChatClient
from azure.identity.aio import DefaultAzureCredential
from dotenv import load_dotenv
from middleware import (
    AgentResponseDiagnosticsMiddleware,
    AgentTelemetryRegistry,
    ToolCallDiagnosticsMiddleware,
)
from models import (
    AggregatedIncidentContext,
    BranchDiagnostics,
    BusinessScoreOutput,
    ContextAssessmentModel,
    ContextBranchOutput,
    DocsAssessmentModel,
    DocsBranchOutput,
    KnowledgeAssessmentModel,
    KnowledgeBranchOutput,
    NormalizedTicket,
    ResolutionDraftModel,
    ResolutionPackage,
    RouteDecision,
    SupportTicketCase,
    TriageAssessmentModel,
    TriageBranchOutput,
    ValidationAssessmentModel,
    ValidationPackage,
    ValidatorPolicyDecision,
    RetryInstructionModel,
    RetryTarget,
)
from tools import (
    fetch_asset_snapshot,
    get_service_health,
    lookup_asset_record,
    lookup_recent_cases,
    lookup_runbook,
)
from pydantic import BaseModel
from typing_extensions import Never
from tracing import (
    A_ACTION,
    A_CACHED,
    A_CATEGORY,
    A_CONFIDENCE,
    A_PAGE,
    A_RETRY_PASS,
    A_RETRY_TGT,
    A_SCORE,
    A_SEVERITY,
    A_SLA,
    A_TICKET_ID,
    A_TIER,
    set_ok,
    span_attrs,
    start_executor_span,
)

load_dotenv(override=False)

TICKET_STATE_KEY = "support_ticket.case"
TRIAGE_STATE_KEY = "support_ticket.triage"
KNOWLEDGE_STATE_KEY = "support_ticket.knowledge"
CONTEXT_STATE_KEY = "support_ticket.context"
DOCS_STATE_KEY = "support_ticket.docs"
BUSINESS_STATE_KEY = "support_ticket.business"
AGGREGATE_STATE_KEY = "support_ticket.aggregate"
RESOLUTION_STATE_KEY = "support_ticket.resolution"
VALIDATION_STATE_KEY = "support_ticket.validation"
RETRY_STATE_KEY = "support_ticket.retry_counts"
RETRY_DECISION_KEY = "support_ticket.retry_decision"


def _action_is(expected_action: str):
    def predicate(decision: Any) -> bool:
        return isinstance(decision, ValidatorPolicyDecision) and decision.action == expected_action

    return predicate


def _should_retry_branch(target: str, decision: ValidatorPolicyDecision) -> bool:
    """Return True if this branch should re-run based on the policy decision.

    A branch re-runs when:
    - Its name is an explicit retry target, OR
    - 'prepare' is the only target (full context refresh → all branches revisit conclusions).
    """
    specific_branch_targets = {t for t in decision.retry_targets if t in {"triage", "knowledge", "context"}}
    if target in decision.retry_targets:
        return True
    if "prepare" in decision.retry_targets and not specific_branch_targets:
        return True
    return False


def _get_credential():
    return DefaultAzureCredential(
        exclude_environment_credential=True,
        exclude_shared_token_cache_credential=True,
        exclude_visual_studio_code_credential=True,
        exclude_powershell_credential=True,
        exclude_developer_cli_credential=True,
        exclude_interactive_browser_credential=True,
    )


def _make_foundry_client() -> FoundryChatClient:
    project_endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT") or os.getenv("AZURE_AI_PROJECT_ENDPOINT")
    model = os.getenv("FOUNDRY_MODEL") or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")

    if not project_endpoint or not model:
        raise RuntimeError(
            "Set FOUNDRY_PROJECT_ENDPOINT and FOUNDRY_MODEL, or use the workspace defaults "
            "AZURE_AI_PROJECT_ENDPOINT and AZURE_AI_MODEL_DEPLOYMENT_NAME."
        )

    return FoundryChatClient(
        project_endpoint=project_endpoint,
        model=model,
        credential=_get_credential(),
    )


def _to_serializable(payload: Any) -> Any:
    if hasattr(payload, "model_dump"):
        return payload.model_dump(mode="json")
    if is_dataclass(payload):
        return {field.name: _to_serializable(getattr(payload, field.name)) for field in fields(payload)}
    if isinstance(payload, dict):
        return {key: _to_serializable(value) for key, value in payload.items()}
    if isinstance(payload, (list, tuple, set)):
        return [_to_serializable(item) for item in payload]
    return payload


def _to_json(payload: Any) -> str:
    return json.dumps(_to_serializable(payload), indent=2, sort_keys=True)


def _extract_first_json_object(text: str) -> str | None:
    """Extract the first valid JSON object from a possibly noisy model response."""
    for start, ch in enumerate(text):
        if ch != "{":
            continue

        depth = 0
        in_string = False
        escaped = False
        for end in range(start, len(text)):
            c = text[end]

            if in_string:
                if escaped:
                    escaped = False
                elif c == "\\":
                    escaped = True
                elif c == '"':
                    in_string = False
                continue

            if c == '"':
                in_string = True
                continue

            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : end + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except Exception:
                        break
    return None


def _parse_structured_json(text: str, model_type: type[BaseModel]) -> BaseModel:
    """Parse strict JSON first, then recover by extracting first JSON object."""
    try:
        return model_type.model_validate_json(text)
    except Exception as original_error:
        candidate = _extract_first_json_object(text)
        if candidate is None:
            raise original_error
        return model_type.model_validate_json(candidate)


class TriageService:
    """Wraps the triage agent and emits normalized branch output."""

    def __init__(self, agent: Agent, diagnostics: AgentTelemetryRegistry, agent_name: str) -> None:
        self._agent = agent
        self._diagnostics = diagnostics
        self._agent_name = agent_name

    async def run(self, ticket: NormalizedTicket) -> TriageBranchOutput:
        prompt = (
            "You are the first-line IT incident triage analyst. Review the ticket and return a precise triage "
            "decision as JSON. Prefer the most plausible owning team and be explicit about missing evidence.\n\n"
            f"Ticket:\n{_to_json(ticket)}"
        )
        result = await self._agent.run(prompt)
        parsed = _parse_structured_json(result.text, TriageAssessmentModel)
        return TriageBranchOutput(
            confirmed_priority=parsed.confirmed_priority,
            suspected_team=parsed.suspected_team,
            confidence=parsed.confidence,
            missing_information=parsed.missing_information,
            concise_reason=parsed.concise_reason,
            diagnostics=self._diagnostics.snapshot(self._agent_name),
        )


class KnowledgeService:
    """Wraps the runbook and service-health specialist agent."""

    def __init__(self, agent: Agent, diagnostics: AgentTelemetryRegistry, agent_name: str) -> None:
        self._agent = agent
        self._diagnostics = diagnostics
        self._agent_name = agent_name

    async def run(self, ticket: NormalizedTicket) -> KnowledgeBranchOutput:
        prompt = (
            "You are an IT operations knowledge specialist. Use the available tools before answering. Return JSON "
            "with probable_cause, recommended_runbook, evidence, confidence, and should_retry.\n\n"
            f"Ticket:\n{_to_json(ticket)}"
        )
        result = await self._agent.run(prompt)
        parsed = _parse_structured_json(result.text, KnowledgeAssessmentModel)
        diagnostics = self._diagnostics.snapshot(self._agent_name)
        if not diagnostics.tool_calls:
            diagnostics.notes.append("knowledge_response_without_tool_call")
        return KnowledgeBranchOutput(
            probable_cause=parsed.probable_cause,
            recommended_runbook=parsed.recommended_runbook,
            evidence=parsed.evidence,
            confidence=parsed.confidence,
            should_retry=parsed.should_retry,
            diagnostics=diagnostics,
        )


class ContextService:
    """Wraps the history and asset-context specialist agent."""

    def __init__(self, agent: Agent, diagnostics: AgentTelemetryRegistry, agent_name: str) -> None:
        self._agent = agent
        self._diagnostics = diagnostics
        self._agent_name = agent_name

    async def run(self, ticket: NormalizedTicket) -> ContextBranchOutput:
        prompt = (
            "You are an IT support context analyst. Use the available tools before answering. Return JSON with "
            "asset_summary, recent_related_cases, escalation_notes, confidence, and should_retry.\n\n"
            f"Ticket:\n{_to_json(ticket)}"
        )
        result = await self._agent.run(prompt)
        parsed = _parse_structured_json(result.text, ContextAssessmentModel)
        diagnostics = self._diagnostics.snapshot(self._agent_name)
        if not diagnostics.tool_calls:
            diagnostics.notes.append("context_response_without_tool_call")
        return ContextBranchOutput(
            asset_summary=parsed.asset_summary,
            recent_related_cases=parsed.recent_related_cases,
            escalation_notes=parsed.escalation_notes,
            confidence=parsed.confidence,
            should_retry=parsed.should_retry,
            diagnostics=diagnostics,
        )


class DocsService:
    """Wraps a Microsoft Learn MCP-backed agent for product guidance."""

    def __init__(self, agent: Agent, diagnostics: AgentTelemetryRegistry, agent_name: str) -> None:
        self._agent = agent
        self._diagnostics = diagnostics
        self._agent_name = agent_name

    async def run(self, ticket: NormalizedTicket) -> DocsBranchOutput:
        prompt = (
            "You are a Microsoft product guidance specialist for IT support. Use the available Microsoft Learn MCP "
            "tool before answering. Return JSON with recommended_articles, troubleshooting_notes, confidence, and "
            "should_retry. Keep the articles focused on the ticket's Microsoft product area.\n\n"
            f"Ticket:\n{_to_json(ticket)}"
        )
        diagnostics = self._diagnostics.snapshot(self._agent_name)
        try:
            result = await self._agent.run(prompt)
            parsed = _parse_structured_json(result.text, DocsAssessmentModel)
        except Exception as exc:
            # Fail-open so one tool/LLM formatting miss does not fail the full workflow.
            diagnostics.notes.append(f"docs_parse_error:{type(exc).__name__}")
            if not diagnostics.tool_calls:
                diagnostics.notes.append("docs_response_without_tool_call")
            return DocsBranchOutput(
                recommended_articles=[],
                troubleshooting_notes=[
                    "Microsoft guidance unavailable for this run; continue with triage and knowledge outputs."
                ],
                confidence=0.0,
                should_retry=False,
                diagnostics=diagnostics,
            )

        if not diagnostics.tool_calls:
            diagnostics.notes.append("docs_response_without_tool_call")
        return DocsBranchOutput(
            recommended_articles=parsed.recommended_articles,
            troubleshooting_notes=parsed.troubleshooting_notes,
            confidence=parsed.confidence,
            should_retry=parsed.should_retry,
            diagnostics=diagnostics,
        )


class ResolutionService:
    """Wraps the resolution summarizer agent."""

    def __init__(self, agent: Agent, diagnostics: AgentTelemetryRegistry, agent_name: str) -> None:
        self._agent = agent
        self._diagnostics = diagnostics
        self._agent_name = agent_name

    async def run(self, incident: AggregatedIncidentContext) -> ResolutionPackage:
        prompt = (
            "You are a support resolution planner. Create an internal summary, customer-facing summary, next actions, "
            "closure recommendation, and confidence as JSON.\n\n"
            f"Incident:\n{_to_json(incident)}"
        )
        result = await self._agent.run(prompt)
        parsed = _parse_structured_json(result.text, ResolutionDraftModel)
        return ResolutionPackage(
            draft=parsed,
            diagnostics=self._diagnostics.snapshot(self._agent_name),
        )


class ValidationService:
    """Wraps the validator agent that reviews the resolution draft."""

    def __init__(self, agent: Agent, diagnostics: AgentTelemetryRegistry, agent_name: str) -> None:
        self._agent = agent
        self._diagnostics = diagnostics
        self._agent_name = agent_name

    async def run(self, payload: dict[str, Any]) -> ValidationPackage:
        prompt = (
            "You are the final workflow validator. Review the incident context, retry history, and resolution draft. "
            "Return JSON with verdict, confidence, retry_targets, retry_instructions, concerns, rationale, and "
            "requires_human_follow_up. Each retry_instructions entry must include the target node and a concrete "
            "reason that cites the middleware-enriched diagnostics, such as missing tool calls, low-confidence "
            "evidence, or model_requested_more_context notes. Recommend retry only for prepare, triage, knowledge, "
            "or context when evidence is incomplete. You may select one target or multiple targets. If no retry is "
            "needed, return an empty retry_instructions list.\n\n"
            f"Validator payload:\n{json.dumps(payload, indent=2, sort_keys=True)}"
        )
        result = await self._agent.run(prompt)
        parsed = _parse_structured_json(result.text, ValidationAssessmentModel)
        return ValidationPackage(
            assessment=parsed,
            diagnostics=self._diagnostics.snapshot(self._agent_name),
        )


class InitializeTicket(Executor):
    """Creates normalized workflow state and resets retry bookkeeping."""

    def __init__(self, diagnostics: AgentTelemetryRegistry, id: str) -> None:
        super().__init__(id=id)
        self._diagnostics = diagnostics

    @staticmethod
    def _coerce_request(request: SupportTicketCase | Message | list[Message]) -> SupportTicketCase:
        if isinstance(request, SupportTicketCase):
            return request

        if isinstance(request, Message):
            request = [request]

        user_text = ""
        for msg in reversed(request):
            if getattr(msg, "role", None) != "user":
                continue

            content = getattr(msg, "contents", None)
            if content is None:
                content = getattr(msg, "content", "")

            if isinstance(content, str):
                user_text = content.strip()
                break

            if isinstance(content, list):
                parts: list[str] = []
                for item in content:
                    if isinstance(item, str) and item.strip():
                        parts.append(item.strip())
                        continue

                    text_value = getattr(item, "text", None)
                    if isinstance(text_value, str) and text_value.strip():
                        parts.append(text_value.strip())
                        continue

                    if isinstance(item, dict):
                        raw_text = item.get("text")
                        if isinstance(raw_text, str) and raw_text.strip():
                            parts.append(raw_text.strip())

                user_text = "\n".join(parts).strip()
                if user_text:
                    break

        if user_text:
            try:
                parsed = json.loads(user_text)
                if isinstance(parsed, dict):
                    return SupportTicketCase.model_validate(parsed)
            except Exception:
                pass

            return SupportTicketCase(summary=user_text)

        return SupportTicketCase()

    @handler
    async def handle(self, request: SupportTicketCase | Message | list[Message], ctx: WorkflowContext[NormalizedTicket]) -> None:
        request_case = self._coerce_request(request)
        attrs = span_attrs(**{
            A_TICKET_ID: request_case.ticket_id,
            A_SEVERITY: request_case.severity,
            A_TIER: request_case.customer_tier,
            A_CATEGORY: request_case.category,
        })
        with start_executor_span("initialize_ticket", attrs) as span:
            self._diagnostics.reset()
            normalized = NormalizedTicket(
                ticket_id=request_case.ticket_id,
                customer_name=request_case.customer_name,
                customer_tier=request_case.customer_tier,
                severity=request_case.severity,
                category=request_case.category,
                service_name=request_case.service_name,
                region=request_case.region,
                asset_id=request_case.asset_id,
                summary=request_case.summary,
                recent_change=request_case.recent_change,
                business_deadline_hours=request_case.business_deadline_hours,
                notes=request_case.notes or "No extra intake notes provided.",
                demo_mode=request_case.demo_mode,
            )
            ctx.set_state(RETRY_STATE_KEY, {"triage": 0, "knowledge": 0, "context": 0})
            ctx.set_state(TICKET_STATE_KEY, normalized)
            await ctx.send_message(normalized)
            set_ok(span)


class RouteTicket(Executor):
    """Uses explicit if/elif/else logic before the workflow fans out."""

    @handler
    async def handle(self, ticket: NormalizedTicket, ctx: WorkflowContext[RouteDecision]) -> None:
        attrs = span_attrs(**{A_TICKET_ID: ticket.ticket_id, A_SEVERITY: ticket.severity})
        with start_executor_span("route_ticket", attrs) as span:
            if ticket.asset_id:
                decision = RouteDecision(path="asset_enrichment", reason="Asset identifier is present, so fetch deterministic asset data.")
            elif ticket.severity in {"high", "critical"}:
                decision = RouteDecision(
                    path="direct",
                    reason="Proceed directly so high-severity tickets are not blocked by missing asset metadata.",
                )
            else:
                decision = RouteDecision(path="direct", reason="No asset identifier was provided, so continue with a generic context.")

            span.set_attribute("route.path", decision.path)
            ticket.route_reason = decision.reason
            ctx.set_state(TICKET_STATE_KEY, ticket)
            await ctx.send_message(decision)
            set_ok(span)


class PrepareContext(Executor):
    """Unified preparation stage — serves both the initial pass and validator-requested retries.

    Initial pass (RouteDecision from route_ticket):
      - Clears the retry-decision sentinel so all downstream branches run unconditionally.
      - Always fetches a fresh asset snapshot.

    Retry pass (ValidatorPolicyDecision from the switch-case):
      - Persists the decision in state so the analysis branches know which of them
        should re-run vs. forward their cached results (smart passthrough).
      - Only refreshes the asset snapshot when 'prepare' is among the retry targets;
        node-specific retries (e.g. 'knowledge' only) skip the redundant fetch.

    In both cases the same NormalizedTicket is emitted, triggering the identical
    fan-out to triage / knowledge / context / docs / business — no separate retry nodes needed.
    """

    @handler
    async def handle(self, decision: RouteDecision | ValidatorPolicyDecision, ctx: WorkflowContext[NormalizedTicket]) -> None:
        ticket: NormalizedTicket = ctx.get_state(TICKET_STATE_KEY)
        is_retry = isinstance(decision, ValidatorPolicyDecision)
        retry_targets = decision.retry_targets if is_retry else []
        attrs = span_attrs(**{
            A_TICKET_ID: ticket.ticket_id,
            A_RETRY_PASS: is_retry,
            A_RETRY_TGT: ",".join(retry_targets) if retry_targets else None,
        })
        with start_executor_span("prepare_context", attrs) as span:
            if isinstance(decision, RouteDecision):
                ctx.set_state(RETRY_DECISION_KEY, None)
                route_note = "Prepared with asset-enrichment path." if decision.path == "asset_enrichment" else "Prepared with direct path."
                refresh_asset = True
            else:
                ctx.set_state(RETRY_DECISION_KEY, decision)
                route_note = f"Prepared during validator-requested retry. Targets={decision.retry_targets}"
                refresh_asset = "prepare" in decision.retry_targets
            enriched = replace(
                ticket,
                route_reason=f"{ticket.route_reason} | {route_note}",
                asset_snapshot=fetch_asset_snapshot(ticket.asset_id, ticket.service_name, ticket.customer_name) if refresh_asset else ticket.asset_snapshot,
            )
            span.set_attribute("prepare.refreshed_asset", refresh_asset)
            ctx.set_state(TICKET_STATE_KEY, enriched)
            await ctx.send_message(enriched)
            set_ok(span)


class TriageBranch(Executor):
    """Triage branch — runs on every initial pass; on retry, only re-runs when 'triage' is targeted."""

    def __init__(self, service: TriageService, id: str) -> None:
        super().__init__(id=id)
        self._service = service

    @handler
    async def handle(self, ticket: NormalizedTicket, ctx: WorkflowContext[TriageBranchOutput]) -> None:
        retry_decision: ValidatorPolicyDecision | None = ctx.get_state(RETRY_DECISION_KEY)
        is_cached = retry_decision is not None and not _should_retry_branch("triage", retry_decision)
        attrs = span_attrs(**{A_TICKET_ID: ticket.ticket_id, A_CACHED: is_cached, A_RETRY_PASS: retry_decision is not None})
        with start_executor_span("triage_branch", attrs) as span:
            if is_cached:
                await ctx.send_message(ctx.get_state(TRIAGE_STATE_KEY))
                set_ok(span)
                return
            output = await self._service.run(ticket)
            if retry_decision is not None:
                reason = retry_decision.retry_reasons.get("triage") or retry_decision.retry_reasons.get("prepare", "unspecified")
                output.diagnostics.notes.append(f"retry_reason:{reason}")
            span.set_attribute(A_CONFIDENCE, output.confidence)
            ctx.set_state(TRIAGE_STATE_KEY, output)
            await ctx.send_message(output)
            set_ok(span)


class KnowledgeBranch(Executor):
    """Knowledge branch — runs on every initial pass; on retry, only re-runs when 'knowledge' is targeted."""

    def __init__(self, service: KnowledgeService, id: str) -> None:
        super().__init__(id=id)
        self._service = service

    @handler
    async def handle(self, ticket: NormalizedTicket, ctx: WorkflowContext[KnowledgeBranchOutput]) -> None:
        retry_decision: ValidatorPolicyDecision | None = ctx.get_state(RETRY_DECISION_KEY)
        is_cached = retry_decision is not None and not _should_retry_branch("knowledge", retry_decision)
        attrs = span_attrs(**{A_TICKET_ID: ticket.ticket_id, A_CACHED: is_cached, A_RETRY_PASS: retry_decision is not None})
        with start_executor_span("knowledge_branch", attrs) as span:
            if is_cached:
                await ctx.send_message(ctx.get_state(KNOWLEDGE_STATE_KEY))
                set_ok(span)
                return
            output = await self._service.run(ticket)
            if retry_decision is not None:
                reason = retry_decision.retry_reasons.get("knowledge") or retry_decision.retry_reasons.get("prepare", "unspecified")
                output.diagnostics.notes.append(f"retry_reason:{reason}")
            span.set_attribute(A_CONFIDENCE, output.confidence)
            ctx.set_state(KNOWLEDGE_STATE_KEY, output)
            await ctx.send_message(output)
            set_ok(span)


class ContextBranch(Executor):
    """Context branch — runs on every initial pass; on retry, only re-runs when 'context' is targeted."""

    def __init__(self, service: ContextService, id: str) -> None:
        super().__init__(id=id)
        self._service = service

    @handler
    async def handle(self, ticket: NormalizedTicket, ctx: WorkflowContext[ContextBranchOutput]) -> None:
        retry_decision: ValidatorPolicyDecision | None = ctx.get_state(RETRY_DECISION_KEY)
        is_cached = retry_decision is not None and not _should_retry_branch("context", retry_decision)
        attrs = span_attrs(**{A_TICKET_ID: ticket.ticket_id, A_CACHED: is_cached, A_RETRY_PASS: retry_decision is not None})
        with start_executor_span("context_branch", attrs) as span:
            if is_cached:
                await ctx.send_message(ctx.get_state(CONTEXT_STATE_KEY))
                set_ok(span)
                return
            output = await self._service.run(ticket)
            if retry_decision is not None:
                reason = retry_decision.retry_reasons.get("context") or retry_decision.retry_reasons.get("prepare", "unspecified")
                output.diagnostics.notes.append(f"retry_reason:{reason}")
            span.set_attribute(A_CONFIDENCE, output.confidence)
            ctx.set_state(CONTEXT_STATE_KEY, output)
            await ctx.send_message(output)
            set_ok(span)


class DocsBranch(Executor):
    """Microsoft Learn branch backed by a hosted MCP tool.

    This branch gathers product-specific Microsoft guidance during the initial pass.
    On targeted retries unrelated to full preparation, the cached result is forwarded.
    """

    def __init__(self, service: DocsService, id: str) -> None:
        super().__init__(id=id)
        self._service = service

    @handler
    async def handle(self, ticket: NormalizedTicket, ctx: WorkflowContext[DocsBranchOutput]) -> None:
        retry_decision: ValidatorPolicyDecision | None = ctx.get_state(RETRY_DECISION_KEY)
        is_cached = retry_decision is not None and "prepare" not in retry_decision.retry_targets
        attrs = span_attrs(**{A_TICKET_ID: ticket.ticket_id, A_CACHED: is_cached, A_RETRY_PASS: retry_decision is not None})
        with start_executor_span("docs_branch", attrs) as span:
            if is_cached:
                await ctx.send_message(ctx.get_state(DOCS_STATE_KEY))
                set_ok(span)
                return
            output = await self._service.run(ticket)
            span.set_attribute(A_CONFIDENCE, output.confidence)
            ctx.set_state(DOCS_STATE_KEY, output)
            await ctx.send_message(output)
            set_ok(span)


class BusinessImpactScorer(Executor):
    """Business logic branch — recomputes SLA/impact on initial pass and when 'prepare' is retried.

    Business scores only change when ticket metadata is refreshed (the 'prepare' retry target).
    On any other retry pass (e.g. 'knowledge' only) the cached score is forwarded so the
    fan-in barrier is always satisfied without redundant computation.
    """

    @handler
    async def handle(self, ticket: NormalizedTicket, ctx: WorkflowContext[BusinessScoreOutput]) -> None:
        retry_decision: ValidatorPolicyDecision | None = ctx.get_state(RETRY_DECISION_KEY)
        is_cached = retry_decision is not None and "prepare" not in retry_decision.retry_targets
        attrs = span_attrs(**{A_TICKET_ID: ticket.ticket_id, A_CACHED: is_cached, A_SEVERITY: ticket.severity, A_TIER: ticket.customer_tier})
        with start_executor_span("business_impact_branch", attrs) as span:
            if is_cached:
                await ctx.send_message(ctx.get_state(BUSINESS_STATE_KEY))
                set_ok(span)
                return
            score = 35
            score += {"low": 5, "medium": 15, "high": 30, "critical": 45}[ticket.severity]
            score += {"standard": 0, "priority": 10, "executive": 20}[ticket.customer_tier]
            if ticket.recent_change:
                score += 10
            if ticket.business_deadline_hours <= 8:
                score += 10

            sla_minutes = {"low": 240, "medium": 120, "high": 60, "critical": 30}[ticket.severity]
            should_page = ticket.severity in {"high", "critical"} and ticket.service_name in {"Endpoint Manager", "Corporate VPN"}
            business = BusinessScoreOutput(
                impact_score=min(score, 100),
                sla_minutes=sla_minutes,
                should_page_on_call=should_page,
                business_reason=(
                    f"Tier={ticket.customer_tier}, severity={ticket.severity}, recent_change={ticket.recent_change}, "
                    f"deadline_hours={ticket.business_deadline_hours}"
                ),
            )
            span.set_attribute(A_SCORE, business.impact_score)
            span.set_attribute(A_SLA, business.sla_minutes)
            span.set_attribute(A_PAGE, business.should_page_on_call)
            ctx.set_state(BUSINESS_STATE_KEY, business)
            await ctx.send_message(business)
            set_ok(span)


class AggregateIncident(Executor):
    """Merges branch outputs and persists a single aggregate object."""

    @handler
    async def handle(
        self,
        results: list[TriageBranchOutput | KnowledgeBranchOutput | ContextBranchOutput | DocsBranchOutput | BusinessScoreOutput],
        ctx: WorkflowContext[AggregatedIncidentContext],
    ) -> None:
        ticket = ctx.get_state(TICKET_STATE_KEY)
        attrs = span_attrs(**{A_TICKET_ID: ticket.ticket_id if ticket else None})
        with start_executor_span("aggregate_incident", attrs) as span:
            triage = ctx.get_state(TRIAGE_STATE_KEY)
            knowledge = ctx.get_state(KNOWLEDGE_STATE_KEY)
            context_result = ctx.get_state(CONTEXT_STATE_KEY)
            docs_result = ctx.get_state(DOCS_STATE_KEY)
            business = ctx.get_state(BUSINESS_STATE_KEY)

            for result in results:
                if isinstance(result, TriageBranchOutput):
                    triage = result
                elif isinstance(result, KnowledgeBranchOutput):
                    knowledge = result
                elif isinstance(result, ContextBranchOutput):
                    context_result = result
                elif isinstance(result, DocsBranchOutput):
                    docs_result = result
                elif isinstance(result, BusinessScoreOutput):
                    business = result

            aggregate = AggregatedIncidentContext(
                ticket=ticket,
                triage=triage,
                knowledge=knowledge,
                context=context_result,
                docs=docs_result,
                business=business,
                retry_history=ctx.get_state(RETRY_STATE_KEY),
            )
            ctx.set_state(AGGREGATE_STATE_KEY, aggregate)
            await ctx.send_message(aggregate)
            set_ok(span)


class ResolutionBranch(Executor):
    """Creates a structured incident summary and closure draft."""

    def __init__(self, service: ResolutionService, id: str) -> None:
        super().__init__(id=id)
        self._service = service

    @handler
    async def handle(self, aggregate: AggregatedIncidentContext, ctx: WorkflowContext[ResolutionPackage]) -> None:
        attrs = span_attrs(**{A_TICKET_ID: aggregate.ticket.ticket_id if aggregate.ticket else None})
        with start_executor_span("resolution_branch", attrs) as span:
            output = await self._service.run(aggregate)
            span.set_attribute(A_CONFIDENCE, output.draft.confidence)
            ctx.set_state(RESOLUTION_STATE_KEY, output)
            await ctx.send_message(output)
            set_ok(span)


class ValidatorBranch(Executor):
    """Runs the LLM validator and emits structured assessment only.

    The retry/close/error policy is intentionally handled in RetryController so
    workflow routing remains deterministic and fully visible in executor logic.
    """

    def __init__(self, service: ValidationService, id: str) -> None:
        super().__init__(id=id)
        self._service = service

    @handler
    async def handle(self, resolution: ResolutionPackage, ctx: WorkflowContext[ValidationPackage]) -> None:
        aggregate: AggregatedIncidentContext = ctx.get_state(AGGREGATE_STATE_KEY)
        retry_counts: dict[str, int] = dict(ctx.get_state(RETRY_STATE_KEY))
        attrs = span_attrs(**{A_TICKET_ID: aggregate.ticket.ticket_id if aggregate.ticket else None})
        with start_executor_span("validator_branch", attrs) as span:
            payload = {
                "aggregate": json.loads(_to_json(aggregate)),
                "resolution": json.loads(_to_json(resolution)),
                "retry_history": retry_counts,
            }
            validation_package = await self._service.run(payload)
            ctx.set_state(VALIDATION_STATE_KEY, validation_package)
            span.set_attribute(A_CONFIDENCE, validation_package.assessment.confidence)
            set_ok(span)
            await ctx.send_message(validation_package)


class RetryController(Executor):
    """Deterministic post-validator policy node.

    Reads validator assessment + workflow context, applies retry/close/error
    policy, persists retry counters, and emits ValidatorPolicyDecision for graph
    switch routing.
    """

    _CLOSE_CONFIDENCE = 0.8
    _MIN_CONFIDENCE = 0.6
    _HIGH_IMPACT_SCORE = 90

    def __init__(self, id: str) -> None:
        super().__init__(id=id)
        self._max_retries = max(1, int(os.getenv("SUPPORT_WORKFLOW_MAX_RETRIES", "3")))

    def _build_retry_instructions(
        self,
        assessment: ValidationAssessmentModel,
        aggregate: AggregatedIncidentContext,
        retry_counts: dict[str, int],
    ) -> list[RetryInstructionModel]:
        instructions: list[RetryInstructionModel] = list(assessment.retry_instructions)

        if not instructions:
            for target in assessment.retry_targets:
                instructions.append(
                    RetryInstructionModel(
                        target=target,
                        reason="Validator flagged this node but did not supply a detailed reason.",
                    )
                )

        if aggregate.ticket.demo_mode == "force_retry_knowledge" and retry_counts.get("knowledge", 0) == 0:
            instructions.append(
                RetryInstructionModel(
                    target="knowledge",
                    reason="Demo mode forced a knowledge retry to illustrate targeted branch re-runs.",
                )
            )

        if not instructions and assessment.verdict == "needs_retry":
            instructions.append(
                RetryInstructionModel(
                    target="prepare",
                    reason="Validator requested retry but gave no specific targets; rerun preparation to refresh downstream context.",
                )
            )

        deduped: dict[RetryTarget, str] = {}
        for inst in instructions:
            if inst.target not in deduped:
                deduped[inst.target] = inst.reason

        return [RetryInstructionModel(target=target, reason=reason) for target, reason in deduped.items()]

    def _derive_decision(
        self,
        assessment: ValidationAssessmentModel,
        aggregate: AggregatedIncidentContext,
        retry_counts: dict[str, int],
    ) -> ValidatorPolicyDecision:
        retry_instructions = self._build_retry_instructions(assessment, aggregate, retry_counts)
        retry_targets = [item.target for item in retry_instructions]
        retry_reasons = {item.target: item.reason for item in retry_instructions}

        if (
            aggregate.ticket.demo_mode == "force_escalate"
            or assessment.verdict == "escalate"
            or assessment.requires_human_follow_up
            or (
                aggregate.business.impact_score >= self._HIGH_IMPACT_SCORE
                and assessment.confidence < self._MIN_CONFIDENCE
            )
        ):
            return ValidatorPolicyDecision(
                action="error",
                retry_targets=retry_targets,
                retry_reasons=retry_reasons,
                reason=assessment.rationale,
                retry_snapshot=dict(retry_counts),
            )

        if (
            assessment.verdict == "complete"
            and assessment.confidence >= self._CLOSE_CONFIDENCE
            and not retry_targets
            and not assessment.requires_human_follow_up
        ):
            return ValidatorPolicyDecision(
                action="close",
                reason=assessment.rationale,
                retry_snapshot=dict(retry_counts),
            )

        if (
            aggregate.ticket.demo_mode == "prefer_close"
            and assessment.verdict != "escalate"
            and assessment.confidence >= self._MIN_CONFIDENCE
            and not retry_targets
            and bool(aggregate.knowledge.evidence)
            and bool(aggregate.context.recent_related_cases)
        ):
            return ValidatorPolicyDecision(
                action="close",
                reason=f"{assessment.rationale} | Closed via prefer_close demo override.",
                retry_snapshot=dict(retry_counts),
            )

        allowed_targets = [target for target in retry_targets if retry_counts.get(target, 0) < self._max_retries]
        if allowed_targets:
            updated_retry_counts = dict(retry_counts)
            for target in allowed_targets:
                updated_retry_counts[target] = updated_retry_counts.get(target, 0) + 1
            return ValidatorPolicyDecision(
                action="retry",
                retry_targets=allowed_targets,
                retry_reasons={target: retry_reasons[target] for target in allowed_targets},
                reason=assessment.rationale,
                retry_snapshot=updated_retry_counts,
            )

        return ValidatorPolicyDecision(
            action="error",
            retry_targets=retry_targets,
            retry_reasons=retry_reasons,
            reason=f"Retries exhausted (max per target={self._max_retries}). {assessment.rationale}",
            retry_snapshot=dict(retry_counts),
        )

    @handler
    async def handle(self, validation: ValidationPackage, ctx: WorkflowContext[ValidatorPolicyDecision]) -> None:
        aggregate: AggregatedIncidentContext = ctx.get_state(AGGREGATE_STATE_KEY)
        retry_counts: dict[str, int] = dict(ctx.get_state(RETRY_STATE_KEY))
        attrs = span_attrs(**{A_TICKET_ID: aggregate.ticket.ticket_id if aggregate.ticket else None})
        with start_executor_span("retry_controller", attrs) as span:
            decision = self._derive_decision(validation.assessment, aggregate, retry_counts)
            if decision.action == "retry":
                ctx.set_state(RETRY_STATE_KEY, decision.retry_snapshot)
            span.set_attribute(A_ACTION, decision.action)
            span.set_attribute(A_RETRY_TGT, ",".join(decision.retry_targets) if decision.retry_targets else "")
            set_ok(span)
            await ctx.send_message(decision)


class CloseTicket(Executor):
    """Formats the successful workflow output."""

    @handler
    async def handle(self, decision: ValidatorPolicyDecision, ctx: WorkflowContext[Never, str]) -> None:
        aggregate: AggregatedIncidentContext = ctx.get_state(AGGREGATE_STATE_KEY)
        resolution: ResolutionPackage = ctx.get_state(RESOLUTION_STATE_KEY)
        attrs = span_attrs(**{A_TICKET_ID: aggregate.ticket.ticket_id if aggregate.ticket else None, A_ACTION: "close"})
        with start_executor_span("close_ticket", attrs) as span:
            set_ok(span)
        await ctx.yield_output(
            "Support ticket closed successfully\n"
            f"Ticket: {aggregate.ticket.ticket_id}\n"
            f"Customer: {aggregate.ticket.customer_name}\n"
            f"Probable cause: {aggregate.knowledge.probable_cause}\n"
            f"Microsoft guidance: {', '.join(aggregate.docs.recommended_articles[:2]) or 'No articles returned'}\n"
            f"Customer summary: {resolution.draft.customer_summary}\n"
            f"Next actions: {', '.join(resolution.draft.next_actions)}\n"
            f"Retry reasons: {decision.retry_reasons}\n"
            f"Retry counts: {decision.retry_snapshot}\n"
            f"Validator rationale: {decision.reason}"
        )


class ErrorTicket(Executor):
    """Terminal error output when validation keeps failing or retries are exhausted."""

    @handler
    async def handle(self, decision: ValidatorPolicyDecision, ctx: WorkflowContext[Never, str]) -> None:
        aggregate: AggregatedIncidentContext = ctx.get_state(AGGREGATE_STATE_KEY)
        validation: ValidationPackage = ctx.get_state(VALIDATION_STATE_KEY)
        attrs = span_attrs(**{A_TICKET_ID: aggregate.ticket.ticket_id if aggregate.ticket else None, A_ACTION: "error"})
        with start_executor_span("error_ticket", attrs) as span:
            set_ok(span)
        await ctx.yield_output(
            "Support ticket flow failed after validator checks\n"
            f"Ticket: {aggregate.ticket.ticket_id}\n"
            f"Retry counts: {decision.retry_snapshot}\n"
            f"Requested retry targets: {decision.retry_targets}\n"
            f"Retry reasons: {decision.retry_reasons}\n"
            f"Validator concerns: {', '.join(validation.assessment.concerns) or 'None provided'}\n"
            f"Error reason: {decision.reason}"
        )


def _build_agent(
    agent_name: str,
    instructions: str,
    diagnostics: AgentTelemetryRegistry,
    *,
    client: FoundryChatClient,
    tools: list[Any] | None = None,
    response_format: Any | None = None,
    extra_middleware: list[Any] | None = None,
) -> Agent:
    middleware = [AgentResponseDiagnosticsMiddleware(diagnostics, agent_name)]
    if tools:
        middleware.append(ToolCallDiagnosticsMiddleware(diagnostics, agent_name))
    if extra_middleware:
        middleware.extend(extra_middleware)

    default_options = {"response_format": response_format} if response_format is not None else None
    return Agent(
        client=client,
        name=agent_name,
        instructions=instructions,
        tools=tools or [],
        middleware=middleware,
        default_options=default_options,
    )


def create_support_ticket_workflow():
    """Create the support-ticket workflow POC."""
    diagnostics = AgentTelemetryRegistry()
    shared_client = _make_foundry_client()
    microsoft_learn_mcp_url = os.getenv("MICROSOFT_LEARN_MCP_URL", "https://learn.microsoft.com/api/mcp")
    microsoft_learn_mcp_tool = shared_client.get_mcp_tool(
        name="Microsoft_Learn_MCP",
        url=microsoft_learn_mcp_url,
        description="Microsoft Learn documentation and troubleshooting guidance for Microsoft products.",
    )

    triage_service = TriageService(
        _build_agent(
            "triage_agent",
            "You classify IT incidents. Return structured JSON only.",
            diagnostics,
            client=shared_client,
            response_format=TriageAssessmentModel,
        ),
        diagnostics,
        "triage_agent",
    )
    knowledge_service = KnowledgeService(
        _build_agent(
            "knowledge_agent",
            "You ground your answer in tools and produce structured IT runbook guidance.",
            diagnostics,
            client=shared_client,
            tools=[lookup_runbook, get_service_health],
            response_format=KnowledgeAssessmentModel,
        ),
        diagnostics,
        "knowledge_agent",
    )
    context_service = ContextService(
        _build_agent(
            "context_agent",
            "You ground your answer in ticket history and asset details before returning structured JSON.",
            diagnostics,
            client=shared_client,
            tools=[lookup_recent_cases, lookup_asset_record],
            response_format=ContextAssessmentModel,
        ),
        diagnostics,
        "context_agent",
    )
    docs_service = DocsService(
        _build_agent(
            "docs_agent",
            "You must use Microsoft Learn MCP tools to find relevant Microsoft product guidance and return structured JSON only.",
            diagnostics,
            client=shared_client,
            tools=[microsoft_learn_mcp_tool],
            response_format=DocsAssessmentModel,
        ),
        diagnostics,
        "docs_agent",
    )
    resolution_service = ResolutionService(
        _build_agent(
            "resolution_agent",
            "You write concise internal and customer-ready incident summaries as structured JSON.",
            diagnostics,
            client=shared_client,
            response_format=ResolutionDraftModel,
        ),
        diagnostics,
        "resolution_agent",
    )
    validation_service = ValidationService(
        _build_agent(
            "validator_agent",
            "You are a strict workflow validator and must return structured JSON only.",
            diagnostics,
            client=shared_client,
            response_format=ValidationAssessmentModel,
        ),
        diagnostics,
        "validator_agent",
    )

    initialize = InitializeTicket(diagnostics=diagnostics, id="initialize_ticket")
    route_ticket = RouteTicket(id="route_ticket")
    prepare_context = PrepareContext(id="prepare_context")
    triage = TriageBranch(service=triage_service, id="triage_branch")
    knowledge = KnowledgeBranch(service=knowledge_service, id="knowledge_branch")
    context_branch = ContextBranch(service=context_service, id="context_branch")
    docs_branch = DocsBranch(service=docs_service, id="docs_branch")
    business = BusinessImpactScorer(id="business_impact_branch")
    aggregate_initial = AggregateIncident(id="aggregate_initial")
    resolution = ResolutionBranch(service=resolution_service, id="resolution_branch")
    validator = ValidatorBranch(service=validation_service, id="validator_branch")
    retry_controller = RetryController(id="retry_controller")
    close_ticket = CloseTicket(id="close_ticket")
    error_ticket = ErrorTicket(id="error_ticket")

    return (
        WorkflowBuilder(
            name="Support Ticket Retry Controller Workflow",
            description="Hosted-first POC: parallel analysis with middleware diagnostics and explicit retry-controller policy routing.",
            start_executor=initialize,
        )
        # Initial pass
        .add_edge(initialize, route_ticket)
        .add_edge(route_ticket, prepare_context)
        .add_fan_out_edges(prepare_context, [triage, knowledge, context_branch, docs_branch, business])
        .add_fan_in_edges([triage, knowledge, context_branch, docs_branch, business], aggregate_initial)
        .add_edge(aggregate_initial, resolution)
        .add_edge(resolution, validator)
        .add_edge(validator, retry_controller)
        # Validator decision — retry loops back to prepare_context (same node, no duplicates).
        # prepare_context stores the ValidatorPolicyDecision in state; each branch uses it
        # to decide whether to re-run its service or forward the cached result.
        .add_switch_case_edge_group(
            retry_controller,
            [
                Case(condition=_action_is("close"), target=close_ticket),
                Case(condition=_action_is("retry"), target=prepare_context),
                Default(target=error_ticket),
            ],
        )
        .build()
    )
