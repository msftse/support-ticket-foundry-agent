# Copyright (c) Microsoft. All rights reserved.

from __future__ import annotations

from typing import Annotated

from agent_framework import tool
from models import AssetSnapshot
from pydantic import Field


_ASSET_DATABASE: dict[str, AssetSnapshot] = {
    "LT-2024-77": AssetSnapshot(
        asset_id="LT-2024-77",
        service="Endpoint Manager",
        owner="Adele Vance",
        environment="corp-managed",
        recent_change="BitLocker policy and sign-in hardening applied 2 hours ago.",
        known_issue="Cached credentials can break Outlook token refresh after the hardening rollout.",
    ),
    "NW-EDGE-22": AssetSnapshot(
        asset_id="NW-EDGE-22",
        service="Corporate VPN",
        owner="Network Operations",
        environment="edge-westus2",
        recent_change="Gateway certificate rotated during the last maintenance window.",
        known_issue="Intermittent reconnect loops for clients on older tunnel profiles.",
    ),
}

_RECENT_CASES: dict[str, list[str]] = {
    "Endpoint Manager": [
        "INC-0990: Outlook sign-in failed after device policy refresh; resolved by clearing WAM cache.",
        "INC-1018: Device compliance policy rollout delayed SSO renewal for executive devices.",
    ],
    "Corporate VPN": [
        "INC-0871: West US users stuck in reconnect loop after gateway certificate rotation.",
        "INC-0944: Priority users unable to re-authenticate until the tunnel profile was reset.",
    ],
}

_RUNBOOKS: dict[str, list[str]] = {
    "Endpoint Manager": [
        "Confirm whether the latest security baseline is installed.",
        "Clear Workplace Join and Web Account Manager tokens.",
        "Force device sync and verify Outlook sign-in after cache reset.",
    ],
    "Corporate VPN": [
        "Check gateway health in the regional dashboard.",
        "Re-issue the tunnel profile if certificate trust is stale.",
        "Validate reconnect behavior on a clean user session.",
    ],
}

_SERVICE_HEALTH: dict[str, str] = {
    "Endpoint Manager": "No global outage. A contained westus2 degradation affects about 7 percent of managed laptops.",
    "Corporate VPN": "Intermittent westus2 gateway instability after certificate rotation. Mitigation is active.",
}


def fetch_asset_snapshot(asset_id: str | None, service_name: str, customer_name: str) -> AssetSnapshot:
    """Return deterministic asset data for code-driven enrichment."""
    if asset_id and asset_id in _ASSET_DATABASE:
        return _ASSET_DATABASE[asset_id]

    return AssetSnapshot(
        asset_id=asset_id or "unassigned-asset",
        service=service_name,
        owner=customer_name,
        environment="unknown",
        recent_change="No asset-specific change record found during intake.",
        known_issue="No asset-specific known issue was found.",
    )


@tool(approval_mode="never_require")
def lookup_runbook(
    service_name: Annotated[str, Field(description="Impacted service name.")],
) -> str:
    """Return deterministic runbook steps for the service."""
    steps = _RUNBOOKS.get(service_name, ["Collect fresh diagnostics and escalate to the owning team."])
    return "\n".join(f"- {step}" for step in steps)


@tool(approval_mode="never_require")
def get_service_health(
    service_name: Annotated[str, Field(description="Impacted service name.")],
) -> str:
    """Return a deterministic service health summary."""
    return _SERVICE_HEALTH.get(service_name, "No active service advisory is open for this service.")


@tool(approval_mode="never_require")
def lookup_recent_cases(
    service_name: Annotated[str, Field(description="Impacted service name.")],
) -> str:
    """Return similar incidents for context enrichment."""
    cases = _RECENT_CASES.get(service_name, ["No similar incidents were found in the last 30 days."])
    return "\n".join(f"- {case}" for case in cases)


@tool(approval_mode="never_require")
def lookup_asset_record(
    asset_id: Annotated[str, Field(description="Device or asset identifier.")],
) -> str:
    """Return the asset record used by the context agent."""
    snapshot = fetch_asset_snapshot(asset_id, service_name="Unknown service", customer_name="Unknown owner")
    return (
        f"Asset: {snapshot.asset_id}\n"
        f"Environment: {snapshot.environment}\n"
        f"Recent change: {snapshot.recent_change}\n"
        f"Known issue: {snapshot.known_issue}"
    )
