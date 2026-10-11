#!/usr/bin/env python3
"""
Basic example of using the Claude API client for the Azure Advisor Task Manager.

This demonstrates:
1. Single-turn message exchange
2. Multi-turn conversation
3. Tool use setup (for MCP servers)
"""

import sys
import os

# Add src to path so we can import our modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from advisor_agent.client import AdvisorAgentClient


def example_basic_chat():
    """Example 1: Basic single and multi-turn conversation."""
    print("=" * 60)
    print("Example 1: Basic Chat")
    print("=" * 60)

    client = AdvisorAgentClient()

    # Print model info
    model_info = client.get_model_info()
    print(f"Using model: {model_info['display_name']}")
    print(f"Max input tokens: {model_info['max_input_tokens']}")
    print()

    # Single turn
    print("User: What is the Azure Advisor tool?")
    response = client.send_message(
        "What is the Azure Advisor tool?",
        system_prompt="You are an expert on Microsoft Azure services.",
    )
    print(f"Assistant: {response}\n")

    # Multi-turn - the conversation history is maintained
    print("User: What recommendations does it provide?")
    response = client.send_message(
        "What recommendations does it provide?"
    )
    print(f"Assistant: {response}\n")


def example_system_prompt():
    """Example 2: Using a system prompt to guide Claude's behavior."""
    print("=" * 60)
    print("Example 2: System Prompt Guidance")
    print("=" * 60)

    client = AdvisorAgentClient()

    system_prompt = """You are the Azure Advisor Task Manager agent. Your role is to:
1. Analyze Azure Advisor recommendations
2. Decide whether to create, update, skip, or close Jira tickets
3. Provide structured proposals for human approval

Keep responses concise and professional. Focus on actionable insights."""

    user_query = """Imagine we have these Azure Advisor findings:
    - High severity: Unused VMs (potential savings: $5000/month)
    - Medium severity: Non-configured backup on databases

What would be your initial assessment for creating Jira tickets?"""

    response = client.send_message(user_query, system_prompt=system_prompt)
    print(f"Response:\n{response}\n")


def example_tool_definitions():
    """Example 3: Demonstrating tool definitions (for MCP servers)."""
    print("=" * 60)
    print("Example 3: Tool Definitions (MCP Server Integration)")
    print("=" * 60)

    client = AdvisorAgentClient()

    # Define tools that would call MCP servers
    tools = [
        {
            "name": "get_advisor_findings",
            "description": "Fetch Azure Advisor recommendations from Azure MCP server",
            "input_schema": {
                "type": "object",
                "properties": {
                    "subscription_id": {
                        "type": "string",
                        "description": "Azure subscription ID",
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter by category (Cost, Security, Reliability, Performance, OperationalExcellence)",
                        "enum": [
                            "Cost",
                            "Security",
                            "Reliability",
                            "Performance",
                            "OperationalExcellence",
                        ],
                    },
                },
                "required": ["subscription_id"],
            },
        },
        {
            "name": "create_jira_issue",
            "description": "Create a new Jira issue for an Azure Advisor finding",
            "input_schema": {
                "type": "object",
                "properties": {
                    "project_key": {
                        "type": "string",
                        "description": "Jira project key (e.g., AZURE)",
                    },
                    "issue_type": {
                        "type": "string",
                        "description": "Type of issue (e.g., Task, Bug, Story)",
                    },
                    "summary": {"type": "string", "description": "Issue summary"},
                    "description": {
                        "type": "string",
                        "description": "Detailed issue description",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Issue priority",
                        "enum": ["Low", "Medium", "High"],
                    },
                },
                "required": ["project_key", "issue_type", "summary", "description"],
            },
        },
    ]

    user_message = """Based on Azure Advisor findings, I need to create a Jira ticket
    for a high-severity cost optimization recommendation.
    Can you help me decide what the ticket should contain?"""

    text_response, tool_calls = client.send_message_with_tools(
        user_message, tools=tools, system_prompt="You are the Azure Advisor Task Manager."
    )

    print(f"Response: {text_response}\n")
    if tool_calls:
        print("Tool calls requested:")
        for call in tool_calls:
            print(f"  - Tool: {call['name']}")
            print(f"    Input: {call['input']}\n")
    else:
        print("No tool calls were made.\n")


if __name__ == "__main__":
    # Make sure ANTHROPIC_API_KEY is set
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY environment variable not set")
        print("Please set it before running this example:")
        print("  export ANTHROPIC_API_KEY='your-api-key-here'")
        sys.exit(1)

    # Run examples
    try:
        example_basic_chat()
        print()
        example_system_prompt()
        print()
        example_tool_definitions()
    except Exception as e:
        print(f"Error: {e}")
        raise
