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
                       max_tokens: int = None, tools: list = None,
                       prompt_cache_key: str = None) -> dict:
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
                tools=tools, prompt_cache_key=prompt_cache_key,
                context_window=config.get("context_window"))
        elif provider == "anthropic":
            return LLMProvider._call_anthropic(
                api_model, messages, api_keys["anthropic"], max_tokens,
                tools=tools)
        else:
            raise ValueError(f"Unknown provider: {provider}")

    @staticmethod
    def _call_openai(model: str, messages: list, api_key: str,
                     max_tokens: int = None, tools: list = None,
                     prompt_cache_key: str = None,
                     context_window: int = None) -> dict:
        """
        Call OpenAI via the Responses API (/v1/responses).

        Migrated from /v1/chat/completions because that endpoint rejects
        function tools while model-default reasoning is active (400 on
        gpt-5.6-sol); the Responses API supports reasoning + tools
        together, so reasoning stays at the model's default here.

        Args:
            model: OpenAI model name (e.g., "gpt-5.6-sol")
            messages: List of message dicts in OpenAI chat format
            api_key: OpenAI API key
            tools: Optional tool definitions (Anthropic format, converted here)
            context_window: Model context window, used to report a context
                overflow when the API error carries no token counts

        Returns:
            Dict with content, total_tokens, and tool_calls
        """
        client = OpenAI(api_key=api_key)

        # Convert chat-format messages to Responses input items: content
        # block type is role-dependent (input_text for user/system,
        # output_text for assistant); plain strings pass through as-is.
        input_items = []
        for msg in messages:
            content = msg["content"]
            if isinstance(content, list):
                block_type = ("output_text" if msg["role"] == "assistant"
                              else "input_text")
                content = [
                    {"type": block_type,
                     "text": (block["text"]
                              if isinstance(block, dict) and "text" in block
                              else str(block))}
                    for block in content
                ]
            input_items.append({"role": msg["role"], "content": content})

        kwargs = dict(
            model=model,
            input=input_items,
            max_output_tokens=max_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
            # The Responses API defaults to store=True (response persisted
            # on OpenAI's side); chat completions didn't store — keep that.
            store=False,
        )
        # #189: a stable per-conversation key improves OpenAI's automatic
        # prefix-cache routing (best-effort; no behavior change otherwise).
        if prompt_cache_key:
            kwargs["prompt_cache_key"] = prompt_cache_key

        # Convert Anthropic-format tools to Responses format (flat, not
        # nested under "function"). strict mode would reject our schemas
        # (it requires all-required + additionalProperties: false).
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                    "strict": False,
                }
                for tool in tools
            ]

        try:
            response = client.responses.create(**kwargs)
        except openai.BadRequestError as e:
            mapped = LLMProvider._openai_overflow_error(
                e, input_items, context_window)
            if mapped is e:
                raise
            raise mapped from e

        content = ""
        tool_calls = []
        import json
        for item in response.output:
            if item.type == "message":
                for block in item.content:
                    text = getattr(block, "text", None)
                    if text:
                        content += text
            elif item.type == "function_call":
                tool_calls.append({
                    "id": item.call_id,
                    "name": item.name,
                    "input": json.loads(item.arguments),
                })

        status = response.status
        incomplete_reason = getattr(
            getattr(response, "incomplete_details", None), "reason", None)
        truncated = (status == "incomplete"
                     and incomplete_reason == "max_output_tokens")
        logger.info(f"OpenAI API response: model={model}, input_tokens={response.usage.input_tokens}, output_tokens={response.usage.output_tokens}, status={status}")
        if truncated:
            logger.warning(f"OpenAI response truncated (max_tokens reached): model={model}, output_tokens={response.usage.output_tokens}")

        # #189: OpenAI auto-caches >=1024-token prefixes; cached_tokens is
        # the cached SUBSET of input_tokens (unlike Anthropic's disjoint
        # counters) and is billed at a discount we must account for.
        details = getattr(response.usage, "input_tokens_details", None)
        cached_tokens = getattr(details, "cached_tokens", 0) or 0
        if cached_tokens:
            logger.info(
                f"OpenAI prompt cache: cached={cached_tokens} of "
                f"{response.usage.input_tokens} input tokens")

        return {
            "content": content,
            "total_tokens": response.usage.total_tokens,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cached_tokens": cached_tokens,
            "tool_calls": tool_calls,
            "truncated": truncated,
        }

    @staticmethod
    def _openai_overflow_error(e, input_items, context_window):
        """
        Map an OpenAI BadRequestError to PromptTooLongError when it is a
        context overflow; otherwise return the original error to re-raise.

        The Responses API's overflow error may carry no token counts
        (unlike chat completions), so when the message has none we fall
        back to a chars/4 estimate of the input and the configured context
        window — reduce_export_tokens only needs a sensible ratio.
        """
        error_msg = str(e)
        match = re.search(
            r'maximum context length is (\d+) tokens.*?resulted in (\d+) tokens',
            error_msg
        )
        if match:
            return PromptTooLongError(
                int(match.group(2)), int(match.group(1)), e)

        code = getattr(e, "code", None)
        overflow = (code == "context_length_exceeded"
                    or "exceeds the context window" in error_msg)
        if overflow and context_window:
            input_chars = sum(
                (len(m["content"]) if isinstance(m["content"], str)
                 else sum(len(b.get("text", "")) for b in m["content"]))
                for m in input_items
            )
            estimated_tokens = max(input_chars // 4, context_window + 1)
            return PromptTooLongError(estimated_tokens, context_window, e)
        return e

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

        # Convert remaining messages to Anthropic format. Content blocks
        # are passed through as-is (#187): block boundaries and any
        # cache_control markers placed upstream must survive — flattening
        # to a string would erase the cache breakpoints.
        anthropic_messages = []
        for msg in messages:
            if msg["role"] in ["user", "assistant"]:
                content = msg["content"]
                if isinstance(content, list) and len(content) > 0:
                    if not (isinstance(content[0], dict)
                            and "text" in content[0]):
                        content = str(content)
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
        total_input_chars = sum(
            (len(m["content"]) if isinstance(m["content"], str)
             else sum(len(b.get("text", "")) for b in m["content"]))
            for m in anthropic_messages
        )
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
        stop_reason = response.stop_reason
        truncated = stop_reason == "max_tokens"
        logger.info(f"Anthropic API response: model={model}, input_tokens={response.usage.input_tokens}, output_tokens={response.usage.output_tokens}, stop_reason={stop_reason}")
        if truncated:
            logger.warning(f"Anthropic response truncated (max_tokens reached): model={model}, output_tokens={response.usage.output_tokens}")

        cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(
            response.usage, "cache_creation_input_tokens", 0) or 0
        if cache_read or cache_write:
            logger.info(
                f"Anthropic prompt cache: read={cache_read} "
                f"write={cache_write} uncached={response.usage.input_tokens}")

        return {
            "content": content,
            "total_tokens": total_tokens,
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_write,
            "tool_calls": tool_calls,
            "truncated": truncated,
        }
