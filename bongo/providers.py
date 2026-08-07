from __future__ import annotations

import json
import os
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


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
            kwargs["base_url"] = normalize_openai_base_url(config.base_url)
        self.client = OpenAI(**kwargs)
        self.model = config.model or os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")

    def complete(self, messages, system, response_schema=None):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": messages,
            "max_output_tokens": 8192,
        }
        if response_schema:
            kwargs["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "bongo_response",
                    "strict": True,
                    "schema": response_schema,
                }
            }
        for attempt in range(2):
            try:
                response = self.client.responses.create(**kwargs)
            except Exception as exc:
                raise ProviderError(f"OpenAI request failed: {exc}") from exc
            text = (response.output_text or "").strip()
            if not text:
                if attempt == 0:
                    continue
                status = getattr(response, "status", "unknown")
                output_types = ", ".join(
                    str(getattr(item, "type", type(item).__name__))
                    for item in getattr(response, "output", [])
                ) or "none"
                raise ProviderError(
                    f"OpenAI returned empty output (status: {status}, output: {output_types})"
                )
            if not response_schema:
                return text
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                if attempt == 0:
                    continue
                raise ProviderError(f"OpenAI returned invalid structured output: {exc}") from exc
        raise ProviderError("OpenAI request failed without a response")


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
        startup = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                cwd=self.cwd,
                input=prompt,
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
        except OSError as exc:
            raise ProviderError(f"Claude Code could not be started: {exc}") from exc
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


class CodexCliProvider(ConversationProvider):
    """Read-only Codex CLI adapter. Application history remains the source of truth."""

    def __init__(self, config: ProviderConfig, cwd: str | Path | None = None):
        executable = shutil.which("codex")
        if not executable:
            raise ProviderError("Codex CLI was not found in PATH")
        self.executable = executable
        self.model = config.model
        self.cwd = Path(cwd or Path.home()).resolve()

    def complete(self, messages, system, response_schema=None):
        transcript = []
        for item in messages:
            label = "用户" if item.get("role") == "user" else "助学伙伴"
            transcript.append(f"{label}: {item.get('content', '')}")
        prompt = f"{system}\n\n" + "\n\n".join(transcript)
        if response_schema:
            prompt += (
                "\n\nReturn only JSON matching this JSON Schema. Do not use markdown fences:\n"
                + json.dumps(response_schema, ensure_ascii=False)
            )
        command = [
            self.executable,
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-C",
            str(self.cwd),
        ]
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")
        startup = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                command,
                cwd=self.cwd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
                creationflags=startup,
            )
        except subprocess.TimeoutExpired as exc:
            raise ProviderError("Codex CLI request timed out after 180 seconds") from exc
        except OSError as exc:
            raise ProviderError(f"Codex CLI could not be started: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ProviderError(f"Codex CLI failed: {detail[:500]}")
        output = completed.stdout.strip()
        return _extract_json(output) if response_schema else output


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


def normalize_openai_base_url(value: str) -> str:
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.path in {"", "/"}:
        parsed = parsed._replace(path="/v1")
    return urlunsplit(parsed)


def available_providers() -> list[str]:
    return ["openai", "anthropic"]


def available_chat_backends() -> list[str]:
    return ["default", "cc", "codex"]


def normalize_chat_backend(name: str) -> str:
    aliases = {
        "auto": "default",
        "builtin": "default",
        "claude-code": "cc",
    }
    normalized = aliases.get(name, name)
    return normalized if normalized in available_chat_backends() else "default"


def _cli_agent_available(executable_name: str) -> bool:
    executable = shutil.which(executable_name)
    if not executable:
        return False
    startup = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=startup,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def chat_backend_available(name: str) -> bool:
    normalized = normalize_chat_backend(name)
    if normalized == "default":
        return True
    executable = "claude" if normalized == "cc" else "codex"
    return _cli_agent_available(executable)


def resolve_chat_backend(name: str) -> str:
    return normalize_chat_backend(name)


def build_provider(config: ProviderConfig, cwd: str | Path | None = None) -> ConversationProvider:
    if config.name in {"cc", "claude-code"}:
        return ClaudeCodeProvider(config, cwd=cwd)
    if config.name == "codex":
        return CodexCliProvider(config, cwd=cwd)
    if config.name == "openai":
        return OpenAIProvider(config)
    if config.name == "anthropic":
        return AnthropicProvider(config)
    raise ProviderError(f"Unknown provider: {config.name}")
