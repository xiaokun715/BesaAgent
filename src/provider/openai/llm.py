"""OpenAI 兼容协议的对话补全实现。

**模板方法**：本类持有公共管线（校验 → 能力守卫 → 构造载荷 → 传输 → 解析），
子类只给四个钩子 —— 这四个钩子**就是**厂商差异的全部：
《架构概要设计-provider》§3.4 的字段映射表逐行对应它们。

============================  ==========================================
钩子                           对应映射表里的差异
============================  ==========================================
``_endpoint()``                端点路径
``_build_payload()``           请求体字段
``_parse()``                   响应体字段
``_delta_content()``           流式增量字段
============================  ==========================================

**为什么 ``chat`` 是具体方法、子类不改它**：管线里的每一步顺序都是有理由的
（校验必须在发请求之前、能力守卫必须在构造载荷之前），
让子类覆写 ``chat`` 就等于允许子类把顺序改错。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from provider.base import ChatModel, guard_request, validate_messages
from provider.errors import ProtocolError
from provider.types import (
    Capability,
    ChatRequest,
    ChatResponse,
    ImagePart,
    Message,
    TextPart,
    ToolCall,
    Usage,
    message_text,
)

__all__ = ["OpenAIChatModel"]

_log = logging.getLogger(__name__)

#: OpenAI 兼容端点普遍支持的能力。**不包含 ``VISION``** ——
#: 是否支持图片取决于具体模型，把它放进默认集会静默放行不该发的请求。
DEFAULT_CAPABILITIES: frozenset[Capability] = frozenset(
    {Capability.CHAT, Capability.STREAM, Capability.TOOLS, Capability.JSON}
)


class OpenAIChatModel(ChatModel):
    """OpenAI 兼容形状的对话模型。"""

    @classmethod
    def default_capabilities(cls) -> frozenset[Capability]:
        return DEFAULT_CAPABILITIES

    # ------------------------------------------------------------------ 钩子
    def _endpoint(self) -> str:
        return "/chat/completions"

    def _build_payload(self, req: ChatRequest, *, stream: bool) -> dict[str, Any]:
        """构造请求体。子类覆写本方法以适配非 OpenAI 形状的厂商。"""
        cfg = self.config
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [self._encode_message(m) for m in req.messages],
            "temperature": cfg.temperature if req.temperature is None else req.temperature,
        }

        max_tokens = req.max_tokens or cfg.max_tokens
        if max_tokens:
            payload["max_tokens"] = max_tokens

        if req.tools:
            payload["tools"] = [self._encode_tool(t) for t in req.tools]
            payload["tool_choice"] = req.tool_choice or "auto"

        if req.stop:
            payload["stop"] = list(req.stop)

        response_format = self._response_format(req)
        if response_format is not None:
            payload["response_format"] = response_format

        if stream:
            payload["stream"] = True

        payload.update(cfg.extra)
        return payload

    def _parse(
        self,
        body: Mapping[str, Any],
        req: ChatRequest,
        *,
        structured_native: bool,
    ) -> ChatResponse:
        try:
            choice = body["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as exc:
            raise ProtocolError(
                f"响应缺少 choices[0].message：{str(body)[:200]}",
                provider=self.provider_name,
                model=self.model_name,
                trace_id=req.trace_id,
            ) from exc

        # 只取 content。推理模型另给 reasoning_content —— 那是思考过程不是答案，
        # 混进来会污染下游结构化解析（FR-P-05）。
        raw_content = message.get("content")
        text = "" if raw_content is None else str(raw_content)
        finish_reason = str(choice.get("finish_reason") or "")

        # 空正文 + 被长度截断 = 极可能思考把 max_tokens 吃光了。
        #
        # 用 WARNING 而不是 DEBUG 是刻意的：这个症状在业务侧的表现是
        # 「检索命中了，但答案是空的」—— 根因却在 max_tokens 配置上，
        # 中间隔了三层，没有这条日志就是纯靠猜（besa-iv-kb 实测踩过）。
        if not text.strip() and finish_reason == "length":
            _log.warning(
                "返回空 content 且 finish_reason=length：max_tokens=%s 大概率被推理模型的"
                "思考过程吃光（思考也计入 max_tokens）。请调大 max_tokens。model=%s trace=%s",
                self.config.max_tokens,
                self.model_name,
                req.trace_id,
            )

        return ChatResponse(
            content=text,
            model=str(body.get("model") or self.model_name),
            finish_reason=finish_reason,
            reasoning=self._parse_reasoning(message),
            tool_calls=self._parse_tool_calls(message, req),
            usage=self._parse_usage(body.get("usage")),
            structured_native=structured_native,
            raw=body if isinstance(body, dict) else dict(body),
            trace_id=req.trace_id,
        )

    def _delta_content(self, line: str) -> str:
        """从一行 SSE 取增量正文。非数据行 / ``[DONE]`` / 空 delta → 空串。"""
        stripped = line.strip()
        if not stripped.startswith("data:"):
            return ""
        data = stripped[len("data:") :].strip()
        if not data or data == "[DONE]":
            return ""
        try:
            chunk = json.loads(data)
        except ValueError:
            # 单个坏分片不该毁掉整个流 —— 丢弃并继续。
            # 但**不静默**：坏分片往往意味着上游协议变了。
            _log.debug("跳过无法解析的 SSE 分片：%s", data[:120])
            return ""
        try:
            delta = chunk["choices"][0].get("delta") or {}
        except (KeyError, IndexError, TypeError):
            return ""
        # 同样只取 content：推理模型的思考增量在 delta 里但字段名不同（FR-P-05）
        content = delta.get("content")
        return "" if content is None else str(content)

    # ------------------------------------------------------------------ 编码
    def _encode_message(self, message: Message) -> dict[str, Any]:
        content = message.content
        if isinstance(content, str):
            encoded: Any = content
        else:
            encoded = [self._encode_part(part) for part in content]

        payload: dict[str, Any] = {"role": message.role, "content": encoded}
        if message.name:
            payload["name"] = message.name
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            payload["tool_calls"] = [self._encode_tool_call(c) for c in message.tool_calls]
            # assistant 消息只带工具调用时，content 必须是 null 而不是空串 ——
            # 部分厂商对空串会返回 400。
            if not message_text(message).strip():
                payload["content"] = None
        return payload

    def _encode_part(self, part: TextPart | ImagePart) -> dict[str, Any]:
        if isinstance(part, TextPart):
            return {"type": "text", "text": part.text}
        image_url: dict[str, Any] = {
            "url": part.data_uri if part.data else (part.url or "")
        }
        if part.detail:
            image_url["detail"] = part.detail
        return {"type": "image_url", "image_url": image_url}

    def _encode_tool(self, tool: Any) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.parameters) or {"type": "object", "properties": {}},
            },
        }

    def _encode_tool_call(self, call: ToolCall) -> dict[str, Any]:
        # 回填历史消息时必须给**原始串**：重新序列化会改变键的顺序与空白，
        # 部分厂商据此判定「工具调用被篡改」并拒绝。
        arguments = call.arguments_raw or json.dumps(
            dict(call.arguments), ensure_ascii=False
        )
        return {
            "id": call.id,
            "type": "function",
            "function": {"name": call.name, "arguments": arguments},
        }

    # ------------------------------------------------------------------ 结构化输出
    def _response_format(self, req: ChatRequest) -> dict[str, Any] | None:
        """把契约里的简化取值映射成厂商的 ``response_format``。

        **不支持时返回 ``None`` = 降级**（``FR-P-04``）：结果仍然可用，只是约束弱了。
        调用方通过 ``ChatResponse.structured_native=False`` 得知这一点。
        ``json_schema`` 缺 schema 的情况已在 ``ChatRequest`` 构造期报错，这里不再判。
        """
        fmt = req.response_format
        if fmt is None or fmt == "text":
            return None
        if not self.config.supports_json_native:
            _log.debug("端点声明不支持原生结构化输出，本次降级为无约束生成")
            return None
        if fmt == "json_schema":
            return {
                "type": "json_schema",
                "json_schema": {
                    "name": "response",
                    "schema": dict(req.json_schema or {}),
                    "strict": True,
                },
            }
        if self.config.uses_json_object_mode:
            return {"type": "json_object"}
        return None

    # ------------------------------------------------------------------ 解析辅助
    def _parse_reasoning(self, message: Mapping[str, Any]) -> str | None:
        """推理模型的思考过程。**独立字段，不进 content**（FR-P-05）。"""
        for key in ("reasoning_content", "reasoning"):
            value = message.get(key)
            if value:
                return str(value)
        return None

    def _parse_tool_calls(
        self, message: Mapping[str, Any], req: ChatRequest
    ) -> tuple[ToolCall, ...]:
        raw_calls = message.get("tool_calls")
        if not raw_calls:
            return ()

        calls: list[ToolCall] = []
        for index, raw in enumerate(raw_calls):
            try:
                function = raw.get("function") or {}
                arguments_raw = str(function.get("arguments") or "")
            except (AttributeError, TypeError):
                _log.warning("跳过无法解析的 tool_call：%s", str(raw)[:120])
                continue

            arguments: dict[str, Any] = {}
            if arguments_raw.strip():
                try:
                    parsed = json.loads(arguments_raw)
                    if isinstance(parsed, dict):
                        arguments = parsed
                    else:
                        _log.warning(
                            "tool_call 参数不是 JSON 对象（第 %d 个）：%s",
                            index, arguments_raw[:120],
                        )
                except ValueError:
                    # 模型幻觉出非法 JSON：**保留原始串**，让调用方能报出有价值的错误。
                    # 丢掉它的话，上游只能看到「参数为空」，完全无从下手。
                    _log.warning(
                        "tool_call 参数不是合法 JSON（第 %d 个）：%s", index, arguments_raw[:120]
                    )

            calls.append(
                ToolCall(
                    id=str(raw.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments_raw=arguments_raw,
                    arguments=arguments,
                )
            )
        return tuple(calls)

    def _parse_usage(self, usage: Any) -> Usage:
        """解析用量。**字段缺失一律留 ``None``，绝不补 0**（FR-P-10）。"""
        if not isinstance(usage, Mapping):
            return Usage()

        def _int(value: Any) -> int | None:
            if value is None or value == "":
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        cached = None
        details = usage.get("prompt_tokens_details")
        if isinstance(details, Mapping):
            cached = _int(details.get("cached_tokens"))
        if cached is None:
            # DeepSeek 用另一个字段名，语义相同
            cached = _int(usage.get("prompt_cache_hit_tokens"))

        return Usage(
            input_tokens=_int(usage.get("prompt_tokens")),
            output_tokens=_int(usage.get("completion_tokens")),
            cached_input_tokens=cached,
        )

    # ------------------------------------------------------------------ 公共管线
    async def chat(self, req: ChatRequest) -> ChatResponse:
        """生成一次完整回复。"""
        validate_messages(req.messages, model=self.model_name)
        guard_request(
            req, self.capabilities(), model=self.model_name, provider=self.provider_name
        )

        payload = self._build_payload(req, stream=False)
        structured_native = "response_format" in payload

        body = await self._client.post_json(
            self._endpoint(), payload, timeout_s=req.timeout_s, trace_id=req.trace_id
        )
        return self._parse(body, req, structured_native=structured_native)

    def stream_chat(self, req: ChatRequest) -> AsyncIterator[str]:
        """逐段产出增量正文。

        **校验在这里同步执行**，而不是留给生成器 —— 见 ``base.Client.stream_sse``。
        这让「请求了不支持流式的模型」在**调用瞬间**抛错，而不是等消费端第一次迭代。
        """
        validate_messages(req.messages, model=self.model_name)
        guard_request(
            req, self.capabilities(), model=self.model_name, provider=self.provider_name
        )

        stream_req = req
        if req.stream is not True:
            from dataclasses import replace

            stream_req = replace(req, stream=True)

        payload = self._build_payload(stream_req, stream=True)
        return self._stream(payload, stream_req)

    async def _stream(
        self, payload: Mapping[str, Any], req: ChatRequest
    ) -> AsyncIterator[str]:
        async for line in self._client.stream_sse(
            self._endpoint(), payload, timeout_s=req.timeout_s, trace_id=req.trace_id
        ):
            piece = self._delta_content(line)
            if piece:
                yield piece
