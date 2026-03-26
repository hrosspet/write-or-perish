"""
LLM Provider Abstraction Layer

This module provides a unified interface for calling different LLM providers
(OpenAI and Anthropic) with automatic format conversion.
"""
import logging
import re

import anthropic
import openai
from anthropic import Anthropic
from openai import OpenAI
from flask import current_app

logger = logging.getLogger(__name__)

DEFAULT_MAX_OUTPUT_TOKENS = 10000


class PromptTooLongError(Exception):
    """Raised when the prompt exceeds the model's context window."""

    def __init__(self, actual_tokens: int, max_tokens: int, original_error=None):
        self.actual_tokens = actual_tokens
        self.max_tokens = max_tokens
        self.original_error = original_error
        super().__init__(
            f"Prompt too long: {actual_tokens} tokens > {max_tokens} maximum"
        )


class LLMProvider:
    """Unified interface for multiple LLM providers"""

    @staticmethod
    def get_completion(model_id: str, messages: list, api_keys: dict,
                       max_tokens: int = None, tools: list = None) -> dict:
        """
        Generate a completion using the specified model.

        Args:
            model_id: Internal model identifier (e.g., "gpt-5", "claude-sonnet-4.5")
            messages: List of message dicts in OpenAI format
            api_keys: Dict with "openai" and "anthropic" keys
            max_tokens: Optional max output tokens (overrides provider default)
            tools: Optional list of tool definitions (Anthropic format)

        Returns:
            Dict with:
                - content (str): The generated text
                - total_tokens (int): Total tokens used
                - tool_calls (list): Tool call results [{id, name, input}]

        Raises:
            ValueError: If model is unsupported or provider is unknown
        """
        config = current_app.config["SUPPORTED_MODELS"].get(model_id)
        if not config:
            raise ValueError(f"Unsupported model: {model_id}")

        provider = config["provider"]
        api_model = config["api_model"]

        model_max = config.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
        if max_tokens is None:
            max_tokens = min(model_max, DEFAULT_MAX_OUTPUT_TOKENS)
        else:
            max_tokens = min(max_tokens, model_max, DEFAULT_MAX_OUTPUT_TOKENS)

        if provider == "openai":
            return LLMProvider._call_openai(
                api_model, messages, api_keys["openai"], max_tokens,
                tools=tools)
        elif provider == "anthropic":
            return LLMProvider._call_anthropic(
                api_model, messages, api_keys["anthropic"], max_tokens,
                tools=tools)
        else:
            raise ValueError(f"Unknown provider: {provider}")

    @staticmethod
    def _call_openai(model: str, messages: list, api_key: str,
                     max_tokens: int = None, tools: list = None) -> dict:
        """
        Call OpenAI API with the given model and messages.

        Args:
            model: OpenAI model name (e.g., "gpt-5")
            messages: List of message dicts in OpenAI format
            api_key: OpenAI API key
            tools: Optional tool definitions (Anthropic format, converted here)

        Returns:
            Dict with content, total_tokens, and tool_calls
        """
        client = OpenAI(api_key=api_key)

        kwargs = dict(
            model=model,
            messages=messages,
            temperature=1,
            max_completion_tokens=max_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
        )

        # Convert Anthropic-format tools to OpenAI format
        if tools:
            openai_tools = []
            for tool in tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {}),
                    }
                })
            kwargs["tools"] = openai_tools

        try:
            response = client.chat.completions.create(**kwargs)
        except openai.BadRequestError as e:
            error_msg = str(e)
            match = re.search(
                r'maximum context length is (\d+) tokens.*?resulted in (\d+) tokens',
                error_msg
            )
            if match:
                max_tok = int(match.group(1))
                actual_tok = int(match.group(2))
                raise PromptTooLongError(actual_tok, max_tok, e) from e
            raise

        message = response.choices[0].message
        tool_calls = []
        if message.tool_calls:
            import json
            for tc in message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": json.loads(tc.function.arguments),
                })

        return {
            "content": message.content or "",
            "total_tokens": response.usage.total_tokens,
            "input_tokens": response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
            "tool_calls": tool_calls,
        }

    @staticmethod
    def _call_anthropic(model: str, messages: list, api_key: str,
                        max_tokens: int = None, tools: list = None) -> dict:
        """
        Call Anthropic API with the given model and messages.

        Converts OpenAI-style messages to Anthropic format:
        - System messages go in a separate 'system' parameter
        - Messages must alternate between 'user' and 'assistant'

        Args:
            model: Anthropic model name (e.g., "claude-sonnet-4-5-20250929")
            messages: List of message dicts in OpenAI format
            api_key: Anthropic API key
            tools: Optional tool definitions (Anthropic format)

        Returns:
            Dict with content, total_tokens, and tool_calls
        """
        client = Anthropic(api_key=api_key)

        # Extract system messages
        system_messages = [m for m in messages if m.get("role") == "system"]
        system_text = "\n\n".join([
            m["content"][0]["text"] if isinstance(m.get("content"), list) else m["content"]
            for m in system_messages
            if m.get("content")
        ])

        # Convert remaining messages to Anthropic format
        anthropic_messages = []
        for msg in messages:
            if msg["role"] in ["user", "assistant"]:
                content = msg["content"]
                # Convert content format if needed
                if isinstance(content, list) and len(content) > 0:
                    if isinstance(content[0], dict) and "text" in content[0]:
                        content = content[0]["text"]
                anthropic_messages.append({
                    "role": msg["role"],
                    "content": content
                })

        # Make API call
        # System parameter must be a list of content blocks
        system_param = [{"type": "text", "text": system_text}] if system_text else []

        if max_tokens is None:
            max_tokens = DEFAULT_MAX_OUTPUT_TOKENS

        # Log the actual API call details
        total_input_chars = sum(len(m.get("content", "")) for m in anthropic_messages)
        logger.info(f"Anthropic API call: model={model}, num_messages={len(anthropic_messages)}, total_input_chars={total_input_chars}, max_tokens={max_tokens}")

        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system_param,
            messages=anthropic_messages,
        )
        if tools:
            kwargs["tools"] = tools

        try:
            response = client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            error_msg = str(e)
            match = re.search(
                r'prompt is too long: (\d+) tokens > (\d+) maximum',
                error_msg
            )
            if match:
                actual_tok = int(match.group(1))
                max_tok = int(match.group(2))
                raise PromptTooLongError(actual_tok, max_tok, e) from e
            raise

        # Extract text content and tool calls from response
        content = ""
        tool_calls = []
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        # Calculate total tokens (Anthropic reports input/output separately)
        total_tokens = response.usage.input_tokens + response.usage.output_tokens

        return {
            "content": content,
            "total_tokens": total_tokens,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "tool_calls": tool_calls,
        }
