from __future__ import annotations

import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProviderError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    model: str = ""
    api_key: str = ""
    base_url: str = ""


class ConversationProvider(ABC):
    @abstractmethod
    def complete(
        self,
        messages: list[dict[str, str]],
        system: str,
        response_schema: dict[str, Any] | None = None,
    ) -> str | dict:
        raise NotImplementedError


class OpenAIProvider(ConversationProvider):
    def __init__(self, config: ProviderConfig):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ProviderError("OpenAI SDK is not installed") from exc
        api_key = config.api_key or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise ProviderError("OPENAI_API_KEY is not configured")
        kwargs: dict[str, str] = {"api_key": api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self.client = OpenAI(**kwargs)
        self.model = config.model or os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

    def complete(self, messages, system, response_schema=None):
        request_messages = [{"role": "system", "content": system}, *messages]
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": request_messages,
            "temperature": 0.2,
        }
        if response_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "bongo_response",
                    "strict": True,
                    "schema": response_schema,
                },
            }
        try:
            response = self.client.chat.completions.create(**kwargs)
            text = response.choices[0].message.content or ""
            return json.loads(text) if response_schema else text
        except Exception as exc:
            raise ProviderError(f"OpenAI request failed: {exc}") from exc


class AnthropicProvider(ConversationProvider):
    def __init__(self, config: ProviderConfig):
        try:
            from anthropic import Anthropic
        except ImportError as exc:
            raise ProviderError("Anthropic SDK is not installed") from exc
        api_key = config.api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not configured")
        kwargs: dict[str, str] = {"api_key": api_key}
        if config.base_url:
            kwargs["base_url"] = config.base_url
        self.client = Anthropic(**kwargs)
        self.model = config.model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    def complete(self, messages, system, response_schema=None):
        prompt = system
        if response_schema:
            prompt += (
                "\nReturn only JSON matching this JSON Schema. Do not use markdown fences:\n"
                + json.dumps(response_schema, ensure_ascii=False)
            )
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                temperature=0.2,
                system=prompt,
                messages=messages,
            )
            text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
            return _extract_json(text) if response_schema else text
        except Exception as exc:
            raise ProviderError(f"Anthropic request failed: {exc}") from exc


class ClaudeCodeProvider(ConversationProvider):
    """Tool-free Claude Code adapter. Application history remains the source of truth."""

    def __init__(self, config: ProviderConfig, cwd: str | Path | None = None):
        executable = shutil.which("claude")
        if not executable:
            raise ProviderError("Claude Code was not found in PATH")
        self.executable = executable
        self.model = config.model
        self.cwd = Path(cwd or Path.home()).resolve()

    def complete(self, messages, system, response_schema=None):
        transcript = []
        for item in messages:
            label = "用户" if item.get("role") == "user" else "助学伙伴"
            transcript.append(f"{label}: {item.get('content', '')}")
        prompt = "\n\n".join(transcript)
        command = [
            self.executable,
            "--print",
            "--safe-mode",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--tools",
            "",
            "--system-prompt",
            system,
        ]
        if self.model:
            command.extend(["--model", self.model])
        if response_schema:
            command.extend(
                ["--output-format", "json", "--json-schema", json.dumps(response_schema, ensure_ascii=False)]
            )
        command.append(prompt)
        startup = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                creationflags=startup,
                env={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("Claude Code request timed out after 180 seconds") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ProviderError(f"Claude Code failed: {detail[:500]}")
        output = completed.stdout.strip()
        if not response_schema:
            return output
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return _extract_json(output)
        structured = payload.get("structured_output")
        if isinstance(structured, dict):
            return structured
        result = payload.get("result", output)
        return result if isinstance(result, dict) else _extract_json(str(result))


def _extract_json(text: str) -> dict:
    value = text.strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        start = value.find("{")
        end = value.rfind("}")
        if start >= 0 and end > start:
            return json.loads(value[start : end + 1])
        raise ProviderError("The model did not return valid JSON")


def available_providers() -> list[str]:
    values = []
    if shutil.which("claude"):
        values.append("claude-code")
    values.extend(["openai", "anthropic"])
    return values


def build_provider(config: ProviderConfig, cwd: str | Path | None = None) -> ConversationProvider:
    if config.name == "claude-code":
        return ClaudeCodeProvider(config, cwd=cwd)
    if config.name == "openai":
        return OpenAIProvider(config)
    if config.name == "anthropic":
        return AnthropicProvider(config)
    raise ProviderError(f"Unknown provider: {config.name}")
