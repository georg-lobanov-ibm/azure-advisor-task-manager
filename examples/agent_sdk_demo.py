#!/usr/bin/env python3
"""
Demo of the Azure Advisor Task Manager using the Anthropic API and MCP tools.

This demonstrates:
1. Initializing the Advisor Agent
2. Dry-run analysis of Azure Advisor findings
3. Processing and proposing Jira changes
4. Decision logging and audit trail

Examples 1 and 2 call the Claude API and start the MCP servers (the Azure one
needs uvx and Azure credentials). Examples 3, 4 and 5 only inspect the
agent and make no API calls.
"""

import sys
import os
import json
from pathlib import Path

from dotenv import load_dotenv

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from advisor_agent.agent import AdvisorAgent


def demo_basic_agent_interaction():
    """Example 1: Basic agent interaction."""
    print("=" * 70)
    print("Example 1: Initialize and Query the Advisor Agent")
    print("=" * 70)

    # Simple query
    prompt = """List the key features and benefits of the Azure Advisor Task Manager agent.
    What makes it different from a simple script?"""

    with AdvisorAgent() as agent:
        response = agent.query(prompt)
    print(f"\nAgent Response:\n{response}\n")


def demo_dry_run_analysis():
    """Example 2: Dry-run analysis of findings."""
    print("=" * 70)
    print("Example 2: Dry-Run Analysis of Azure Advisor Findings")
    print("=" * 70)

    # Simulate a dry-run analysis
    print("\nInitiating dry-run analysis...")
    with AdvisorAgent() as agent:
        result = agent.process_findings_dry_run(
            subscription_id="12345678-1234-1234-1234-123456789012",
            jira_project="AZURE",
            dry_run=True,
        )

    print(f"\nAnalysis Result:")
    print(json.dumps(result, indent=2))


def demo_available_tools():
    """Example 3: Available tools."""
    print("=" * 70)
    print("Example 3: MCP Servers Available to the Agent")
    print("=" * 70)

    with AdvisorAgent() as agent:
        # Nothing is started yet, so this lists each configured server as
        # "server:*". After the first query() it lists the real tool names.
        tools = agent.get_agent_tools()

    print("\nThe Advisor Agent's tools come from these MCP servers:\n")
    for tool in tools:
        print(f"  • {tool}")

    print("\nThe agent has no file, shell or web tools. Through the servers it can:")
    print("  • Read Azure Advisor recommendations (read-only server)")
    print("  • Read Jira issues, if the Jira server is configured")
    print("  • Change Jira only after you approve each call on the terminal")
    print("    (calls are denied automatically when there is no terminal)")


def demo_decision_logging():
    """Example 4: Decision logging and audit trail."""
    print("=" * 70)
    print("Example 4: Decision Logging and Audit Trail")
    print("=" * 70)

    # Simulate some decisions
    decisions = [
        {
            "timestamp": "2024-01-15T10:30:00Z",
            "advisor_id": "advisor-1001",
            "category": "Cost",
            "impact": "High",
            "decision": "CREATE",
            "jira_key": "AZURE-123",
            "rationale": "High impact cost optimization, no existing ticket",
            "estimated_savings": "$5000/month",
        },
        {
            "timestamp": "2024-01-15T10:31:00Z",
            "advisor_id": "advisor-1002",
            "category": "Security",
            "impact": "Medium",
            "decision": "SKIP",
            "rationale": "Below threshold, will revisit quarterly",
        },
        {
            "timestamp": "2024-01-15T10:32:00Z",
            "advisor_id": "advisor-1003",
            "category": "Reliability",
            "impact": "High",
            "decision": "UPDATE",
            "jira_key": "AZURE-456",
            "rationale": "Severity escalated from Medium to High",
        },
    ]

    with AdvisorAgent() as agent:
        print("\nSimulating decision logging...\n")
        for decision in decisions:
            agent.save_decision_log(decision)
            print(f"Logged: {decision['decision']:6} | {decision['advisor_id']:15} | {decision['category']:12}")

        print("\nLoading audit log...")
        log = agent.load_decision_log()

    print(f"Total decisions in log: {len(log)}\n")

    print("Full audit log:")
    print(json.dumps(log, indent=2))


def demo_system_prompt():
    """Example 5: System prompt guidance."""
    print("=" * 70)
    print("Example 5: Agent System Prompt")
    print("=" * 70)

    with AdvisorAgent() as agent:
        system_prompt = agent.system_prompt

    print("\nThe agent operates under this system prompt:\n")
    print("-" * 70)
    print(system_prompt)
    print("-" * 70)


if __name__ == "__main__":
    # Load the repo-root .env (see .env.example). Variables already set in the
    # environment take precedence over the file; a missing file is ignored.
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")

    # Check for API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable not set")
        print("Please set it in .env (cp .env.example .env) or export it:")
        print("  export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    # Run demos
    try:
        demo_basic_agent_interaction()
        print("\n")
        demo_available_tools()
        print("\n")
        demo_decision_logging()
        print("\n")
        demo_system_prompt()
        print("\n")
        demo_dry_run_analysis()

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
