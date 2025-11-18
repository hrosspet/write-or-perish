"""
LLM Provider Abstraction Layer

This module provides a unified interface for calling different LLM providers
(OpenAI and Anthropic) with automatic format conversion.
"""

from anthropic import Anthropic
from openai import OpenAI
from flask import current_app


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
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=1,
            max_completion_tokens=10000,
        )
        return {
            "content": response.choices[0].message.content,
            "total_tokens": response.usage.total_tokens
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

        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_param,
            messages=anthropic_messages
        )

        # Extract text content from response
        content = ""
        for block in response.content:
            if hasattr(block, "text"):
                content += block.text

        # Calculate total tokens (Anthropic reports input/output separately)
        total_tokens = response.usage.input_tokens + response.usage.output_tokens

        return {
            "content": content,
            "total_tokens": total_tokens
        }
