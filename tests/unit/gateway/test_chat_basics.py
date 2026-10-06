"""**基线路径**：一次正常的对话、一次正常的向量化、一条正常的流。

**为什么单独一个文件**：``test_acceptance.py`` 那 20 条覆盖的是 gateway 的
**网关职责**（选谁 / 挂了怎么办 / 花多少 / 还活着吗）—— 那是这个模块真正的难点。
但「一次正常的对话」这条**基线路径**此前只有副产品级覆盖：
成功断言散落在 B-2（换模型）、B-4（降级）、B-13（成本）里，各自服务于别的目的。

**而且它下面挂着的参数透传一处都没测**。证据：把 ``chat()`` 里的
``temperature=temperature`` 改成写死的 ``0.0``，**全量测试仍然全绿** ——
因为 :class:`HostRouter` 那时只记录 host，看不到发出去的内容。

所以这个文件补两件事：

1. 一条真正的 happy path（不掺任何故障）；
2. **透传矩阵**：给 gateway 的参数有没有原样到达上游。
"""

from __future__ import annotations

import json

import httpx
import pytest

from gateway.types import StreamChunk, StreamDone
from provider.types import Message

from .conftest import HostRouter, StreamingTransport, chat_body, embed_body, model_spec

ALIAS = {"runtime.default": {"candidates": ["m1"], "strategy": ["capability"]}}
MODELS = {"m1": model_spec("a")}


# --------------------------------------------------------------------------- #
# 一、对话的基线路径
# --------------------------------------------------------------------------- #


async def test_chat_returns_the_model_content(gateway_factory):
    """**最基本的那件事**：模型说了什么，网关就返回什么。

    没有故障、没有降级、没有重试 —— 只有「发出去、拿回来」。
    在这条用例之前，没有任何一条测试是单纯为这件事写的。
    """
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("你好，我是模型")))
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    result = await gateway.chat("runtime.default", [Message.text("user", "你好")])

    assert result.content == "你好，我是模型"
    # 注意 ``GatewayResult`` **没有 ``ok``**：gateway 的约定是「返回了就是成功，
    # 失败会抛异常」。所以「成功」这件事由「没抛」表达，不需要额外的标志位。
    assert result.degraded is False
    assert result.attempts[-1].outcome == "success"
    assert router.count("a") == 1, "一次成功调用只该打一次上游"


async def test_chat_sends_the_messages_it_was_given(gateway_factory):
    """消息要**原样**送上去（role 与 content 都对）。

    这条测的是「网关只是把你给的东西转手递下去」——
    这类路径不会出错，但一旦出错（漏了一条消息、role 拼错）也不会报错，
    只是模型基于残缺的上下文回答，而排障会从「答案不对」开始，离原因很远。
    """
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("好")))
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    await gateway.chat(
        "runtime.default",
        [Message.text("system", "你是测试助手"), Message.text("user", "设计一个用例")],
    )

    assert router.body()["messages"] == [
        {"role": "system", "content": "你是测试助手"},
        {"role": "user", "content": "设计一个用例"},
    ]


# --------------------------------------------------------------------------- #
# 二、透传矩阵 —— 网关有 11 个关键字参数，此前 6 个一处都没测
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("kwargs", "key", "expected"),
    [
        ({"temperature": 0.7}, "temperature", 0.7),
        ({"max_tokens": 123}, "max_tokens", 123),
        ({"stop": ["END", "STOP"]}, "stop", ["END", "STOP"]),
    ],
)
async def test_chat_forwards_sampling_parameters(gateway_factory, kwargs, key, expected):
    """采样的三个参数要原样到达上游。

    它们此前**一处都没测**：改坏了不会有任何断言变红，
    而症状是「模型行为变了」—— 一个从代码上看不出因果的现象。
    """
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("好")))
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    await gateway.chat("runtime.default", [Message.text("user", "hi")], **kwargs)

    assert router.body()[key] == expected


async def test_temperature_falls_back_to_the_model_config(gateway_factory):
    """不传 temperature 时用**模型配置里的值**，而不是某个写死的默认。

    provider 的规则是 ``配置值 if 请求没给 else 请求值``。
    把这条写反（比如反过来）不会报错，只会让所有「没显式指定温度」的调用
    意外地用上另一个温度 —— 而没人会去查这个。
    """
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("好")))
    gateway = gateway_factory(
        models={"m1": model_spec("a", temperature=0.42)}, aliases=ALIAS, handler=router
    )

    await gateway.chat("runtime.default", [Message.text("user", "hi")])

    assert router.body()["temperature"] == 0.42


async def test_request_temperature_wins_over_the_config(gateway_factory):
    """请求里给了就以请求为准 —— 与上一条是同一个规则的另一面。"""
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("好")))
    gateway = gateway_factory(
        models={"m1": model_spec("a", temperature=0.42)}, aliases=ALIAS, handler=router
    )

    await gateway.chat("runtime.default", [Message.text("user", "hi")], temperature=0.9)

    assert router.body()["temperature"] == 0.9


async def test_chat_forwards_tools(gateway_factory):
    """工具定义要送到上游。

    此前 ``tools=`` 只出现在 B-11 里，而那一条断言的是**能力不足时被拒绝** ——
    也就是说，**成功路径上工具定义有没有真的传下去，从没验证过**。
    这个漏检很实：模型收不到工具就不会调用它，而症状是「模型不调工具」，
    看起来像模型的能力问题。
    """
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("好")))
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    from provider.types import ToolSpec

    await gateway.chat(
        "runtime.default",
        [Message.text("user", "查一下")],
        tools=[ToolSpec(name="search", description="搜索", parameters={"type": "object"})],
        tool_choice="auto",
    )

    body = router.body()
    assert body["tools"][0]["function"]["name"] == "search"
    assert body["tool_choice"] == "auto"


async def test_chat_forwards_json_response_format(gateway_factory):
    """``response_format="json"`` → 厂商的 ``json_object``。"""
    router = HostRouter().on("a", httpx.Response(200, json=chat_body('{"a":1}')))
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    await gateway.chat(
        "runtime.default", [Message.text("user", "给个 JSON")], response_format="json"
    )

    assert router.body()["response_format"] == {"type": "json_object"}


async def test_chat_forwards_json_schema(gateway_factory):
    """``json_schema`` 要与 schema 一起送到上游，且标 ``strict``。

    `FR-P-04` 要求「``json_schema`` 缺 schema 时必须报错」——
    而**把它透传下去**的这个环节此前一行测试都没有。
    """
    schema = {"type": "object", "properties": {"name": {"type": "string"}}}
    router = HostRouter().on("a", httpx.Response(200, json=chat_body('{"name":"x"}')))
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    await gateway.chat(
        "runtime.default",
        [Message.text("user", "给个 JSON")],
        response_format="json_schema",
        json_schema=schema,
    )

    forwarded = router.body()["response_format"]
    assert forwarded["type"] == "json_schema"
    assert forwarded["json_schema"]["schema"] == schema
    assert forwarded["json_schema"]["strict"] is True, "结构化输出必须严格模式，否则约束是弱的"


async def test_text_response_format_sends_nothing(gateway_factory):
    """``response_format="text"`` 是默认值，**不该往上游塞一个多余字段**。

    透传「默认值」会让部分厂商把它当成显式约束，行为与不传不同。
    """
    router = HostRouter().on("a", httpx.Response(200, json=chat_body("好")))
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    await gateway.chat(
        "runtime.default", [Message.text("user", "hi")], response_format="text"
    )

    assert "response_format" not in router.body()


# --------------------------------------------------------------------------- #
# 三、向量化的基线路径 —— 此前**零条单测**
# --------------------------------------------------------------------------- #


@pytest.fixture
def embed_setup(gateway_factory):
    """一个走 embedding 能力的模型。"""
    router = HostRouter()

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        return httpx.Response(200, json=embed_body(len(payload["input"]), dim=4))

    router.on("a", _handler)
    gateway = gateway_factory(
        models={"m1": model_spec("a", capabilities=["embedding"])},
        aliases={"emb.default": {"candidates": ["m1"], "strategy": ["capability"]}},
        handler=router,
    )
    return gateway, router


async def test_embed_returns_vectors(embed_setup):
    """**向量化的成功路径此前只有一条集成测试**（``test_bootstrap.py``），
    单测一条都没有。而它与对话路径共用同一套编排（预算 / 重试 / 降级 / 计量）——
    共用意味着「对话侧测过就等于向量化也测过」**不成立**：
    编排里针对请求形状的分支（``RoutingContext`` 的构造、能力的推导）是分开写的。
    """
    gateway, _ = embed_setup

    result = await gateway.embed("emb.default", ["一段文本"])

    assert len(result.response.vectors) == 1
    assert result.response.dimension == 4
    assert result.attempts[-1].outcome == "success"
    assert len(result.response.vectors[0]) == 4, "向量长度要与声明的维度一致"


async def test_embed_sends_all_texts_in_one_request(embed_setup):
    """批量向量化要一次发上去（条数没超 max_batch 时）。"""
    gateway, router = embed_setup

    result = await gateway.embed("emb.default", ["甲", "乙", "丙"])

    assert router.body()["input"] == ["甲", "乙", "丙"]
    assert len(result.response.vectors) == 3
    assert router.count("a") == 1, "一次批量调用只该打一次上游"


async def test_embed_requires_the_embedding_capability(gateway_factory):
    """能力不足时**本地拦下**，零网络调用 —— 与 B-11 同一条纪律，但走的是向量化那条路。

    把对话模型挂到 ``emb.default`` 上必须报错，而不是发出去再拿一个看不懂的 400。
    """
    from gateway.errors import NoCapableModelError

    router = HostRouter().on("a", httpx.Response(200, json=embed_body()))
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, handler=router)

    with pytest.raises(NoCapableModelError):
        await gateway.embed("runtime.default", ["一段文本"])

    assert router.calls == [], "能力不足必须在本地拦下，不得发生网络调用"


# --------------------------------------------------------------------------- #
# 四、流式的基线路径 —— 此前两条都是**边界**（B-10 / B-10b）
# --------------------------------------------------------------------------- #


async def test_stream_chat_yields_chunks_then_done(gateway_factory):
    """**一条正常的流**：分片按序到达，最后以 ``StreamDone`` 收尾。

    此前 ``stream_chat`` 只有两条测试，而两条测的都是「已输出后断开怎么办」
    与「还没输出时能不能降级」—— 都是边界。**没断开时会拿到什么，没有测过。**
    """
    transport = StreamingTransport(pieces=3)
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, transport=transport)

    events = [
        event
        async for event in gateway.stream_chat("runtime.default", [Message.text("user", "hi")])
    ]

    chunks = [e.text for e in events if isinstance(e, StreamChunk)]
    assert chunks == ["片0", "片1", "片2"], "分片要按序、不丢、不重"
    assert isinstance(events[-1], StreamDone), "正常结束必须以 StreamDone 收尾，不是 StreamFailed"
    assert not any(type(e).__name__ == "StreamFailed" for e in events)


async def test_stream_chat_done_carries_the_full_text(gateway_factory):
    """``StreamDone`` 里的结果要含**完整正文**，而不只是最后一片。

    调用方通常在拿到 Done 之后就用那个结果（不再自己拼），
    所以它里面若只有最后一片，会安静地返回残缺的答案。
    """
    transport = StreamingTransport(pieces=3)
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, transport=transport)

    events = [
        event
        async for event in gateway.stream_chat("runtime.default", [Message.text("user", "hi")])
    ]
    done = events[-1]
    assert isinstance(done, StreamDone)
    assert done.result.content == "片0片1片2"


async def test_stream_chat_forwards_sampling_parameters(gateway_factory):
    """流式路径的 temperature / max_tokens 也要透传。

    注意这条**不能**沿用 ``HostRouter``：流式的传输层是 ``StreamingTransport``
    （它要造 SSE 响应）。所以这里直接读请求体 —— 说明「夹具要能记录请求体」
    这件事对流式同样必要。
    """
    captured: list[dict] = []

    class Capturing(StreamingTransport):
        async def handle_async_request(self, request):
            captured.append(json.loads(request.content))
            return await super().handle_async_request(request)

    gateway = gateway_factory(models=MODELS, aliases=ALIAS, transport=Capturing(pieces=1))

    async for _ in gateway.stream_chat(
        "runtime.default", [Message.text("user", "hi")], temperature=0.3, max_tokens=64
    ):
        pass

    assert captured[0]["temperature"] == 0.3
    assert captured[0]["max_tokens"] == 64
    assert captured[0]["stream"] is True, "流式请求必须带 stream=True"


async def test_stream_chat_lands_in_the_ledger(gateway_factory):
    """流式调用也要记账（哪怕拿不到用量）。

    gateway 的流式路径**拿不到 usage**（契约是 ``AsyncIterator[str]``）——
    所以成本是「未知」而不是 0。这条钉住「记录存在」这件事：
    若哪天有人以为「没用量就不用记」，账单会少掉整个流式那部分。
    """
    transport = StreamingTransport(pieces=2)
    gateway = gateway_factory(models=MODELS, aliases=ALIAS, transport=transport)

    async for _ in gateway.stream_chat("runtime.default", [Message.text("user", "hi")]):
        pass

    records = gateway.ledger.records
    assert records, "流式调用也必须留下账本记录"
    assert records[-1].alias == "runtime.default", "失败/流式的记录同样要带逻辑名"
    assert records[-1].input_tokens is None, "拿不到用量就是 None，不是 0"
