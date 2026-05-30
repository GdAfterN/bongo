"""模型后端适配层。

runtime 只关心一件事：给我一个 prompt，我拿回一段文本。
不同 provider 在 HTTP 接口、响应结构、是否支持 prompt cache 上都有差异，
这些差异都在这里被抹平成统一的 complete() 接口。
"""

import hashlib
import json
import re
import time
from http.client import RemoteDisconnected
import urllib.error
import urllib.request


# ── 结构化 API 辅助函数 ──────────────────────────────────────

def convert_tools_to_api_schema(tools):
    """将 bongo 的工具定义转换为 API 兼容的 JSON Schema 格式。"""
    api_tools = []
    for name, tool in tools.items():
        properties = {}
        required = []
        param_descs = tool.get("param_descriptions", {})
        for param_name, param_type in tool.get("schema", {}).items():
            prop = _parse_param_type(param_name, param_type)
            if param_name in param_descs:
                prop["description"] = param_descs[param_name]
            properties[param_name] = prop
            if prop.pop("_required", False):
                required.append(param_name)
        api_tools.append({
            "name": name,
            "description": tool.get("description", ""),
            "input_schema": {
                "type": "object",
                "properties": properties,
                **({"required": required} if required else {}),
            },
        })
    return api_tools


def _parse_param_type(param_name, type_str):
    """解析 'str', 'int=1', \"str='.'\" 等类型字符串为 JSON Schema 属性。"""
    type_str = str(type_str).strip()
    has_default = "=" in type_str
    parts = type_str.split("=", 1)
    base_type = parts[0].strip().lower()
    default = None
    if has_default and len(parts) > 1:
        raw = parts[1].strip().strip("'\"")
        if base_type == "int":
            try:
                default = int(raw)
            except ValueError:
                default = raw
        elif base_type == "float":
            try:
                default = float(raw)
            except ValueError:
                default = raw
        else:
            default = raw

    type_map = {"str": "string", "string": "string", "int": "integer", "float": "number", "bool": "boolean"}
    json_type = type_map.get(base_type, "string")
    prop = {"type": json_type}
    if default is not None:
        prop["default"] = default
    if not has_default:
        prop["_required"] = True
    return prop


def convert_history_to_messages(history):
    """将 bongo 的 history 转换为 user/assistant 交替的消息格式。

    history 格式：assistant content 为 list（含 tool_use 块），tool 有 tool_use_id
    """
    messages = []
    pending_tool_results = []

    for item in history:
        role = item.get("role", "")
        content = item.get("content", "")

        if role == "tool":
            # 新格式：直接有 tool_use_id
            tool_use_id = item.get("tool_use_id", "")
            if not tool_use_id:
                # 旧格式兼容：从 name+args 生成 id
                tool_name = item.get("name", "")
                tool_args = item.get("args", {})
                tool_use_id = f"toolu_{hashlib.sha256(json.dumps({'name': tool_name, 'args': tool_args}, sort_keys=True).encode()).hexdigest()[:12]}"
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": str(content)[:4000],
            })
            continue

        if pending_tool_results:
            messages.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

        if role in ("user", "system"):
            text = str(content)
            if messages and messages[-1]["role"] == "user":
                prev = messages[-1]
                if isinstance(prev["content"], list):
                    prev["content"].append({"type": "text", "text": text})
                else:
                    prev["content"] = str(prev["content"]) + "\n" + text
            else:
                messages.append({"role": "user", "content": text})
        elif role == "assistant":
            if isinstance(content, list):
                # 新格式：已经是结构化 list，直接透传
                messages.append({"role": "assistant", "content": content})
            else:
                # 旧格式兼容：纯文本
                messages.append({"role": "assistant", "content": [{"type": "text", "text": str(content)}]})

    if pending_tool_results:
        if messages and messages[-1]["role"] == "user":
            prev = messages[-1]
            if isinstance(prev["content"], list):
                prev["content"].extend(pending_tool_results)
            else:
                messages.append({"role": "user", "content": pending_tool_results})
        else:
            messages.append({"role": "user", "content": pending_tool_results})

    return messages


class FakeModelClient:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.structured_calls = []  # 记录结构化调用参数
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        # 记录结构化参数（如果有）
        if any(k in kwargs for k in ("system", "tools", "messages")):
            self.structured_calls.append({
                "system": kwargs.get("system"),
                "tools": kwargs.get("tools"),
                "messages": kwargs.get("messages"),
            })
        if not getattr(self, "last_completion_metadata", None):
            self.last_completion_metadata = {}
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)


class OllamaModelClient:
    def __init__(self, model, host, temperature, top_p, timeout):
        self.model = model
        self.host = host.rstrip("/")
        self.temperature = temperature
        self.top_p = top_p
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.last_completion_metadata = {}
        system = kwargs.get("system")
        tools = kwargs.get("tools")
        messages = kwargs.get("messages")

        # 优先使用结构化 /api/chat，回退到 /api/generate
        if messages:
            return self._complete_chat(system, tools, messages, max_new_tokens)
        # 无结构化数据时拼接为单条 prompt
        if system:
            prompt = f"{system}\n\n{prompt}"
        return self._complete_generate(prompt, max_new_tokens)

    def _complete_chat(self, system, tools, messages, max_new_tokens):
        ollama_messages = []
        if system:
            ollama_messages.append({"role": "system", "content": system})
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
                tool_results = [p for p in content if isinstance(p, dict) and p.get("type") == "tool_result"]
                if tool_results:
                    for tr in tool_results:
                        ollama_messages.append({
                            "role": "tool",
                            "content": str(tr.get("content", ""))[:4000],
                        })
                elif text_parts:
                    ollama_messages.append({"role": role, "content": "\n".join(text_parts)})
            else:
                ollama_messages.append({"role": role, "content": str(content)})

        payload = {
            "model": self.model,
            "messages": ollama_messages,
            "stream": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        if tools:
            ollama_tools = []
            for t in tools:
                ollama_tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {}),
                    },
                })
            payload["tools"] = ollama_tools

        request = urllib.request.Request(
            self.host + "/api/chat",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        data = self._do_request(request)
        message = data.get("message", {})
        # 优先检查 tool_calls（原生工具调用）
        if tools:
            tool_calls = message.get("tool_calls", [])
            if tool_calls:
                tc = tool_calls[0]
                func = tc.get("function", {})
                return {
                    "type": "tool_use",
                    "id": f"toolu_{hashlib.sha256(json.dumps(func, sort_keys=True).encode()).hexdigest()[:12]}",
                    "name": func.get("name", ""),
                    "input": func.get("arguments", {}),
                }
        return message.get("content", data.get("response", ""))

    def _complete_generate(self, prompt, max_new_tokens):
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "raw": False,
            "think": False,
            "options": {
                "num_predict": max_new_tokens,
                "temperature": self.temperature,
                "top_p": self.top_p,
            },
        }
        request = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        data = self._do_request(request)
        return data.get("response", "")

    def _do_request(self, request):
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                "Could not reach Ollama.\n"
                "Make sure `ollama serve` is running and the model is available.\n"
                f"Host: {self.host}\n"
                f"Model: {self.model}"
            ) from exc


def _normalize_versioned_base_url(base_url):
    base = str(base_url).rstrip("/")
    if not base.endswith("/v1"):
        base += "/v1"
    return base


def _extract_openai_text(data):
    if data.get("output_text"):
        return data["output_text"]

    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict):
                text = content.get("text")
                if text:
                    return text

    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        return text

    return ""


def _extract_openai_tool_call(data):
    """从 OpenAI 响应中提取 tool_call，返回统一格式 dict 或 None。"""
    # Responses API: output items with type "tool_call"
    for item in data.get("output", []):
        if isinstance(item, dict) and item.get("type") == "tool_call":
            args = item.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            return {
                "type": "tool_use",
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "input": args,
            }
    # Chat Completions API: choices[0].message.tool_calls
    choices = data.get("choices", [])
    if choices:
        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls", [])
        if tool_calls:
            tc = tool_calls[0]
            func = tc.get("function", {})
            args = func.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            return {
                "type": "tool_use",
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "input": args,
            }
    return None


def _extract_openai_text_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            continue
        if event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text
        part = event.get("part")
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                return text
        item = event.get("item")
        if isinstance(item, dict):
            text = _extract_openai_text({"output": [item]})
            if text:
                return text
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            text = _extract_openai_text(response)
            if text:
                return text
        text = _extract_openai_text(event)
        if text:
            return text
    if deltas:
        return "".join(deltas)
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response)
    return ""


def _extract_openai_response_from_sse(body_text):
    last_response = None
    deltas = []
    for line in body_text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        response = event.get("response")
        if isinstance(response, dict):
            last_response = response
            if event.get("type") == "response.completed":
                text = _extract_openai_text(response)
                if text:
                    return text, response
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif event_type == "response.output_text.done":
            text = event.get("text")
            if isinstance(text, str) and text:
                return text, last_response or {}
        else:
            text = _extract_openai_text(event)
            if text:
                return text, event
    if deltas:
        return "".join(deltas), last_response or {}
    if isinstance(last_response, dict):
        return _extract_openai_text(last_response), last_response
    return "", {}


def _extract_usage_cache_details(data):
    # 把不同 OpenAI-compatible 返回里的 usage 字段整理成统一结构，
    # 让 runtime/trace/report 不需要关心 provider 细节。
    usage = data.get("usage") or {}
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
    input_details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") or {}
    cached_tokens = int(input_details.get("cached_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cached_tokens,
        "cache_hit": cached_tokens > 0,
    }


class OpenAICompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        # 当前只在明确支持 prompt cache 语义的后端上启用这条链路，
        # 避免对不支持的后端传一个“看起来统一、其实没意义”的伪参数。
        self.supports_prompt_cache = any(host in self.base_url for host in ("openai.com", "right.codes"))
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None, **kwargs):
        self.last_completion_metadata = {}
        system = kwargs.get("system")
        tools = kwargs.get("tools")
        messages = kwargs.get("messages")

        if messages:
            input_messages = self._build_structured_input(system, tools, messages)
        else:
            if system:
                prompt = f"{system}\n\n{prompt}"
            input_messages = [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}]

        payload = {
            "model": self.model,
            "input": input_messages,
            "max_output_tokens": max_new_tokens,
            "stream": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        # runtime 传入的是“稳定前缀”的签名，而不是整段 prompt 的签名。
        # 这样缓存复用针对的是稳定段，不会因为动态 history 每轮变化而失效。
        if self.supports_prompt_cache and prompt_cache_key:
            payload["prompt_cache_key"] = prompt_cache_key
        if self.supports_prompt_cache and prompt_cache_retention:
            payload["prompt_cache_retention"] = prompt_cache_retention

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        request = urllib.request.Request(
            self.base_url + "/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                    headers = getattr(response, "headers", {}) or {}
                    content_type = headers.get("Content-Type", "")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"OpenAI-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the OpenAI-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        # 有些兼容后端返回普通 JSON，有些返回 SSE。
        # 这里两种都接住，并尽量统一抽取文本和 usage/cache 元数据。
        if content_type.startswith("text/event-stream") or body_text.lstrip().startswith("data:"):
            text, response_data = _extract_openai_response_from_sse(body_text)
            if isinstance(response_data, dict) and response_data:
                self.last_completion_metadata = {
                    "prompt_cache_supported": self.supports_prompt_cache,
                    "prompt_cache_key": prompt_cache_key,
                    "prompt_cache_retention": prompt_cache_retention,
                    **_extract_usage_cache_details(response_data),
                }
                # 优先检查 tool_calls
                if tools:
                    tc = _extract_openai_tool_call(response_data)
                    if tc:
                        return tc
            if text:
                return text
            raise RuntimeError("OpenAI-compatible error: could not extract text from event stream response")

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "OpenAI-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"OpenAI-compatible error: {data['error']}")
        self.last_completion_metadata = {
            "prompt_cache_supported": self.supports_prompt_cache,
            "prompt_cache_key": prompt_cache_key,
            "prompt_cache_retention": prompt_cache_retention,
            **_extract_usage_cache_details(data),
        }
        # 优先检查 tool_calls
        if tools:
            tc = _extract_openai_tool_call(data)
            if tc:
                return tc
        return _extract_openai_text(data)

    def _build_structured_input(self, system, tools, messages):
        """将 system/tools/messages 转换为 OpenAI Responses API 的 input 格式。"""
        input_messages = []
        if system:
            input_messages.append({"role": "system", "content": system})
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                openai_content = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type", "")
                    if ptype == "text":
                        openai_content.append({"type": "input_text", "text": part.get("text", "")})
                    elif ptype == "tool_use":
                        openai_content.append({
                            "type": "tool_call",
                            "call_id": part.get("id", ""),
                            "name": part.get("name", ""),
                            "arguments": json.dumps(part.get("input", {}), ensure_ascii=False),
                        })
                    elif ptype == "tool_result":
                        openai_content.append({
                            "type": "tool_result",
                            "call_id": part.get("tool_use_id", ""),
                            "output": str(part.get("content", ""))[:4000],
                        })
                    elif ptype == "input_text":
                        openai_content.append(part)
                if openai_content:
                    input_messages.append({"role": role, "content": openai_content})
            else:
                mapped_role = "system" if role == "system" else role
                input_messages.append({"role": mapped_role, "content": [{"type": "input_text", "text": str(content)}]})
        return input_messages


def _extract_anthropic_response(data, has_tools=False):
    """从 Anthropic 响应中提取结果。

    has_tools=True 时优先返回 tool_use 块（dict），否则返回文本（str）。
    """
    if has_tools:
        for item in data.get("content", []):
            if isinstance(item, dict) and item.get("type") == "tool_use":
                return {
                    "type": "tool_use",
                    "id": item.get("id", ""),
                    "name": item.get("name", ""),
                    "input": item.get("input", {}),
                }
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str) and text:
                return text
    # Some models (e.g. mimo) may return only thinking content without text.
    for item in data.get("content", []):
        if isinstance(item, dict) and item.get("type") == "thinking":
            text = item.get("thinking")
            if isinstance(text, str) and text:
                return text
    return ""


class AnthropicCompatibleModelClient:
    def __init__(self, model, base_url, api_key, temperature, timeout):
        self.model = model
        self.base_url = _normalize_versioned_base_url(base_url)
        self.api_key = api_key
        self.temperature = temperature
        self.timeout = timeout
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, prompt_cache_key=None, prompt_cache_retention=None, **kwargs):
        del prompt_cache_key, prompt_cache_retention
        self.last_completion_metadata = {}
        system = kwargs.get("system")
        tools = kwargs.get("tools")
        messages = kwargs.get("messages")

        if messages:
            api_messages = self._build_anthropic_messages(messages)
        else:
            if system:
                prompt = f"{system}\n\n{prompt}"
            api_messages = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]

        payload = {
            "model": self.model,
            "messages": api_messages,
            "max_tokens": max_new_tokens,
            "stream": False,
        }
        if system and messages:
            payload["system"] = system
        if tools:
            payload["tools"] = tools
        if self.temperature is not None:
            payload["temperature"] = self.temperature

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        request = urllib.request.Request(
            self.base_url + "/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        attempts = 3
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body_text = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                if exc.code >= 500 and attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(f"Anthropic-compatible request failed with HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, RemoteDisconnected) as exc:
                if attempt < attempts - 1:
                    time.sleep(0.5 * (attempt + 1))
                    continue
                raise RuntimeError(
                    "Could not reach the Anthropic-compatible backend.\n"
                    f"Base URL: {self.base_url}\n"
                    f"Model: {self.model}"
                ) from exc

        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Anthropic-compatible error: backend returned non-JSON content that could not be parsed"
            ) from exc
        if data.get("error"):
            raise RuntimeError(f"Anthropic-compatible error: {data['error']}")
        result = _extract_anthropic_response(data, has_tools=bool(tools))
        if result:
            return result
        content_summary = [
            f"type={item.get('type')},len={len(item.get('text', '') or item.get('thinking', ''))}"
            for item in data.get("content", [])
        ]
        raise RuntimeError(
            f"Anthropic-compatible error: could not extract text from response. "
            f"content=[{', '.join(content_summary)}], stop_reason={data.get('stop_reason')}"
        )

    def _build_anthropic_messages(self, messages):
        """将通用消息格式转换为 Anthropic Messages API 格式。"""
        api_messages = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                api_content = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type", "")
                    if ptype == "text":
                        api_content.append({"type": "text", "text": part.get("text", "")})
                    elif ptype == "tool_use":
                        api_content.append({
                            "type": "tool_use",
                            "id": part.get("id", ""),
                            "name": part.get("name", ""),
                            "input": part.get("input", {}),
                        })
                    elif ptype == "tool_result":
                        api_content.append({
                            "type": "tool_result",
                            "tool_use_id": part.get("tool_use_id", ""),
                            "content": str(part.get("content", ""))[:4000],
                        })
                if api_content:
                    api_messages.append({"role": role, "content": api_content})
            else:
                api_messages.append({"role": role, "content": str(content)})
        return api_messages
