# Copyright (c) Microsoft. All rights reserved.

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

RetryTarget = Literal["prepare", "triage", "knowledge", "context"]
WorkflowAction = Literal["close", "retry", "error"]


class SupportTicketCase(BaseModel):
    """Entry payload for the support-ticket workflow."""

    ticket_id: str = Field(description="Stable ticket identifier.", default="INC-1042")
    customer_name: str = Field(description="Customer or employee name.", default="Adele Vance")
    customer_tier: Literal["standard", "priority", "executive"] = Field(
        description="Business tier for SLA scoring.",
        default="priority",
    )
    severity: Literal["low", "medium", "high", "critical"] = Field(
        description="Incident severity selected by intake.",
        default="high",
    )
    category: Literal["identity", "network", "device", "collaboration"] = Field(
        description="Primary issue category.",
        default="device",
    )
    service_name: str = Field(description="Impacted service or application.", default="Endpoint Manager")
    region: str = Field(description="Region where the issue is observed.", default="westus2")
    asset_id: str | None = Field(description="Optional device or asset identifier.", default="LT-2024-77")
    summary: str = Field(
        description="Short description of the issue.",
        default="Laptop sign-in keeps failing after a security update and the user cannot access Outlook.",
    )
    recent_change: bool = Field(description="Whether a recent change likely triggered the incident.", default=True)
    business_deadline_hours: int = Field(description="Hours until the business deadline is impacted.", default=6)
    notes: str | None = Field(description="Extra notes from the intake agent or human.", default=None)
    demo_mode: Literal["normal", "prefer_close", "force_retry_knowledge", "force_escalate"] = Field(
        description="Optional deterministic demo mode used to illustrate close, retry, and escalation scenarios.",
        default="normal",
    )


class TriageAssessmentModel(BaseModel):
    """Structured output returned by the triage agent."""

    confirmed_priority: Literal["low", "medium", "high", "critical"]
    suspected_team: Literal["identity", "network", "device", "collaboration"]
    confidence: float = Field(ge=0.0, le=1.0)
    missing_information: list[str] = Field(default_factory=list)
    concise_reason: str


class KnowledgeAssessmentModel(BaseModel):
    """Structured output returned by the knowledge agent."""

    probable_cause: str
    recommended_runbook: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    should_retry: bool = False


class ContextAssessmentModel(BaseModel):
    """Structured output returned by the context agent."""

    asset_summary: str
    recent_related_cases: list[str] = Field(default_factory=list)
    escalation_notes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    should_retry: bool = False


class DocsAssessmentModel(BaseModel):
    """Structured output returned by the Microsoft Learn docs agent."""

    recommended_articles: list[str] = Field(default_factory=list)
    troubleshooting_notes: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    should_retry: bool = False


class ResolutionDraftModel(BaseModel):
    """Structured output returned by the resolution agent."""

    internal_summary: str
    customer_summary: str
    next_actions: list[str] = Field(default_factory=list)
    closure_recommendation: str
    confidence: float = Field(ge=0.0, le=1.0)


class RetryInstructionModel(BaseModel):
    """Validator-provided instruction describing which node should be retried and why."""

    target: RetryTarget
    reason: str


class ValidationAssessmentModel(BaseModel):
    """Structured output returned by the validator agent."""

    verdict: Literal["complete", "needs_retry", "escalate"]
    confidence: float = Field(ge=0.0, le=1.0)
    retry_targets: list[RetryTarget] = Field(default_factory=list)
    retry_instructions: list[RetryInstructionModel] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    rationale: str
    requires_human_follow_up: bool = False


@dataclass
class BranchDiagnostics:
    """Non-LLM metadata gathered by middleware and branch wrappers."""

    agent_name: str
    attempt_count: int = 0
    tool_calls: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class AssetSnapshot:
    """Deterministic asset details used by both code paths and tools."""

    asset_id: str
    service: str
    owner: str
    environment: str
    recent_change: str
    known_issue: str


@dataclass
class NormalizedTicket:
    """Workflow state after intake normalization."""

    ticket_id: str
    customer_name: str
    customer_tier: str
    severity: str
    category: str
    service_name: str
    region: str
    asset_id: str | None
    summary: str
    recent_change: bool
    business_deadline_hours: int
    notes: str
    demo_mode: str = "normal"
    route_reason: str = ""
    asset_snapshot: AssetSnapshot | None = None


@dataclass
class RouteDecision:
    """Routing decision used for the if/else-style branch."""

    path: Literal["asset_enrichment", "direct"]
    reason: str


@dataclass
class TriageBranchOutput:
    """Normalized triage branch output."""

    confirmed_priority: str
    suspected_team: str
    confidence: float
    missing_information: list[str]
    concise_reason: str
    diagnostics: BranchDiagnostics


@dataclass
class KnowledgeBranchOutput:
    """Normalized knowledge branch output."""

    probable_cause: str
    recommended_runbook: list[str]
    evidence: list[str]
    confidence: float
    should_retry: bool
    diagnostics: BranchDiagnostics


@dataclass
class ContextBranchOutput:
    """Normalized context branch output."""

    asset_summary: str
    recent_related_cases: list[str]
    escalation_notes: list[str]
    confidence: float
    should_retry: bool
    diagnostics: BranchDiagnostics


@dataclass
class DocsBranchOutput:
    """Normalized Microsoft Learn guidance branch output."""

    recommended_articles: list[str]
    troubleshooting_notes: list[str]
    confidence: float
    should_retry: bool
    diagnostics: BranchDiagnostics


@dataclass
class BusinessScoreOutput:
    """Deterministic branch showing regular Python business logic."""

    impact_score: int
    sla_minutes: int
    should_page_on_call: bool
    business_reason: str


@dataclass
class AggregatedIncidentContext:
    """Single object consumed by the resolution and validator stages."""

    ticket: NormalizedTicket
    triage: TriageBranchOutput
    knowledge: KnowledgeBranchOutput
    context: ContextBranchOutput
    docs: DocsBranchOutput
    business: BusinessScoreOutput
    retry_history: dict[str, int]


@dataclass
class ResolutionPackage:
    """Resolution draft plus diagnostics."""

    draft: ResolutionDraftModel
    diagnostics: BranchDiagnostics


@dataclass
class ValidationPackage:
    """Validator output plus diagnostics."""

    assessment: ValidationAssessmentModel
    diagnostics: BranchDiagnostics


@dataclass
class ValidatorPolicyDecision:
    """Deterministic action emitted after inspecting the validator LLM result."""

    action: WorkflowAction
    retry_targets: list[RetryTarget] = field(default_factory=list)
    retry_reasons: dict[RetryTarget, str] = field(default_factory=dict)
    reason: str = ""
    retry_snapshot: dict[str, int] = field(default_factory=dict)
