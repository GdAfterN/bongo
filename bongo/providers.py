from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit


class ProviderError(RuntimeError):
    pass


TRANSIENT_REQUEST_ATTEMPTS = 3
TRANSIENT_RETRY_DELAYS = (1.0, 2.0)


def _is_transient_request_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code is None:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code in {408, 409, 429} or (isinstance(status_code, int) and status_code >= 500):
        return True

    detail = f"{getattr(exc, 'body', '')} {exc}".lower()
    return any(
        marker in detail
        for marker in (
            "upstream_error",
            "overloaded",
            "overload",
            "rate limit",
            "rate_limit",
            "temporarily unavailable",
        )
    )


def _request_with_transient_retry(request, provider_name: str):
    for attempt in range(TRANSIENT_REQUEST_ATTEMPTS):
        try:
            return request()
        except Exception as exc:
            if not _is_transient_request_error(exc):
                raise ProviderError(f"{provider_name} request failed: {exc}") from exc
            if attempt == TRANSIENT_REQUEST_ATTEMPTS - 1:
                raise ProviderError(
                    f"{provider_name} 模型服务暂时繁忙或请求过多，"
                    f"已自动重试 {TRANSIENT_REQUEST_ATTEMPTS} 次，请稍后再试。"
                ) from exc
            time.sleep(TRANSIENT_RETRY_DELAYS[attempt])
    raise ProviderError(f"{provider_name} request failed without a response")


def _stream_with_transient_retry(
    request: Callable[[Callable[[str], None]], None],
    provider_name: str,
    on_delta: Callable[[str], None],
) -> str:
    for attempt in range(TRANSIENT_REQUEST_ATTEMPTS):
        chunks: list[str] = []
        emitted = False

        def emit(delta: str) -> None:
            nonlocal emitted
            text = str(delta)
            if not text:
                return
            emitted = True
            chunks.append(text)
            on_delta(text)

        try:
            request(emit)
        except Exception as exc:
            if emitted:
                if isinstance(exc, ProviderError):
                    raise
                raise ProviderError(f"{provider_name} stream failed after output started: {exc}") from exc
            if isinstance(exc, ProviderError) or not _is_transient_request_error(exc):
                if isinstance(exc, ProviderError):
                    raise
                raise ProviderError(f"{provider_name} request failed: {exc}") from exc
            if attempt == TRANSIENT_REQUEST_ATTEMPTS - 1:
                raise ProviderError(
                    f"{provider_name} 模型服务暂时繁忙或请求过多，"
                    f"已自动重试 {TRANSIENT_REQUEST_ATTEMPTS} 次，请稍后再试。"
                ) from exc
            time.sleep(TRANSIENT_RETRY_DELAYS[attempt])
            continue
        answer = "".join(chunks)
        if not answer.strip():
            raise ProviderError(f"{provider_name} returned empty streamed output")
        return answer
    raise ProviderError(f"{provider_name} request failed without a response")


def _run_jsonl_process(
    command: list[str],
    *,
    cwd: Path,
    input_text: str,
    environment: dict[str, str] | None,
    timeout: int,
    provider_name: str,
    on_payload: Callable[[dict[str, Any]], None],
) -> None:
    startup = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=startup,
            env=environment,
        )
    except OSError as exc:
        raise ProviderError(f"{provider_name} could not be started: {exc}") from exc

    stderr_lines: list[str] = []
    timed_out = threading.Event()

    def drain_stderr() -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            stderr_lines.append(line)
            if len(stderr_lines) > 200:
                del stderr_lines[:100]

    def stop_process() -> None:
        if process.poll() is None:
            timed_out.set()
            try:
                process.kill()
            except OSError:
                pass

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    timeout_timer = threading.Timer(timeout, stop_process)
    timeout_timer.daemon = True
    timeout_timer.start()
    try:
        if process.stdin is None or process.stdout is None:
            raise ProviderError(f"{provider_name} process streams are unavailable")
        process.stdin.write(input_text)
        process.stdin.close()
        for line in process.stdout:
            value = line.strip()
            if not value:
                continue
            try:
                payload = json.loads(value)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                on_payload(payload)
        return_code = process.wait()
    except Exception as exc:
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        process.wait()
        if timed_out.is_set():
            raise ProviderError(f"{provider_name} request timed out after {timeout} seconds") from exc
        if isinstance(exc, ProviderError):
            raise
        raise ProviderError(f"{provider_name} stream failed: {exc}") from exc
    finally:
        timeout_timer.cancel()
        stderr_thread.join(timeout=1)

    if timed_out.is_set():
        raise ProviderError(f"{provider_name} request timed out after {timeout} seconds")
    if return_code != 0:
        detail = "".join(stderr_lines).strip() or f"exit code {return_code}"
        raise ProviderError(f"{provider_name} failed: {detail[:500]}")


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

    def stream_text(
        self,
        messages: list[dict[str, str]],
        system: str,
        on_delta: Callable[[str], None],
    ) -> str:
        result = self.complete(messages, system)
        if isinstance(result, dict):
            raise ProviderError("Provider returned structured output for a text stream")
        text = str(result)
        if text:
            on_delta(text)
        return text


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
            response = _request_with_transient_retry(
                lambda: self.client.responses.create(**kwargs),
                "OpenAI",
            )
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

    def stream_text(self, messages, system, on_delta):
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": messages,
            "max_output_tokens": 8192,
        }

        def request(emit):
            with self.client.responses.stream(**kwargs) as stream:
                for event in stream:
                    if getattr(event, "type", "") == "response.output_text.delta":
                        emit(getattr(event, "delta", ""))

        return _stream_with_transient_retry(request, "OpenAI", on_delta)


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
        def request():
            return self.client.messages.create(
                model=self.model,
                max_tokens=4096,
                temperature=0.2,
                system=prompt,
                messages=messages,
            )

        try:
            response = _request_with_transient_retry(request, "Anthropic")
            text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
            return _extract_json(text) if response_schema else text
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderError(f"Anthropic request failed: {exc}") from exc

    def stream_text(self, messages, system, on_delta):
        def request(emit):
            with self.client.messages.stream(
                model=self.model,
                max_tokens=4096,
                temperature=0.2,
                system=system,
                messages=messages,
            ) as stream:
                for delta in stream.text_stream:
                    emit(delta)

        return _stream_with_transient_retry(request, "Anthropic", on_delta)


class ClaudeCodeProvider(ConversationProvider):
    """Claude Code adapter using the selected workspace as its working directory."""

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
            "--permission-mode",
            "acceptEdits",
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

    def stream_text(self, messages, system, on_delta):
        transcript = []
        for item in messages:
            label = "用户" if item.get("role") == "user" else "助学伙伴"
            transcript.append(f"{label}: {item.get('content', '')}")
        prompt = "\n\n".join(transcript)
        command = [
            self.executable,
            "--print",
            "--permission-mode",
            "acceptEdits",
            "--system-prompt",
            system,
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        if self.model:
            command.extend(["--model", self.model])

        chunks: list[str] = []
        final_result = ""

        def handle_payload(payload: dict[str, Any]) -> None:
            nonlocal final_result
            if payload.get("type") == "stream_event":
                event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
                delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
                if event.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    text = str(delta.get("text") or "")
                    if text:
                        chunks.append(text)
                        on_delta(text)
            elif payload.get("type") == "result":
                final_result = str(payload.get("result") or "")

        _run_jsonl_process(
            command,
            cwd=self.cwd,
            input_text=prompt,
            environment={**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
            timeout=180,
            provider_name="Claude Code",
            on_payload=handle_payload,
        )
        if chunks:
            return "".join(chunks)
        if final_result:
            on_delta(final_result)
            return final_result
        raise ProviderError("Claude Code returned empty streamed output")


class CodexCliProvider(ConversationProvider):
    """Codex CLI adapter using workspace-write inside the selected directory."""

    def __init__(self, config: ProviderConfig, cwd: str | Path | None = None, writable: bool = False):
        executable = shutil.which("codex")
        if not executable:
            raise ProviderError("Codex CLI was not found in PATH")
        self.executable = executable
        self.model = config.model
        self.cwd = Path(cwd or Path.home()).resolve()
        self.writable = writable

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
            "workspace-write" if self.writable else "read-only",
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

    def stream_text(self, messages, system, on_delta):
        transcript = []
        for item in messages:
            label = "用户" if item.get("role") == "user" else "助学伙伴"
            transcript.append(f"{label}: {item.get('content', '')}")
        prompt = f"{system}\n\n" + "\n\n".join(transcript)
        command = [
            self.executable,
            "exec",
            "--json",
            "--sandbox",
            "workspace-write" if self.writable else "read-only",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-C",
            str(self.cwd),
        ]
        if self.model:
            command.extend(["--model", self.model])
        command.append("-")

        chunks: list[str] = []

        def handle_payload(payload: dict[str, Any]) -> None:
            if payload.get("type") != "item.completed":
                return
            item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
            if item.get("type") != "agent_message":
                return
            text = str(item.get("text") or "")
            if text:
                chunks.append(text)
                on_delta(text)

        _run_jsonl_process(
            command,
            cwd=self.cwd,
            input_text=prompt,
            environment=None,
            timeout=180,
            provider_name="Codex CLI",
            on_payload=handle_payload,
        )
        if not chunks:
            raise ProviderError("Codex CLI returned no agent_message events")
        return "".join(chunks)


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
