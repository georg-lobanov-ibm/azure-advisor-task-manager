"""
Anthropic Claude API client for the Azure Advisor Task Manager.

This module provides a wrapper around the Anthropic API client with
support for the Agent to interact with MCP servers for Azure Advisor
and Jira integrations.
"""

import os
from anthropic import Anthropic


class AdvisorAgentClient:
    """Client for interacting with Claude API."""

    def __init__(self, api_key: str | None = None, model: str = "claude-opus-5-5"):
        """
        Initialize the Anthropic client.

        Args:
            api_key: Anthropic API key. If not provided, will use ANTHROPIC_API_KEY env var.
            model: Claude model to use. Defaults to Claude Opus 5.5 (latest and most capable).
        """
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY environment variable not set and no API key provided"
            )

        self.client = Anthropic(api_key=self.api_key)
        self.model = model
        self.conversation_history = []

    def send_message(
        self,
        user_message: str,
        system_prompt: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
    ) -> str:
        """
        Send a message to Claude and get a response.

        Args:
            user_message: The user's message to Claude.
            system_prompt: Optional system prompt to guide Claude's behavior.
            temperature: Sampling temperature (0-1). Higher = more random.
            max_tokens: Maximum tokens in the response.

        Returns:
            Claude's response text.
        """
        # Add user message to history
        self.conversation_history.append({"role": "user", "content": user_message})

        # Build messages for the API
        messages = self.conversation_history

        # Call Claude API
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=messages,
        )

        # Extract the response text
        assistant_message = response.content[0].text

        # Add assistant response to history for multi-turn conversations
        self.conversation_history.append(
            {"role": "assistant", "content": assistant_message}
        )

        return assistant_message

    def send_message_with_tools(
        self,
        user_message: str,
        tools: list[dict],
        system_prompt: str | None = None,
        temperature: float = 1.0,
        max_tokens: int = 4096,
    ) -> tuple[str, list[dict]]:
        """
        Send a message with tools available to Claude.

        This is useful for tool use scenarios (like calling MCP servers).

        Args:
            user_message: The user's message to Claude.
            tools: List of tool definitions (JSON schema format).
            system_prompt: Optional system prompt.
            temperature: Sampling temperature.
            max_tokens: Maximum tokens in response.

        Returns:
            Tuple of (response_text, tool_calls) where tool_calls is a list of
            tool use blocks if Claude called any tools, empty otherwise.
        """
        # Add user message to history
        self.conversation_history.append({"role": "user", "content": user_message})

        messages = self.conversation_history

        # Call Claude API with tools
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        # Process response content
        text_response = ""
        tool_calls = []

        for block in response.content:
            if hasattr(block, "text"):
                text_response = block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    {
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )

        # Add assistant response to history
        self.conversation_history.append({"role": "assistant", "content": response.content})

        return text_response, tool_calls

    def clear_history(self) -> None:
        """Clear the conversation history."""
        self.conversation_history = []

    def get_model_info(self) -> dict:
        """Get information about the current model."""
        model_info = self.client.models.retrieve(self.model)
        return {
            "id": model_info.id,
            "display_name": model_info.display_name,
            "max_input_tokens": model_info.max_input_tokens,
            "max_tokens": model_info.max_tokens,
        }
