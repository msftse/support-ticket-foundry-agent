# Copyright (c) Microsoft. All rights reserved.

"""Host the support-ticket workflow POC behind the Responses protocol."""

import os

from agent_framework_foundry_hosting import ResponsesHostServer
from dotenv import load_dotenv

from workflow import create_support_ticket_workflow


def _configure_telemetry() -> None:
    conn_str = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn_str:
        return
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor  # type: ignore[import]

        configure_azure_monitor(connection_string=conn_str, enable_live_metrics=False)
        print("[telemetry] Azure Monitor configured.", flush=True)
    except ImportError:
        print("[telemetry] azure-monitor-opentelemetry not installed; skipping.", flush=True)


def main() -> None:
    load_dotenv()
    _configure_telemetry()
    workflow = create_support_ticket_workflow()
    workflow_agent = workflow.as_agent(name="SupportTicketWorkflowAgent")
    server = ResponsesHostServer(workflow_agent)
    server.run()


if __name__ == "__main__":
    main()
