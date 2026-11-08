from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import urlparse

import httpx

from .config import Settings
from .models import AgentSpec, LLMResponse, RunState, TaskState, ToolCall, new_id
from .providers import (
    build_headers,
    join_endpoint,
    mask_url,
    network_hint,
    normalize_base_url,
)


logger = logging.getLogger("ai_cluster.llm")


class LLMError(RuntimeError):
    def __init__(self, message: str, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


# Called with (text, kind) where kind is "content" or "reasoning".
DeltaCallback = Callable[[str, str], Awaitable[None]]


def estimate_tokens(value: Any) -> int:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False)
    return max(1, len(text) // 3) if text else 0


class LLMClient:
    """Provider-agnostic client with a deterministic mock fallback.

    Supports three API styles:
      - openai_chat:      POST /chat/completions
      - openai_responses: POST /responses
      - anthropic:        POST /messages

    The active provider is resolved from the database on every call, so the
    console can switch provider / model without restarting the server.
    """

    def __init__(
        self,
        settings: Settings,
        provider_source: Callable[[str | None], dict[str, Any] | None] | None = None,
    ) -> None:
        self.settings = settings
        self._provider_source = provider_source

    def resolve(self, provider_id: str | None = None) -> dict[str, Any] | None:
        if self._provider_source is not None:
            provider = self._provider_source(provider_id)
            if provider:
                style = provider.get("api_style") or "openai_chat"
                try:
                    base_url = normalize_base_url(provider["base_url"], style)
                except ValueError:
                    base_url = provider["base_url"].rstrip("/")
                return {
                    "name": provider.get("name", "provider"),
                    "base_url": base_url,
                    "api_key": provider.get("api_key", ""),
                    "model": provider["model"],
                    "api_style": style,
                    "auth_style": provider.get("auth_style") or "bearer",
                    "extra_headers": provider.get("extra_headers") or "{}",
                    "proxy": provider.get("proxy") or "",
                    "reasoning_effort": provider.get("reasoning_effort") or "",
                    "timeout": int(provider.get("timeout") or 0),
                    "stream": bool(provider.get("stream", 1)),
                }
        # Legacy fallback: configuration from mvp/.env
        if self.settings.llm_base_url:
            parsed = urlparse(self.settings.llm_base_url)
            local = parsed.hostname in ("127.0.0.1", "localhost", "::1")
            if self.settings.llm_api_key or local:
                return {
                    "name": "env",
                    "base_url": self.settings.llm_base_url.rstrip("/"),
                    "api_key": self.settings.llm_api_key,
                    "model": self.settings.llm_model,
                    "api_style": "openai_chat",
                    "auth_style": "bearer",
                    "extra_headers": "{}",
                    "proxy": "",
                    "reasoning_effort": "",
                    "timeout": 0,
                    "stream": True,
                }
        return None

    @property
    def is_mock(self) -> bool:
        return self.resolve() is None

    @property
    def model_name(self) -> str:
        config = self.resolve()
        return config["model"] if config else "mock"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        *,
        task: TaskState | None = None,
        run: RunState | None = None,
        agent: AgentSpec | None = None,
        step: int = 0,
        observations: list[tuple[str, dict[str, Any], str]] | None = None,
        provider_id: str | None = None,
        on_delta: DeltaCallback | None = None,
    ) -> LLMResponse:
        config = self.resolve(provider_id)
        if config is None:
            result = self._mock(task, run, agent, step, observations or [])
            await self._emit(on_delta, result.content)
            return result
        config["session_id"] = run.id if run is not None else new_id("sess")
        style = config.get("api_style", "openai_chat")
        if style == "openai_responses":
            return await self._responses(messages, tools, config, on_delta)
        if style == "anthropic":
            result = await self._anthropic(messages, tools, config)
            await self._emit(on_delta, result.content)
            return result
        return await self._chat(messages, tools, config, on_delta)

    @staticmethod
    async def _emit(
        on_delta: DeltaCallback | None, text: str, kind: str = "content"
    ) -> None:
        if not on_delta or not text:
            return
        try:
            await on_delta(text, kind)
        except Exception:  # noqa: BLE001 - streaming must never break a run
            logger.debug("delta callback failed", exc_info=True)

    async def _post(
        self,
        url: str,
        payload: dict[str, Any],
        config: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            headers = build_headers(
                config.get("api_key", ""),
                config.get("auth_style", "bearer"),
                config.get("extra_headers", "{}"),
                config.get("api_style", "openai_chat"),
                base_url=config.get("base_url", ""),
                session_id=config.get("session_id", ""),
            )
        except ValueError as exc:
            raise LLMError(f"供应商配置错误（{config['name']}）: {exc}") from exc
        try:
            timeout = int(config.get("timeout") or 0) or self.settings.llm_timeout
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=20, read=timeout, write=60, pool=20
                ),
                proxy=config.get("proxy") or None,
            ) as client:
                resp = await client.post(url, headers=headers, json=payload)
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            self._log_network_error(url, config, exc)
            raise LLMError(
                f"LLM 请求失败（{config['name']}）\n"
                f"URL: {mask_url(url)}\n"
                f"错误: {type(exc).__name__}: {exc}\n"
                f"建议: {network_hint(exc)}",
                retryable=True,
            ) from exc
        if resp.status_code >= 400:
            self._raise_status(resp.status_code, resp.text[:1000], config)
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"LLM 响应不是合法 JSON（{config['name']}）", retryable=True
            ) from exc

    def _log_network_error(
        self, url: str, config: dict[str, Any], exc: BaseException
    ) -> None:
        logger.warning(
            "LLM network error provider=%s url=%s type=%s detail=%s",
            config.get("name"),
            mask_url(url),
            type(exc).__name__,
            exc,
        )

    def _raise_status(
        self, status: int, body: str, config: dict[str, Any]
    ) -> None:
        retryable = status in (408, 429) or status >= 500
        hint = ""
        if status == 524:
            hint = (
                "\n建议: 上游/Cloudflare 超时。开启「流式输出」、降低思考强度、"
                "换更快的模型，或让供应商提高网关超时。"
            )
        elif status in (502, 503, 504):
            hint = "\n建议: 供应商网关异常，稍后重试；开启流式输出可能缓解。"
        raise LLMError(
            f"LLM 返回 {status}（{config['name']}）: {body[:600]}{hint}",
            retryable=retryable,
        )

    async def _sse(
        self, response: httpx.Response
    ) -> AsyncIterator[tuple[str | None, str]]:
        event: str | None = None
        async for raw in response.aiter_lines():
            line = raw.rstrip("\r")
            if not line:
                event = None
                continue
            if line.startswith("event:"):
                event = line[6:].strip()
                continue
            if line.startswith("data:"):
                yield event, line[5:].strip()

    async def _chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        config: dict[str, Any],
        on_delta: DeltaCallback | None = None,
    ) -> LLMResponse:
        if config.get("stream", True):
            return await self._chat_stream(messages, tools, config, on_delta)
        result = await self._chat_once(messages, tools, config)
        await self._emit(on_delta, result.content)
        return result

    async def _chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        config: dict[str, Any],
        on_delta: DeltaCallback | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": config["model"] or self.settings.llm_model,
            "messages": messages,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        effort = config.get("reasoning_effort") or ""
        if effort:
            payload["reasoning_effort"] = effort
        url = join_endpoint(config["base_url"], "chat/completions")
        headers = build_headers(
            config.get("api_key", ""),
            config.get("auth_style", "bearer"),
            config.get("extra_headers", "{}"),
            config.get("api_style", "openai_chat"),
            base_url=config.get("base_url", ""),
            session_id=config.get("session_id", ""),
        )
        headers["Accept"] = "text/event-stream"
        parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        timeout = int(config.get("timeout") or 0) or self.settings.llm_timeout
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20, read=timeout, write=60, pool=20),
                proxy=config.get("proxy") or None,
            ) as client:
                async with client.stream(
                    "POST", url, headers=headers, json=payload
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        self._raise_status(response.status_code, body, config)
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/event-stream" not in content_type:
                        raw = await response.aread()
                        if "application/json" in content_type:
                            try:
                                result = self._chat_result(json.loads(raw))
                            except (json.JSONDecodeError, LLMError):
                                pass
                            else:
                                await self._emit(on_delta, result.content)
                                return result
                        raise LLMError(
                            f"供应商未返回 SSE 流（{config['name']}，{content_type or '未知类型'}）",
                            retryable=False,
                        )
                    async for _event, data in self._sse(response):
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            reasoning = (
                                delta.get("reasoning_content")
                                or delta.get("reasoning")
                                or ""
                            )
                            if isinstance(reasoning, str) and reasoning:
                                await self._emit(on_delta, reasoning, "reasoning")
                            if delta.get("content"):
                                parts.append(delta["content"])
                                await self._emit(on_delta, delta["content"])
                            for call in delta.get("tool_calls") or []:
                                index = int(call.get("index", 0))
                                slot = calls.setdefault(
                                    index,
                                    {"id": None, "name": "", "arguments": ""},
                                )
                                if call.get("id"):
                                    slot["id"] = call["id"]
                                fn = call.get("function") or {}
                                if fn.get("name"):
                                    slot["name"] += fn["name"]
                                if fn.get("arguments"):
                                    slot["arguments"] += fn["arguments"]
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            self._log_network_error(url, config, exc)
            raise LLMError(
                f"LLM 请求失败（{config['name']}）\n"
                f"URL: {mask_url(url)}\n"
                f"错误: {type(exc).__name__}: {exc}\n"
                f"建议: {network_hint(exc)}",
                retryable=True,
            ) from exc

        content = "".join(parts)
        tool_calls: list[ToolCall] = []
        for slot in [calls[key] for key in sorted(calls)]:
            raw_args = slot.get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {"_raw": raw_args}
            tool_calls.append(
                ToolCall(
                    id=slot.get("id") or new_id("call"),
                    name=slot.get("name", ""),
                    arguments=args,
                )
            )
        prompt_tokens = int(
            usage.get("prompt_tokens") or estimate_tokens(messages)
        )
        completion_tokens = int(
            usage.get("completion_tokens") or estimate_tokens(content)
        )
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def _chat_result(self, data: dict[str, Any]) -> LLMResponse:
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise LLMError(f"LLM 响应格式异常: {data}") from exc

        calls: list[ToolCall] = []
        for item in message.get("tool_calls") or []:
            fn = item.get("function", {})
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {"_raw": raw_args}
            calls.append(
                ToolCall(
                    id=item.get("id") or new_id("call"),
                    name=fn.get("name", ""),
                    arguments=args,
                )
            )

        usage = data.get("usage") or {}
        return LLMResponse(
            content=message.get("content") or "",
            tool_calls=calls,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )

    async def _chat_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        config: dict[str, Any],
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": config["model"] or self.settings.llm_model,
            "messages": messages,
        }
        effort = config.get("reasoning_effort") or ""
        if effort:
            payload["reasoning_effort"] = effort
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        data = await self._post(
            join_endpoint(config["base_url"], "chat/completions"), payload, config
        )
        return self._chat_result(data)

    async def _responses(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        config: dict[str, Any],
        on_delta: DeltaCallback | None = None,
    ) -> LLMResponse:
        if config.get("stream", True):
            return await self._responses_stream(messages, tools, config, on_delta)
        result = await self._responses_once(messages, tools, config)
        await self._emit(on_delta, result.content)
        return result

    def _responses_result(
        self, output: list[dict[str, Any]], usage: dict[str, Any]
    ) -> LLMResponse:
        texts: list[str] = []
        calls: list[ToolCall] = []
        for item in output:
            item_type = item.get("type")
            if item_type == "message":
                for block in item.get("content") or []:
                    if block.get("type") in ("output_text", "text"):
                        texts.append(block.get("text", ""))
            elif item_type == "output_text":
                texts.append(item.get("text", ""))
            elif item_type in ("function_call", "tool_call"):
                raw_args = item.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {"_raw": raw_args}
                calls.append(
                    ToolCall(
                        id=item.get("call_id") or item.get("id") or new_id("call"),
                        name=item.get("name", ""),
                        arguments=args,
                    )
                )
        return LLMResponse(
            content="\n".join(text for text in texts if text),
            tool_calls=calls,
            prompt_tokens=int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0),
            completion_tokens=int(
                usage.get("output_tokens") or usage.get("completion_tokens") or 0
            ),
        )

    async def _responses_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        config: dict[str, Any],
        on_delta: DeltaCallback | None = None,
    ) -> LLMResponse:
        system_parts: list[str] = []
        items: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                if message.get("content"):
                    system_parts.append(message["content"])
            elif role == "user":
                items.append({"role": "user", "content": message.get("content", "")})
            elif role == "assistant":
                if message.get("content"):
                    items.append({"role": "assistant", "content": message["content"]})
                for call in message.get("tool_calls") or []:
                    fn = call.get("function", {})
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id"),
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", "{}"),
                        }
                    )
            elif role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.get("tool_call_id"),
                        "output": message.get("content", ""),
                    }
                )

        payload: dict[str, Any] = {
            "model": config["model"] or self.settings.llm_model,
            "input": items,
            "stream": True,
        }
        if system_parts:
            payload["instructions"] = "\n\n".join(system_parts)
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.get("function", {}).get("name", ""),
                    "description": tool.get("function", {}).get("description", ""),
                    "parameters": tool.get("function", {}).get("parameters")
                    or {"type": "object", "properties": {}},
                }
                for tool in tools
            ]
        effort = config.get("reasoning_effort") or ""
        if effort:
            payload["reasoning"] = {"effort": effort}

        url = join_endpoint(config["base_url"], "responses")
        headers = build_headers(
            config.get("api_key", ""),
            config.get("auth_style", "bearer"),
            config.get("extra_headers", "{}"),
            config.get("api_style", "openai_chat"),
            base_url=config.get("base_url", ""),
            session_id=config.get("session_id", ""),
        )
        headers["Accept"] = "text/event-stream"
        texts: list[str] = []
        calls: dict[str, dict[str, Any]] = {}
        usage: dict[str, Any] = {}
        completed_output: list[dict[str, Any]] | None = None
        timeout = int(config.get("timeout") or 0) or self.settings.llm_timeout

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=20, read=timeout, write=60, pool=20),
                proxy=config.get("proxy") or None,
            ) as client:
                async with client.stream(
                    "POST", url, headers=headers, json=payload
                ) as response:
                    if response.status_code >= 400:
                        body = (await response.aread()).decode("utf-8", "replace")
                        self._raise_status(response.status_code, body, config)
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/event-stream" not in content_type:
                        raw = await response.aread()
                        if "application/json" in content_type:
                            try:
                                data = json.loads(raw)
                                return self._responses_result(
                                    data.get("output") or [],
                                    data.get("usage") or {},
                                )
                            except (json.JSONDecodeError, LLMError):
                                pass
                        raise LLMError(
                            f"供应商未返回 SSE 流（{config['name']}，{content_type or '未知类型'}）",
                            retryable=False,
                        )
                    async for event, data in self._sse(response):
                        if data == "[DONE]":
                            break
                        try:
                            obj = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        event_type = obj.get("type") or event or ""
                        if event_type == "response.output_text.delta":
                            piece = obj.get("delta", "")
                            if piece:
                                texts.append(piece)
                                await self._emit(on_delta, piece)
                        elif event_type in (
                            "response.reasoning_summary_text.delta",
                            "response.reasoning_text.delta",
                        ):
                            piece = obj.get("delta", "")
                            if piece:
                                await self._emit(on_delta, piece, "reasoning")
                        elif event_type == "response.function_call_arguments.delta":
                            key = obj.get("item_id") or obj.get("call_id") or "0"
                            slot = calls.setdefault(
                                key, {"id": None, "name": "", "arguments": ""}
                            )
                            slot["arguments"] += obj.get("delta", "")
                        elif event_type == "response.output_item.added":
                            item = obj.get("item") or {}
                            if item.get("type") == "function_call":
                                key = item.get("id") or item.get("call_id") or "0"
                                slot = calls.setdefault(
                                    key, {"id": None, "name": "", "arguments": ""}
                                )
                                if item.get("call_id"):
                                    slot["id"] = item["call_id"]
                                if item.get("name"):
                                    slot["name"] = item["name"]
                                if item.get("arguments"):
                                    slot["arguments"] = item["arguments"]
                        elif event_type == "response.output_item.done":
                            item = obj.get("item") or {}
                            if item.get("type") == "function_call":
                                key = item.get("id") or item.get("call_id") or "0"
                                slot = calls.setdefault(
                                    key, {"id": None, "name": "", "arguments": ""}
                                )
                                if item.get("call_id"):
                                    slot["id"] = item["call_id"]
                                if item.get("name"):
                                    slot["name"] = item["name"]
                                if item.get("arguments"):
                                    slot["arguments"] = item["arguments"]
                        elif event_type == "response.completed":
                            final = obj.get("response") or {}
                            usage = final.get("usage") or usage
                            output = final.get("output") or []
                            if output:
                                completed_output = output
                            break
                        elif event_type in ("error", "response.failed"):
                            message = (
                                obj.get("message")
                                or (obj.get("error") or {}).get("message")
                                or obj
                            )
                            raise LLMError(
                                f"LLM 流式返回错误（{config['name']}）: {message}",
                                retryable=True,
                            )
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            self._log_network_error(url, config, exc)
            raise LLMError(
                f"LLM 请求失败（{config['name']}）\n"
                f"URL: {mask_url(url)}\n"
                f"错误: {type(exc).__name__}: {exc}\n"
                f"建议: {network_hint(exc)}",
                retryable=True,
            ) from exc

        if completed_output:
            return self._responses_result(completed_output, usage)

        tool_calls: list[ToolCall] = []
        for slot in calls.values():
            raw_args = slot.get("arguments") or "{}"
            try:
                args = json.loads(raw_args)
            except json.JSONDecodeError:
                args = {"_raw": raw_args}
            tool_calls.append(
                ToolCall(
                    id=slot.get("id") or new_id("call"),
                    name=slot.get("name", ""),
                    arguments=args,
                )
            )
        content = "".join(texts)
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=int(usage.get("input_tokens") or estimate_tokens(messages)),
            completion_tokens=int(
                usage.get("output_tokens") or estimate_tokens(content)
            ),
        )

    async def _responses_once(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        config: dict[str, Any],
    ) -> LLMResponse:
        system_parts: list[str] = []
        items: list[dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                if message.get("content"):
                    system_parts.append(message["content"])
            elif role == "user":
                items.append({"role": "user", "content": message.get("content", "")})
            elif role == "assistant":
                if message.get("content"):
                    items.append({"role": "assistant", "content": message["content"]})
                for call in message.get("tool_calls") or []:
                    fn = call.get("function", {})
                    items.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id"),
                            "name": fn.get("name", ""),
                            "arguments": fn.get("arguments", "{}"),
                        }
                    )
            elif role == "tool":
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.get("tool_call_id"),
                        "output": message.get("content", ""),
                    }
                )

        payload: dict[str, Any] = {
            "model": config["model"] or self.settings.llm_model,
            "input": items,
        }
        if system_parts:
            payload["instructions"] = "\n\n".join(system_parts)
        effort = config.get("reasoning_effort") or ""
        if effort:
            payload["reasoning"] = {"effort": effort}
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.get("function", {}).get("name", ""),
                    "description": tool.get("function", {}).get("description", ""),
                    "parameters": tool.get("function", {}).get("parameters")
                    or {"type": "object", "properties": {}},
                }
                for tool in tools
            ]

        data = await self._post(
            join_endpoint(config["base_url"], "responses"), payload, config
        )
        return self._responses_result(
            data.get("output") or [], data.get("usage") or {}
        )

    async def _anthropic(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        config: dict[str, Any],
    ) -> LLMResponse:
        system_parts: list[str] = []
        conversation: list[dict[str, Any]] = []
        last_was_tool_result = False

        for message in messages:
            role = message.get("role")
            if role == "system":
                if message.get("content"):
                    system_parts.append(message["content"])
                continue
            if role == "user":
                conversation.append(
                    {"role": "user", "content": message.get("content", "")}
                )
                last_was_tool_result = False
            elif role == "assistant":
                blocks: list[dict[str, Any]] = []
                if message.get("content"):
                    blocks.append({"type": "text", "text": message["content"]})
                for call in message.get("tool_calls") or []:
                    fn = call.get("function", {})
                    raw_args = fn.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = {"_raw": raw_args}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id"),
                            "name": fn.get("name", ""),
                            "input": args if isinstance(args, dict) else {},
                        }
                    )
                if blocks:
                    conversation.append({"role": "assistant", "content": blocks})
                last_was_tool_result = False
            elif role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": message.get("tool_call_id"),
                    "content": message.get("content", ""),
                }
                if (
                    last_was_tool_result
                    and conversation
                    and conversation[-1].get("role") == "user"
                ):
                    conversation[-1]["content"].append(block)
                else:
                    conversation.append({"role": "user", "content": [block]})
                last_was_tool_result = True

        payload: dict[str, Any] = {
            "model": config["model"] or self.settings.llm_model,
            "max_tokens": 4096,
            "messages": conversation,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if tools:
            payload["tools"] = [
                {
                    "name": tool.get("function", {}).get("name", ""),
                    "description": tool.get("function", {}).get("description", ""),
                    "input_schema": tool.get("function", {}).get("parameters")
                    or {"type": "object", "properties": {}},
                }
                for tool in tools
            ]

        data = await self._post(
            join_endpoint(config["base_url"], "messages"), payload, config
        )
        texts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                texts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                args = block.get("input") or {}
                calls.append(
                    ToolCall(
                        id=block.get("id") or new_id("call"),
                        name=block.get("name", ""),
                        arguments=args if isinstance(args, dict) else {},
                    )
                )
        usage = data.get("usage") or {}
        return LLMResponse(
            content="\n".join(text for text in texts if text),
            tool_calls=calls,
            prompt_tokens=int(usage.get("input_tokens", 0)),
            completion_tokens=int(usage.get("output_tokens", 0)),
        )

    # ------------------------------------------------------------------
    # Mock behaviour: enough to exercise the whole pipeline end to end.
    # ------------------------------------------------------------------

    def _mock(
        self,
        task: TaskState | None,
        run: RunState | None,
        agent: AgentSpec | None,
        step: int,
        observations: list[tuple[str, dict[str, Any], str]],
    ) -> LLMResponse:
        if task is None or agent is None:
            return LLMResponse(content="mock", prompt_tokens=10, completion_tokens=2)

        # First step: let agents that have retrieval tools actually call one.
        if step == 0 and "search_knowledge" in agent.tools and task.capability != "code":
            args = {"query": f"{task.title} {task.instruction[:80]}"}
            return LLMResponse(
                tool_calls=[ToolCall(id=new_id("call"), name="search_knowledge", arguments=args)],
                prompt_tokens=180,
                completion_tokens=40,
            )

        evidence = ""
        if observations:
            evidence = observations[-1][2][:800]

        goal = run.goal if run else task.instruction
        content = self._mock_text(agent, task, goal, evidence)
        return LLMResponse(
            content=content,
            prompt_tokens=220 + len(evidence) // 3,
            completion_tokens=len(content) // 2,
        )

    @staticmethod
    def _mock_text(agent: AgentSpec, task: TaskState, goal: str, evidence: str) -> str:
        ev = evidence.strip() or "暂无外部资料，以下判断基于任务上下文。"
        templates = {
            "research": (
                f"【{agent.name} · 调研结果】\n"
                f"主题：{task.title}\n"
                f"1. 关键事实：围绕「{goal}」，已梳理出可执行的信息边界与前置条件。\n"
                f"2. 参考依据：{ev}\n"
                f"3. 待确认项：目标用户、数据来源、交付标准。\n"
            ),
            "analysis": (
                f"【{agent.name} · 分析结论】\n"
                f"基于上游材料，对「{task.title}」给出三点判断：\n"
                f"• 核心机会：把重复性的多步工作交给可编排的 Agent 集群，收益主要来自并行与标准化。\n"
                f"• 主要风险：上下文膨胀、成本失控、失败后难以恢复。\n"
                f"• 建议：先做单机 MVP，用事件日志和预算熔断兜底，再逐步拆出分布式 Worker。\n"
                f"依据：{ev}\n"
            ),
            "writing": (
                f"【{agent.name} · 交付初稿】\n"
                f"# {task.title}\n\n"
                f"目标：{goal}\n\n"
                f"## 方案要点\n"
                f"1. 用 DAG 描述任务，用能力标签解耦「任务」与「执行者」。\n"
                f"2. 所有状态变更走事件总线，保证可回放、可观测、可计费。\n"
                f"3. 先落地控制面、调度器与 Worker 三件套，再扩展工具市场与多租户。\n\n"
                f"## 风险与对策\n"
                f"- 预算熔断、步数上限、死锁检测必须从第一天就有。\n"
                f"- 工具调用统一走网关，默认沙箱、最小权限。\n\n"
                f"参考：{ev}\n"
            ),
            "review": (
                f"【{agent.name} · 审校意见】\n"
                f"- 结构完整性：通过（目标、方案、风险均有覆盖）。\n"
                f"- 事实一致性：与上游结论一致，未发现冲突。\n"
                f"- 可执行性：建议补充验收指标（成功率、P95 时延、单任务成本）。\n"
                f"- 结论：可以进入人工确认环节。\n"
                f"抽查依据：{ev}\n"
            ),
            "code": (
                f"【{agent.name} · 实现方案】\n"
                f"任务：{task.title}\n"
                f"1. 定义 Task/Agent/Event 三个核心数据结构。\n"
                f"2. 用能力标签做路由，用 asyncio.Semaphore 控制并发。\n"
                f"3. 每个 step 落一条事件，失败可重放。\n"
                f"参考：{ev}\n"
            ),
        }
        return templates.get(
            task.capability,
            f"【{agent.name}】已完成「{task.title}」。\n{ev}\n",
        )
