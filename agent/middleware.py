# Copyright (c) Microsoft. All rights reserved.

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agent_framework import ChatContext, ChatMiddleware, FunctionInvocationContext, FunctionMiddleware
from models import (
    AggregatedIncidentContext,
    BranchDiagnostics,
    RetryInstructionModel,
    RetryTarget,
    ValidationAssessmentModel,
    ValidatorPolicyDecision,
)


@dataclass
class _TelemetryState:
    attempts: int = 0
    tool_calls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class AgentTelemetryRegistry:
    """Stores lightweight middleware diagnostics for the current workflow run."""

    def __init__(self) -> None:
        self._state: dict[str, _TelemetryState] = defaultdict(_TelemetryState)
        self._retry_aggregate: AggregatedIncidentContext | None = None
        self._retry_counts: dict[str, int] = {}
        self._policy_decision: ValidatorPolicyDecision | None = None

    def reset(self) -> None:
        self._state = defaultdict(_TelemetryState)
        self._retry_aggregate = None
        self._retry_counts = {}
        self._policy_decision = None

    def record_attempt(self, agent_name: str) -> None:
        self._state[agent_name].attempts += 1

    def record_tool_call(self, agent_name: str, tool_name: str) -> None:
        if tool_name not in self._state[agent_name].tool_calls:
            self._state[agent_name].tool_calls.append(tool_name)

    def record_note(self, agent_name: str, note: str) -> None:
        if note not in self._state[agent_name].notes:
            self._state[agent_name].notes.append(note)

    def snapshot(self, agent_name: str) -> BranchDiagnostics:
        state = self._state[agent_name]
        return BranchDiagnostics(
            agent_name=agent_name,
            attempt_count=state.attempts,
            tool_calls=list(state.tool_calls),
            notes=list(state.notes),
        )

    # --- validator policy slot ---

    def set_retry_context(self, aggregate: AggregatedIncidentContext, retry_counts: dict[str, int]) -> None:
        """Called by the executor before running the validator agent so the middleware has the workflow state it needs."""
        self._retry_aggregate = aggregate
        self._retry_counts = dict(retry_counts)

    def get_retry_context(self) -> tuple[AggregatedIncidentContext | None, dict[str, int]]:
        return self._retry_aggregate, dict(self._retry_counts)

    def set_policy_decision(self, decision: ValidatorPolicyDecision) -> None:
        self._policy_decision = decision

    def get_policy_decision(self) -> ValidatorPolicyDecision | None:
        return self._policy_decision


class AgentResponseDiagnosticsMiddleware(ChatMiddleware):
    """Tracks attempts and records simple response heuristics for validator policy."""

    def __init__(self, registry: AgentTelemetryRegistry, agent_name: str) -> None:
        self._registry = registry
        self._agent_name = agent_name

    async def process(
        self,
        context: ChatContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        self._registry.record_attempt(self._agent_name)
        await call_next()

        if context.stream or context.result is None:
            return

        response_text = getattr(context.result, "text", "")
        lowered = response_text.lower()
        if "insufficient" in lowered or "need more context" in lowered:
            self._registry.record_note(self._agent_name, "model_requested_more_context")
        if not response_text.strip():
            self._registry.record_note(self._agent_name, "empty_response")


class ToolCallDiagnosticsMiddleware(FunctionMiddleware):
    """Tracks tool usage so the validator can see whether agents grounded their answers."""

    def __init__(self, registry: AgentTelemetryRegistry, agent_name: str) -> None:
        self._registry = registry
        self._agent_name = agent_name

    async def process(
        self,
        context: FunctionInvocationContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        tool_name = getattr(context.function, "name", "unknown_tool")
        self._registry.record_tool_call(self._agent_name, tool_name)
        await call_next()

        if context.result is None:
            self._registry.record_note(self._agent_name, f"tool_returned_empty:{tool_name}")


class ValidatorPolicyMiddleware(ChatMiddleware):
    """Post-middleware on the validator agent that derives the retry/close/error decision.

    Flow:
      1. The ValidatorBranch executor stores (aggregate, retry_counts) in the registry
         *before* calling the agent.  This gives the middleware the workflow state it needs.
      2. After the agent responds, this middleware parses the ValidationAssessmentModel
         from the raw JSON response.
      3. It then runs the full policy logic:
           - Which nodes did the LLM say to retry (retry_instructions)?
           - Demo-mode overrides (force_retry_knowledge, force_escalate, prefer_close).
           - Retry-count caps (MAX_RETRIES).
           - Escalation triggers (impact score, requires_human_follow_up).
      4. The resulting ValidatorPolicyDecision is stored in the registry.
        5. The ValidatorBranch executor reads the decision, updates workflow state, and
            emits it so the graph can route to close_ticket / retry_selected_nodes / error_ticket.
    """

    # Thresholds mirrored from workflow constants (kept here so the middleware is self-contained).
    _CLOSE_CONFIDENCE = 0.8
    _MIN_CONFIDENCE = 0.6
    _HIGH_IMPACT_SCORE = 90
    def __init__(self, registry: AgentTelemetryRegistry, agent_name: str) -> None:
        self._registry = registry
        self._agent_name = agent_name
        self._max_retries = max(1, int(os.getenv("SUPPORT_WORKFLOW_MAX_RETRIES", "3")))

    # ------------------------------------------------------------------
    # Policy helpers
    # ------------------------------------------------------------------

    def _build_retry_instructions(
        self,
        assessment: ValidationAssessmentModel,
        aggregate: AggregatedIncidentContext,
        retry_counts: dict[str, int],
    ) -> list[RetryInstructionModel]:
        """Derive the ordered, deduplicated list of retry instructions.

        Priority:
          1. Explicit retry_instructions from the LLM response (middleware-enriched).
          2. Fallback: retry_targets with a generic reason.
          3. Demo-mode forced retry (injected last, only if not already present).
        """
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

        # Deduplicate preserving first-seen order.
        deduped: dict[RetryTarget, str] = {}
        for inst in instructions:
            if inst.target not in deduped:
                deduped[inst.target] = inst.reason
        return [RetryInstructionModel(target=t, reason=r) for t, r in deduped.items()]

    def _derive_decision(
        self,
        assessment: ValidationAssessmentModel,
        aggregate: AggregatedIncidentContext,
        retry_counts: dict[str, int],
    ) -> ValidatorPolicyDecision:
        """Apply policy rules on top of the LLM assessment and return a routing decision."""
        retry_instructions = self._build_retry_instructions(assessment, aggregate, retry_counts)
        retry_targets = [i.target for i in retry_instructions]
        retry_reasons = {i.target: i.reason for i in retry_instructions}

        # 1. Hard terminal-error triggers.
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

        # 2. Clean close (LLM says complete, high confidence, nothing to retry).
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

        # 3. prefer_close override (demo mode shortcut).
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

        # 4. Targeted retry (only targets that haven't hit the cap).
        allowed = [t for t in retry_targets if retry_counts.get(t, 0) < self._max_retries]
        if allowed:
            updated = dict(retry_counts)
            for t in allowed:
                updated[t] = updated.get(t, 0) + 1
            return ValidatorPolicyDecision(
                action="retry",
                retry_targets=allowed,
                retry_reasons={t: retry_reasons[t] for t in allowed},
                reason=assessment.rationale,
                retry_snapshot=updated,
            )

        # 5. All retry caps exhausted → terminal error.
        return ValidatorPolicyDecision(
            action="error",
            retry_targets=retry_targets,
            retry_reasons=retry_reasons,
            reason=f"Retries exhausted (max per target={self._max_retries}). {assessment.rationale}",
            retry_snapshot=dict(retry_counts),
        )

    # ------------------------------------------------------------------
    # Middleware hook
    # ------------------------------------------------------------------

    async def process(
        self,
        context: ChatContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        # Run the validator agent.
        await call_next()

        if context.result is None:
            return

        response_text = getattr(context.result, "text", "")
        if not response_text.strip():
            self._registry.record_note(self._agent_name, "validator_empty_response")
            return

        try:
            assessment = ValidationAssessmentModel.model_validate_json(response_text)
        except Exception as exc:
            self._registry.record_note(self._agent_name, f"validator_parse_error:{exc}")
            return

        # Read the workflow context the executor stored before calling the agent.
        aggregate, retry_counts = self._registry.get_retry_context()
        if aggregate is None:
            # Safety fallback: no context means we cannot apply policy — terminal error.
            self._registry.set_policy_decision(
                ValidatorPolicyDecision(
                    action="error",
                    reason="ValidatorPolicyMiddleware: retry context was not set before agent call.",
                    retry_snapshot={},
                )
            )
            return

        decision = self._derive_decision(assessment, aggregate, retry_counts)
        self._registry.set_policy_decision(decision)
