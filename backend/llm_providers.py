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
    def get_completion(model_id: str, messages: list, api_keys: dict) -> dict:
        """
        Generate a completion using the specified model.

        Args:
            model_id: Internal model identifier (e.g., "gpt-5", "claude-sonnet-4.5")
            messages: List of message dicts in OpenAI format
            api_keys: Dict with "openai" and "anthropic" keys

        Returns:
            Dict with:
                - content (str): The generated text
                - total_tokens (int): Total tokens used

        Raises:
            ValueError: If model is unsupported or provider is unknown
        """
        config = current_app.config["SUPPORTED_MODELS"].get(model_id)
        if not config:
            raise ValueError(f"Unsupported model: {model_id}")

        provider = config["provider"]
        api_model = config["api_model"]

        if provider == "openai":
            return LLMProvider._call_openai(api_model, messages, api_keys["openai"])
        elif provider == "anthropic":
            return LLMProvider._call_anthropic(api_model, messages, api_keys["anthropic"])
        else:
            raise ValueError(f"Unknown provider: {provider}")

    @staticmethod
    def _call_openai(model: str, messages: list, api_key: str) -> dict:
        """
        Call OpenAI API with the given model and messages.

        Args:
            model: OpenAI model name (e.g., "gpt-5")
            messages: List of message dicts in OpenAI format
            api_key: OpenAI API key

        Returns:
            Dict with content and total_tokens
        """
        client = OpenAI(api_key=api_key)
        try:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=1,
                max_completion_tokens=10000,
            )
        except openai.BadRequestError as e:
            error_msg = str(e)
            # e.g. "maximum context length is 128000 tokens. However, your messages resulted in 130000 tokens"
            match = re.search(
                r'maximum context length is (\d+) tokens.*?resulted in (\d+) tokens',
                error_msg
            )
            if match:
                max_tok = int(match.group(1))
                actual_tok = int(match.group(2))
                raise PromptTooLongError(actual_tok, max_tok, e) from e
            raise
        return {
            "content": response.choices[0].message.content,
            "total_tokens": response.usage.total_tokens,
            "input_tokens": response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
        }

    @staticmethod
    def _call_anthropic(model: str, messages: list, api_key: str) -> dict:
        """
        Call Anthropic API with the given model and messages.

        Converts OpenAI-style messages to Anthropic format:
        - System messages go in a separate 'system' parameter
        - Messages must alternate between 'user' and 'assistant'

        Args:
            model: Anthropic model name (e.g., "claude-sonnet-4-5-20250929")
            messages: List of message dicts in OpenAI format
            api_key: Anthropic API key

        Returns:
            Dict with content and total_tokens
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

        # Claude Opus 3 has a lower max_tokens limit than newer models
        max_tokens = 4096 if "claude-3-opus" in model else 10000

        # Log the actual API call details
        total_input_chars = sum(len(m.get("content", "")) for m in anthropic_messages)
        logger.info(f"Anthropic API call: model={model}, num_messages={len(anthropic_messages)}, total_input_chars={total_input_chars}, max_tokens={max_tokens}")

        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_param,
                messages=anthropic_messages
            )
        except anthropic.BadRequestError as e:
            error_msg = str(e)
            # e.g. "prompt is too long: 203565 tokens > 200000 maximum"
            match = re.search(
                r'prompt is too long: (\d+) tokens > (\d+) maximum',
                error_msg
            )
            if match:
                actual_tok = int(match.group(1))
                max_tok = int(match.group(2))
                raise PromptTooLongError(actual_tok, max_tok, e) from e
            raise

        # Extract text content from response
        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        # Calculate total tokens (Anthropic reports input/output separately)
        total_tokens = response.usage.input_tokens + response.usage.output_tokens

        return {
            "content": content,
            "total_tokens": total_tokens,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }
