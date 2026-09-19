"""``src/provider`` 的验收标准测试 —— 逐条对应《需求说明书-provider》§8 的 A-1 ~ A-14。

**这个文件的意义**：需求文档里的每条「验收点」在这里都有一个可执行的对应物。
文档与代码一旦分叉，跑一次测试就知道是谁错了。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest

from provider import build_provider
from provider.errors import (
    AuthError,
    CapabilityNotSupportedError,
    NetworkError,
    ProtocolError,
    UpstreamError,
)
from provider.types import (
    Capability,
    ChatRequest,
    ImagePart,
    Message,
    TextPart,
    ToolSpec,
)

from .conftest import CHAT_BODY, OPENAI_ENV, CountingHandler, HangingTransport

# --------------------------------------------------------------------------- #
# A-1 / A-2 —— 换厂商零改动；无网络无密钥可跑通
# --------------------------------------------------------------------------- #


async def test_a1_same_code_across_vendors(ok_handler):
    """A-1：同一段调用代码在 mock / openai / vllm 之间切换，**代码零改动**。"""
    providers = [
        build_provider({"provider": "mock", "model": "mock-llm"}),
        build_provider(
            {"provider": "openai", "model": "gpt-4o-mini"},
            transport=httpx.MockTransport(ok_handler),
            env=OPENAI_ENV,
        ),
        build_provider(
            {"provider": "vllm", "model": "Qwen", "base_url": "http://127.0.0.1:8000/v1"},
            transport=httpx.MockTransport(ok_handler),
            env={},
        ),
    ]

    results = []
    for provider in providers:                      # ← 这段循环体内没有一处厂商分支
        model = provider.chat_model()
        response = await model.chat(ChatRequest(messages=[Message.text("user", "你好")]))
        results.append(response.content)

    assert len(results) == 3
    assert all(isinstance(text, str) for text in results)


async def test_a2_mock_works_without_network_or_key():
    """A-2：无网络、无密钥环境全链路跑通（CI 的前提）。"""
    provider = build_provider({"provider": "mock", "model": "mock-llm"}, env={})

    response = await provider.chat_model().chat(
        ChatRequest(messages=[Message.text("user", "hi")])
    )
    assert response.content

    pieces = [
        piece
        async for piece in provider.chat_model().stream_chat(
            ChatRequest(messages=[Message.text("user", "hi")])
        )
    ]
    assert pieces

    embedded = await provider.embedding_model().embed(["a", "b"])
    assert len(embedded.vectors) == 2


# --------------------------------------------------------------------------- #
# A-3 / A-4 —— 凭据
# --------------------------------------------------------------------------- #


async def test_a3_vllm_works_without_key_and_sends_no_auth_header(ok_handler):
    """A-3：本地端点无密钥可调用，且**不发** ``Authorization`` 头（FR-P-12）。"""
    handler = CountingHandler([httpx.Response(200, json=CHAT_BODY)])
    provider = build_provider(
        {"provider": "vllm", "model": "Qwen", "base_url": "http://127.0.0.1:8000/v1"},
        transport=httpx.MockTransport(handler),
        env={},
    )

    await provider.chat_model().chat(ChatRequest(messages=[Message.text("user", "hi")]))

    assert handler.count == 1
    # 关键：不是「空 Bearer」，而是**完全没有这个头**
    assert "authorization" not in {
        key.lower() for key in handler.calls[0].headers.keys()
    }


def test_a4_cloud_vendor_without_key_fails_at_construction():
    """A-4：云厂商缺密钥 → **构造期**报错，不等第一次调用（FR-P-12）。"""
    with pytest.raises(AuthError) as excinfo:
        build_provider({"provider": "openai", "model": "gpt-4o-mini"}, env={})

    # 错误信息要能直接指向修法，而不是只说「缺密钥」
    assert "OPENAI_API_KEY" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# A-5 / A-6 —— 重试边界（D-1 落地）
# --------------------------------------------------------------------------- #


async def test_a5_401_does_not_retry(make_openai):
    """A-5：401 立即失败，**不重试** —— 重试只是白烧配额。"""
    handler = CountingHandler([httpx.Response(401, json={"error": "bad key"})])
    provider = make_openai(handler)

    with pytest.raises(AuthError) as excinfo:
        await provider.chat_model().chat(ChatRequest(messages=[Message.text("user", "hi")]))

    assert handler.count == 1
    assert excinfo.value.retryable is False


async def test_a6_provider_does_not_retry_http_5xx(make_openai):
    """A-6：**HTTP 5xx 由 provider 上抛，不由 provider 重试**。

    ⚠️ 这条与《需求说明书-provider》§8 原文措辞不一致：原文写的是
    「503 两次后成功 → 按退避重试，最终成功」。但《架构概要设计-provider》§4.2
    把重试边界改成了 **provider 只重试网络层错误，HTTP 状态码错误一律上抛**，
    理由是厂商故障需要**跨模型**决策（换模型往往比锤同一个更有用），
    且 provider 与 gateway 的重试会相乘 → 重试风暴。

    架构文档是更晚、更具体且带理由的决策，以它为准。本测试锁定的是**架构文档**的行为。
    上抛后的重试与降级由 ``src/gateway`` 负责（见 ``架构概要设计-gateway`` §4.4）。
    """
    handler = CountingHandler(
        [httpx.Response(503, text="upstream down"), httpx.Response(200, json=CHAT_BODY)]
    )
    provider = make_openai(handler)

    with pytest.raises(UpstreamError) as excinfo:
        await provider.chat_model().chat(ChatRequest(messages=[Message.text("user", "hi")]))

    assert handler.count == 1, "provider 不得重试 5xx"
    assert excinfo.value.retryable is True, "但它必须标记为可重试，供 gateway 决策"


async def test_network_error_is_retried_with_backoff(make_openai, clock):
    """网络层错误**由 provider 重试**（D-1 的另一半），退避走假时钟。"""
    handler = CountingHandler(
        [
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            httpx.Response(200, json=CHAT_BODY),
        ]
    )
    provider = make_openai(handler, clock=clock)

    response = await provider.chat_model().chat(
        ChatRequest(messages=[Message.text("user", "hi")])
    )

    assert response.content == "你好"
    assert handler.count == 3
    assert clock.sleeps == [1.0, 2.0], "指数退避"


async def test_network_error_exhausts_retries_and_reports(make_openai, clock):
    handler = CountingHandler([httpx.ConnectError("down")])
    provider = make_openai(handler, clock=clock)

    with pytest.raises(NetworkError) as excinfo:
        await provider.chat_model().chat(ChatRequest(messages=[Message.text("user", "hi")]))

    assert handler.count == 3, "1 次首发 + 2 次重试"
    assert excinfo.value.retryable is True


# --------------------------------------------------------------------------- #
# A-7 / A-8 —— 推理模型
# --------------------------------------------------------------------------- #


async def test_a7_empty_content_with_length_emits_warning(make_openai, caplog):
    """A-7：空正文 + ``finish_reason=length`` → 产生**明确告警**（FR-P-05）。

    用 WARNING 而非 DEBUG：这个症状在业务侧表现为「检索命中了，答案是空的」，
    根因却在 ``max_tokens`` 上，中间隔了三层。
    """
    body = {
        "model": "deepseek-v4-pro",
        "choices": [
            {"message": {"content": "", "reasoning_content": "很长的思考"}, "finish_reason": "length"}
        ],
    }
    provider = make_openai(lambda request: httpx.Response(200, json=body))

    with caplog.at_level(logging.WARNING, logger="provider.openai.llm"):
        response = await provider.chat_model().chat(
            ChatRequest(messages=[Message.text("user", "hi")])
        )

    assert response.content == ""
    assert any("max_tokens" in record.message for record in caplog.records)


async def test_a8_reasoning_does_not_leak_into_content(make_openai):
    """A-8：``reasoning_content`` 走独立字段，**不混进** ``content``（FR-P-05）。"""
    body = {
        "model": "deepseek-v4-pro",
        "choices": [
            {
                "message": {"content": "正确答案", "reasoning_content": "长篇思考过程"},
                "finish_reason": "stop",
            }
        ],
    }
    provider = make_openai(lambda request: httpx.Response(200, json=body))

    response = await provider.chat_model().chat(
        ChatRequest(messages=[Message.text("user", "hi")])
    )

    assert response.content == "正确答案"
    assert "思考" not in response.content
    assert response.reasoning == "长篇思考过程"


# --------------------------------------------------------------------------- #
# A-9 —— 能力拦截（零网络调用）
# --------------------------------------------------------------------------- #


async def test_a9_missing_capability_is_blocked_locally():
    """A-9：请求了未声明能力 → 本地报错，**零网络调用**（FR-P-08）。"""
    handler = CountingHandler([httpx.Response(200, json=CHAT_BODY)])
    provider = build_provider(
        {"provider": "vllm", "model": "Qwen", "base_url": "http://127.0.0.1:8000/v1"},
        transport=httpx.MockTransport(handler),
        env={},
    )

    with pytest.raises(CapabilityNotSupportedError) as excinfo:
        provider.chat_model().stream_chat(
            ChatRequest(messages=[Message.text("user", "x")], tools=[ToolSpec("f")])
        )

    assert handler.count == 0, "不得发生网络调用"
    assert "tools" in str(excinfo.value)


async def test_vllm_has_no_embedding_capability():
    """``vllm/`` 没有 ``embedding.py`` —— 走错路必须**立刻暴露**，而不是返回 ``None``。"""
    provider = build_provider(
        {"provider": "vllm", "model": "Qwen", "base_url": "http://127.0.0.1:8000/v1"}, env={}
    )
    with pytest.raises(CapabilityNotSupportedError):
        provider.embedding_model()


async def test_capabilities_are_overridable_from_config():
    """FR-P-08：vLLM 的能力**必须能从配置覆盖**（自建服务的能力由部署决定）。"""
    provider = build_provider(
        {
            "provider": "vllm",
            "model": "Qwen",
            "base_url": "http://127.0.0.1:8000/v1",
            "capabilities": ["runtime", "stream", "tools"],
        },
        env={},
    )
    assert provider.supports(Capability.TOOLS)


def test_capability_list_replaces_and_mapping_overlays():
    """**列表 = 完整声明（替换）；映射 = 增量（叠加在厂商默认之上）**。

    这条语义差异是必需的，不是风格选择：``vllm`` 的默认能力只有 ``runtime`` + ``stream``，
    用户想再加 ``tools`` 时写 ``{tools: true}`` —— 若按「替换」解释，
    他会意外丢掉 ``runtime``，得到一个连对话都不支持、且报错完全指不到配置的模型。
    """
    base = {"provider": "vllm", "model": "Qwen", "base_url": "http://127.0.0.1:8000/v1"}

    replaced = build_provider({**base, "capabilities": ["tools"]}, env={})
    assert replaced.capabilities() == frozenset({Capability.TOOLS}), "列表是完整声明"

    overlaid = build_provider({**base, "capabilities": {"tools": True}}, env={})
    assert overlaid.supports(Capability.TOOLS), "映射加上了 tools"
    assert overlaid.supports(Capability.CHAT), "且**没有**丢掉默认的 runtime"
    assert overlaid.supports(Capability.STREAM)

    removed = build_provider({**base, "capabilities": {"stream": False}}, env={})
    assert not removed.supports(Capability.STREAM)
    assert removed.supports(Capability.CHAT)


# --------------------------------------------------------------------------- #
# A-10 —— 批量向量化的部分失败
# --------------------------------------------------------------------------- #


async def test_a10_partial_embedding_failure_reports_offset():
    """A-10：批量中某一批失败 → 整批失败并**标明失败下标**（FR-P-07）。

    返回短一截的结果会让第 7 条的向量配到第 8 条文本上 —— 而且**不会报任何错**。
    """

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        count = len(payload["input"])
        if count == 1:                       # 第二批（只有 1 条）
            return httpx.Response(500, text="embedding backend down")
        return httpx.Response(
            200,
            json={
                "model": "text-embedding-v3",
                "data": [{"index": i, "embedding": [0.1, 0.2, 0.3, 0.4]} for i in range(count)],
            },
        )

    provider = build_provider(
        {
            "provider": "openai",
            "model": "text-embedding-v3",
            "base_url": "https://api.openai.com/v1",
            "dimension": 4,
            "max_batch": 2,
        },
        transport=httpx.MockTransport(handler),
        env=OPENAI_ENV,
    )

    with pytest.raises(UpstreamError) as excinfo:
        await provider.embedding_model().embed(["a", "b", "c"])

    assert "起始下标 2" in str(excinfo.value)


async def test_embedding_dimension_mismatch_is_rejected():
    """维度不符必须立刻报错 —— 它要到很久以后的向量库写入才暴露（FR-P-07）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"model": "m", "data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        )

    provider = build_provider(
        {
            "provider": "openai",
            "model": "emb",
            "base_url": "https://api.openai.com/v1",
            "dimension": 1024,
        },
        transport=httpx.MockTransport(handler),
        env=OPENAI_ENV,
    )

    with pytest.raises(ProtocolError) as excinfo:
        await provider.embedding_model().embed(["a"])

    assert "vector(N)" in str(excinfo.value), "错误信息要指出与向量库的对齐关系"


# --------------------------------------------------------------------------- #
# A-11 —— 取消传播
# --------------------------------------------------------------------------- #


async def test_a11_cancellation_propagates():
    """A-11：取消必须**原样传播** ``CancelledError``，不得被包装成普通错误（FR-P-14）。"""
    provider = build_provider(
        {"provider": "openai", "model": "gpt-4o-mini"},
        transport=HangingTransport(),
        env=OPENAI_ENV,
    )
    model = provider.chat_model()

    task = asyncio.create_task(
        model.chat(ChatRequest(messages=[Message.text("user", "hi")]))
    )
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# --------------------------------------------------------------------------- #
# A-12 —— 密钥不泄漏
# --------------------------------------------------------------------------- #


async def test_a12_api_key_never_appears_in_errors(make_openai):
    """A-12：错误信息与 ``repr`` 中**不得出现 API Key**（NFR-P-04）。"""
    key = OPENAI_ENV["OPENAI_API_KEY"]
    handler = CountingHandler(
        [httpx.Response(401, json={"error": f"invalid api key {key}"})]
    )
    provider = make_openai(handler)

    with pytest.raises(AuthError) as excinfo:
        await provider.chat_model().chat(ChatRequest(messages=[Message.text("user", "hi")]))

    error = excinfo.value
    assert key not in str(error)
    assert key not in repr(error)
    assert key not in (error.raw or "")
    # 但厂商原始报文（脱敏后）要保留 —— 它是新错误类型的唯一线索
    assert error.raw


def test_repr_never_contains_key():
    """``repr`` 会进日志与调试器，是最常见的泄漏路径。"""
    provider = build_provider(
        {"provider": "openai", "model": "gpt-4o-mini"}, env=OPENAI_ENV
    )
    assert OPENAI_ENV["OPENAI_API_KEY"] not in repr(provider)
    assert OPENAI_ENV["OPENAI_API_KEY"] not in repr(provider.client())


# --------------------------------------------------------------------------- #
# A-13 —— 工具调用往返
# --------------------------------------------------------------------------- #


async def test_a13_tool_call_round_trip():
    """A-13：完整的一轮「请求工具 → 回填结果 → 最终答复」（FR-P-03）。"""
    steps = [
        httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "function": {"name": "get_weather", "arguments": '{"city":"北京"}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        ),
        httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [{"message": {"content": "北京晴"}, "finish_reason": "stop"}],
            },
        ),
    ]
    handler = CountingHandler(steps)
    provider = build_provider(
        {
            "provider": "openai",
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "capabilities": {"tools": True},
        },
        transport=httpx.MockTransport(handler),
        env=OPENAI_ENV,
    )
    model = provider.chat_model()

    first = await model.chat(
        ChatRequest(
            messages=[Message.text("user", "北京天气？")],
            tools=[ToolSpec("get_weather", "查天气", {"type": "object", "properties": {}})],
        )
    )
    assert first.finish_reason == "tool_calls"
    assert first.tool_calls[0].name == "get_weather"
    assert dict(first.tool_calls[0].arguments) == {"city": "北京"}

    # 回填工具结果：注意 tool_call_id 必须原样带上，否则上游无法对应
    second = await model.chat(
        ChatRequest(
            messages=[
                Message.text("user", "北京天气？"),
                Message(role="assistant", content="", tool_calls=first.tool_calls),
                Message.tool_result("call_1", "晴，25℃"),
            ]
        )
    )
    assert second.content == "北京晴"


async def test_invalid_tool_arguments_keep_raw_string():
    """工具参数是**模型幻觉出的非法 JSON** 时，必须保留原始串供排障（FR-P-03）。"""
    body = {
        "model": "m",
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [{"id": "c", "function": {"name": "f", "arguments": "{not json"}}],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    provider = build_provider(
        {"provider": "openai", "model": "m", "capabilities": {"tools": True}},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        env=OPENAI_ENV,
    )

    response = await provider.chat_model().chat(
        ChatRequest(messages=[Message.text("user", "x")])
    )

    assert response.tool_calls[0].arguments_raw == "{not json"
    assert dict(response.tool_calls[0].arguments) == {}


# --------------------------------------------------------------------------- #
# A-14 —— 结构化输出
# --------------------------------------------------------------------------- #


def test_a14_json_schema_without_schema_is_rejected():
    """A-14：要 ``json_schema`` 但不给 schema → 报错，**不静默降级**（FR-P-04）。"""
    with pytest.raises(ValueError) as excinfo:
        ChatRequest(messages=[Message.text("user", "x")], response_format="json_schema")

    assert "json_schema" in str(excinfo.value)


async def test_structured_native_flag_reflects_actual_path(make_openai):
    """``structured_native`` 必须如实反映「这次到底走没走原生约束」（FR-P-04）。"""
    provider = make_openai(lambda request: httpx.Response(200, json=CHAT_BODY))

    native = await provider.chat_model().chat(
        ChatRequest(messages=[Message.text("user", "x")], response_format="json")
    )
    assert native.structured_native is True

    text = await provider.chat_model().chat(
        ChatRequest(messages=[Message.text("user", "x")])
    )
    assert text.structured_native is False


async def test_json_schema_is_forwarded_when_provided(make_openai):
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=CHAT_BODY)

    provider = make_openai(handler)
    await provider.chat_model().chat(
        ChatRequest(
            messages=[Message.text("user", "x")],
            response_format="json_schema",
            json_schema={"type": "object", "properties": {"a": {"type": "integer"}}},
        )
    )

    assert sent["response_format"]["type"] == "json_schema"
    assert sent["response_format"]["json_schema"]["strict"] is True


# --------------------------------------------------------------------------- #
# 多模态编码
# --------------------------------------------------------------------------- #


async def test_image_part_is_encoded_for_vision_models():
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=CHAT_BODY)

    provider = build_provider(
        {
            "provider": "openai",
            "model": "gpt-4o",
            "base_url": "https://api.openai.com/v1",
            "capabilities": ["runtime", "vision"],
        },
        transport=httpx.MockTransport(handler),
        env=OPENAI_ENV,
    )

    await provider.chat_model().chat(
        ChatRequest(
            messages=[
                Message("user", (TextPart("描述这张图"), ImagePart(url="https://x/y.png")))
            ]
        )
    )

    content = sent["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "描述这张图"}
    assert content[1]["image_url"]["url"] == "https://x/y.png"


async def test_base64_image_becomes_data_uri():
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(200, json=CHAT_BODY)

    provider = build_provider(
        {
            "provider": "openai",
            "model": "gpt-4o",
            "base_url": "https://api.openai.com/v1",
            "capabilities": ["runtime", "vision"],
        },
        transport=httpx.MockTransport(handler),
        env=OPENAI_ENV,
    )

    await provider.chat_model().chat(
        ChatRequest(messages=[Message("user", (ImagePart(data="AAAA", media_type="image/jpeg"),))])
    )

    url = sent["messages"][0]["content"][0]["image_url"]["url"]
    assert url == "data:image/jpeg;base64,AAAA"
