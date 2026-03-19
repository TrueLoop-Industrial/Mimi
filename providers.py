"""
LLM provider abstraction — Claude, Groq, and OpenAI/Codex behind one interface.

Each provider implements the same agentic loop contract:
  send(system, messages, tools) -> LLMResponse

Provider selection is per-task via config, so you can use Groq for
fast planning/review and Claude or Codex for heavy code generation.
"""

import json
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any

# ── Provider base ────────────────────────────────────────────


@dataclass
class LLMResponse:
    """Normalized response from any provider."""
    content: list[Any]     # list of content blocks (text / tool_use)
    stop_reason: str       # "end_turn" or "tool_use"
    input_tokens: int
    output_tokens: int


class BaseProvider:
    """Interface all providers implement."""

    def send(self, system: str, messages: list, tools: list) -> LLMResponse:
        raise NotImplementedError


# ── Anthropic (Claude) ───────────────────────────────────────


class ClaudeProvider(BaseProvider):
    def __init__(self, model: str = "claude-sonnet-4-20250514") -> None:
        import anthropic
        self.client = anthropic.Anthropic()  # ANTHROPIC_API_KEY env var
        self.model = model

    def send(self, system: str, messages: list, tools: list) -> LLMResponse:
        import anthropic
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=8096,
                system=system,
                tools=tools,
                messages=messages,
            )
            return LLMResponse(
                content=resp.content,
                stop_reason=resp.stop_reason,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            )
        except anthropic.APIError as e:
            raise ProviderError(f"Claude API error: {e}") from e


# ── Shared OpenAI-compatible conversion layer ────────────────


class _OpenAICompatibleProvider(BaseProvider):
    """
    Base for providers that speak the OpenAI chat-completions format
    (Groq, OpenAI). Provides shared tool-schema and message conversion.
    """

    def _convert_tools(self, tools: list) -> list[dict]:
        """Convert Anthropic tool format to OpenAI function-calling format."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                },
            }
            for t in tools
        ]

    def _convert_messages(self, system: str, messages: list) -> list[dict]:
        """Convert Anthropic message format to OpenAI chat format."""
        out: list[dict] = [{"role": "system", "content": system}]

        for msg in messages:
            if msg["role"] == "user":
                if isinstance(msg["content"], str):
                    out.append({"role": "user", "content": msg["content"]})
                elif isinstance(msg["content"], list):
                    for block in msg["content"]:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            out.append({
                                "role": "tool",
                                "tool_call_id": block["tool_use_id"],
                                "content": str(block.get("content", "")),
                            })
                        else:
                            out.append({"role": "user", "content": str(block)})

            elif msg["role"] == "assistant":
                content_parts = (
                    msg["content"] if isinstance(msg["content"], list) else [msg["content"]]
                )
                text_parts: list[str] = []
                tool_calls: list[dict] = []

                for part in content_parts:
                    if hasattr(part, "type"):
                        if part.type == "text":
                            text_parts.append(part.text)
                        elif part.type == "tool_use":
                            tool_calls.append({
                                "id": part.id,
                                "type": "function",
                                "function": {
                                    "name": part.name,
                                    "arguments": json.dumps(part.input),
                                },
                            })
                    elif isinstance(part, str):
                        text_parts.append(part)

                # content must be explicitly None (not absent) when tool_calls are
                # present and there is no text, so Groq/OpenAI accept the message.
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": "\n".join(text_parts) if text_parts else None,
                }
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                out.append(assistant_msg)

        return out

    def _normalize_response(self, choice: Any) -> list:
        """Convert an OpenAI-compatible choice to Anthropic-style content blocks."""
        blocks: list[Any] = []

        if choice.message.content:
            blocks.append(_TextBlock(choice.message.content))

        if choice.message.tool_calls:
            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except json.JSONDecodeError:
                    # Malformed JSON from the model — surface as an empty dict so
                    # the agentic loop can recover rather than crashing the batch.
                    args = {}
                blocks.append(_ToolUseBlock(
                    id=tc.id,
                    name=tc.function.name,
                    input=args,
                ))

        return blocks

    def _parse_hermes_calls(self, failed_generation: str) -> list:
        """
        Llama models sometimes emit their native Hermes function-call format:
            <function=tool_name{"arg": "value"}</function>
        instead of OpenAI-style tool_calls. Groq rejects this with a 400 and
        puts the raw generation in the error body's 'failed_generation' field.
        This method parses that format so the agentic loop can continue.
        """
        # Llama emits many Hermes separator variants between the name and JSON args:
        #   <function=NAME{...}</function>       no separator
        #   <function=NAME={...}</function>      = separator
        #   <function=NAME[]{...}</function>     [] separator
        #   <function=NAME {...}></function>     space + > before close
        # Strategy: extract each <function=PAYLOAD</function> block, find the first {,
        # everything before it is the raw name token (strip =, [], whitespace),
        # everything from { onward (minus trailing >) is the JSON args.
        blocks: list[Any] = []
        for payload in re.findall(r"<function=(.*?)</function>", failed_generation, re.DOTALL):
            brace_pos = payload.find("{")
            if brace_pos == -1:
                continue
            raw_name = payload[:brace_pos].rstrip("= \t\r\n[]")
            name = re.sub(r"[^A-Za-z0-9_\-]", "", raw_name)
            args_str = payload[brace_pos:].rstrip(" \t\r\n>")
            try:
                args = json.loads(args_str)
            except json.JSONDecodeError:
                args = {}
            blocks.append(_ToolUseBlock(
                id=f"call_{uuid.uuid4().hex[:8]}",
                name=name,
                input=args,
            ))
        return blocks


# ── Groq ─────────────────────────────────────────────────────


class GroqProvider(_OpenAICompatibleProvider):
    """
    Groq for fast routing/review tasks.
    Uses OpenAI-compatible API with Anthropic-style tool schema conversion.
    Llama 3.3 70B: ~$0.59/M input, $0.79/M output tokens.
    """

    def __init__(self, model: str = "llama-3.3-70b-versatile") -> None:
        from groq import Groq
        self.client = Groq()  # GROQ_API_KEY env var
        self.model = model

    def send(self, system: str, messages: list, tools: list) -> LLMResponse:
        from groq import GroqError
        try:
            oai_messages = self._convert_messages(system, messages)
            oai_tools = self._convert_tools(tools) if tools else None

            kwargs: dict = {
                "model": self.model,
                "messages": oai_messages,
                "max_tokens": 8096,
                "service_tier": "auto",  # uses flex on paid plans (10x higher rate limits)
            }
            if oai_tools:
                kwargs["tools"] = oai_tools
                kwargs["tool_choice"] = "auto"
                kwargs["parallel_tool_calls"] = False  # keeps Llama in OpenAI-compat mode

            resp = self.client.chat.completions.create(**kwargs)

            choice = resp.choices[0]
            content = self._normalize_response(choice)
            stop = "tool_use" if choice.finish_reason == "tool_calls" else "end_turn"

            return LLMResponse(
                content=content,
                stop_reason=stop,
                input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
                output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            )
        except GroqError as e:
            # Llama models sometimes emit their native Hermes format instead of
            # OpenAI tool_calls. Groq rejects this as a 400 with failed_generation.
            # Parse it and return a synthetic response so the loop can continue.
            failed_gen: str = ""
            if hasattr(e, "body") and isinstance(e.body, dict):
                failed_gen = (e.body.get("error") or {}).get("failed_generation", "")
            if failed_gen:
                blocks = self._parse_hermes_calls(failed_gen)
                if blocks:
                    return LLMResponse(
                        content=blocks,
                        stop_reason="tool_use",
                        input_tokens=0,
                        output_tokens=0,
                    )
            raise ProviderError(f"Groq API error: {e}") from e


# ── OpenAI / Codex ───────────────────────────────────────────


class OpenAIProvider(_OpenAICompatibleProvider):
    """
    OpenAI for Codex models (gpt-5.3-codex) or GPT-4o.
    Uses the same conversion logic as Groq (OpenAI-compatible).
    """

    def __init__(self, model: str = "gpt-4o") -> None:
        from openai import OpenAI
        self.client = OpenAI()  # OPENAI_API_KEY env var
        self.model = model

    def send(self, system: str, messages: list, tools: list) -> LLMResponse:
        from openai import OpenAIError
        try:
            oai_messages = self._convert_messages(system, messages)
            oai_tools = self._convert_tools(tools) if tools else None

            resp = self.client.chat.completions.create(
                model=self.model,
                messages=oai_messages,
                tools=oai_tools,
                tool_choice="auto" if oai_tools else None,
                max_tokens=8096,
            )

            choice = resp.choices[0]
            content = self._normalize_response(choice)
            stop = "tool_use" if choice.finish_reason == "tool_calls" else "end_turn"

            return LLMResponse(
                content=content,
                stop_reason=stop,
                input_tokens=resp.usage.prompt_tokens if resp.usage else 0,
                output_tokens=resp.usage.completion_tokens if resp.usage else 0,
            )
        except OpenAIError as e:
            raise ProviderError(f"OpenAI API error: {e}") from e


# ── Shared helpers ───────────────────────────────────────────


class _TextBlock:
    """Mimics anthropic TextBlock for Groq/OpenAI responses."""
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _ToolUseBlock:
    """Mimics anthropic ToolUseBlock for Groq/OpenAI responses."""
    def __init__(self, id: str, name: str, input: dict) -> None:  # noqa: A002
        self.type = "tool_use"
        self.id = id
        self.name = name
        self.input = input


class ProviderError(Exception):
    pass


# ── Factory ──────────────────────────────────────────────────


def create_provider(provider_name: str, model: str | None = None) -> BaseProvider:
    """
    Create a provider by name.

    Supported:
        "claude"  → ClaudeProvider  (default: claude-sonnet-4-20250514)
        "groq"    → GroqProvider    (default: llama-3.3-70b-versatile)
        "openai"  → OpenAIProvider  (default: gpt-4o)
        "codex"   → OpenAIProvider  (default: gpt-5.3-codex)
    """
    providers: dict[str, tuple[type, str]] = {
        "claude": (ClaudeProvider, "claude-sonnet-4-20250514"),
        "groq": (GroqProvider, "llama-3.3-70b-versatile"),
        "openai": (OpenAIProvider, "gpt-4o"),
        "codex": (OpenAIProvider, "gpt-5.3-codex"),
    }

    key = provider_name.lower().strip()
    if key not in providers:
        raise ValueError(
            f"Unknown provider '{provider_name}'. "
            f"Available: {', '.join(providers.keys())}"
        )

    cls, default_model = providers[key]
    return cls(model=model or default_model)
