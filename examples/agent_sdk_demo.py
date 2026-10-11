#!/usr/bin/env python3
"""
Demo of the Azure Advisor Task Manager using Claude Agent SDK.

This demonstrates:
1. Initializing the Advisor Agent
2. Dry-run analysis of Azure Advisor findings
3. Processing and proposing Jira changes
4. Decision logging and audit trail
"""

import sys
import os
import json

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from advisor_agent.agent import AdvisorAgent


def demo_basic_agent_interaction():
    """Example 1: Basic agent interaction."""
    print("=" * 70)
    print("Example 1: Initialize and Query the Advisor Agent")
    print("=" * 70)

    agent = AdvisorAgent()

    # Simple query
    prompt = """List the key features and benefits of the Azure Advisor Task Manager agent.
    What makes it different from a simple script?"""

    response = agent.query(prompt)
    print(f"\nAgent Response:\n{response}\n")


def demo_dry_run_analysis():
    """Example 2: Dry-run analysis of findings."""
    print("=" * 70)
    print("Example 2: Dry-Run Analysis of Azure Advisor Findings")
    print("=" * 70)

    agent = AdvisorAgent()

    # Simulate a dry-run analysis
    print("\nInitiating dry-run analysis...")
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
    print("Example 3: Built-in Tools Available to the Agent")
    print("=" * 70)

    agent = AdvisorAgent()

    tools = agent.get_agent_tools()

    print("\nThe Advisor Agent has access to these built-in tools:\n")
    for tool in tools:
        print(f"  • {tool}")

    print("\nThese allow the agent to:")
    print("  • Read Azure Advisor findings and database records")
    print("  • Write audit logs and decision records")
    print("  • Execute bash commands for complex operations")
    print("  • Search file systems for historical data")
    print("  • Access external information via web search")
    print("  • (Soon) Connect to Azure Advisor MCP server")
    print("  • (Soon) Connect to Jira MCP server")


def demo_decision_logging():
    """Example 4: Decision logging and audit trail."""
    print("=" * 70)
    print("Example 4: Decision Logging and Audit Trail")
    print("=" * 70)

    agent = AdvisorAgent()

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

    agent = AdvisorAgent()

    print("\nThe agent operates under this system prompt:\n")
    print("-" * 70)
    print(agent.system_prompt)
    print("-" * 70)


if __name__ == "__main__":
    # Check for API key
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable not set")
        print("Please set it before running this demo:")
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
