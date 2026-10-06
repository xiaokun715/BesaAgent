"""真实厂商的**协议兼容**测试 —— 只覆盖 mock 测不出来的那几件。

跑法（默认跳过）::

    BESA_LIVE_TESTS=1 PYTHONPATH=src python -m pytest tests/integration/provider -v

**不重复单测已经覆盖的东西**（编排、重试、参数透传那些 mock 测得好）。
这里每一条都对应一个「mock 替换掉网络边界，于是看不见」的具体位置。
"""

from __future__ import annotations

import httpx
import pytest

from provider.errors import AuthError
from provider.types import ChatRequest, Message, ToolSpec

from .conftest import DEEPSEEK_MODEL

__all__ = []


class SpyTransport(httpx.AsyncBaseTransport):
    """记录**真实**请求，然后原样转发给真的传输层。

    「发到哪个路径」这件事 mock 看不见：``httpx.MockTransport`` **按 host 分流，
    压根不看路径**。所以把端点写成 ``/runtime/completions`` 时，
    全部单测照过 —— 而真实调用 404。这个 spy 就是为了让那条路径可断言。
    """

    def __init__(self) -> None:
        self._real = httpx.AsyncHTTPTransport()
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return await self._real.handle_async_request(request)

    async def aclose(self) -> None:
        await self._real.aclose()


# --------------------------------------------------------------------------- #
# ① 端点路径 —— 本仓库真的踩过这个坑
# --------------------------------------------------------------------------- #


async def test_chat_hits_the_openai_compatible_path(deepseek):
    """适配器打出去的必须是 ``/v1/chat/completions``。

    ⚠ 这条是**回归守卫**：一次 ``chat`` → ``runtime`` 的全局改名把
    ``_endpoint()`` 的返回值也改成了 ``/runtime/completions``，
    而 **mock 测试全绿**（MockTransport 按 host 分流，不看路径）。
    只有真实调用才 404，且错误被归一化成 ``ModelNotFoundError: 模型不存在或未开通``
    —— 那个信息会把人引去查模型名，而问题在路径上。
    """
    spy = SpyTransport()
    provider = deepseek(transport=spy, max_tokens=16)

    try:
        await provider.chat_model().chat(
            ChatRequest(messages=[Message.text("user", "hi")], max_tokens=16)
        )
    finally:
        await spy.aclose()

    path = spy.requests[0].url.path
    assert path == "/v1/chat/completions", (
        f"对话端点的路径不对：{path}\n"
        "期望 /v1/chat/completions —— 若这里变成了别的（比如含 runtime），"
        "说明有人改了 _endpoint() 而没跑这一组测试。"
    )


async def test_embedding_hits_the_embeddings_path(siliconflow):
    """向量化的端点是 ``/v1/embeddings``（同样是 mock 看不见的）。"""
    spy = SpyTransport()
    provider = siliconflow(transport=spy)

    try:
        await provider.embedding_model().embed(["hi"])
    finally:
        await spy.aclose()

    assert spy.requests[0].url.path == "/v1/embeddings"


# --------------------------------------------------------------------------- #
# ② 对话真的能拿到内容，且思考过程没有混进正文
# --------------------------------------------------------------------------- #


async def test_chat_returns_real_content(deepseek):
    """最基础的那件事，走真网络。"""
    provider = deepseek()

    response = await provider.chat_model().chat(
        ChatRequest(messages=[Message.text("user", "只输出两个字：收到")], max_tokens=1024)
    )

    assert response.content.strip(), "真实模型必须返回非空正文"
    assert response.model, "响应要带模型名"


async def test_reasoning_does_not_leak_into_content(deepseek):
    """``FR-P-05``：推理模型的**思考过程必须独立成字段**，不混进 ``content``。

    这条只能打真实模型：``reasoning_content`` 是 DeepSeek 的**厂商扩展字段**，
    mock 里造它等于把「我们以为的形状」写进断言 —— 那测的是自己的假设，不是上游。

    实测：``deepseek-v4-flash`` 是推理模型，思考会占 token。
    ``max_tokens`` 给小了会得到 ``content=""`` + ``finish_reason="length"`` ——
    适配器对那个情况有专门的 WARNING（``FR-P-05``）。
    """
    provider = deepseek()

    response = await provider.chat_model().chat(
        ChatRequest(
            messages=[Message.text("user", "简单说说为什么 1+1=2")], max_tokens=1024
        )
    )

    assert response.content.strip(), "正文不能是空的 —— 空了多半是 max_tokens 被思考吃光"
    if response.reasoning:
        # 思考过程**不得**出现在正文里：它一旦混进去会污染下游的结构化解析，
        # 而且只在推理模型上复现 —— 开发期极难发现。
        assert response.reasoning.strip() not in response.content, (
            "思考过程混进了 content —— 下游的 JSON 解析会被它污染"
        )


# --------------------------------------------------------------------------- #
# ③ SSE 的真实形状
# --------------------------------------------------------------------------- #


async def test_streaming_yields_text_incrementally(deepseek):
    """真实 SSE：能拿到增量正文。

    mock 里的 SSE 是我们自己按文档拼的 —— 而**上游实际的字段名与结束方式**
    只有真跑才知道（这次踩的 ``reasoning_content`` 就是同一个道理）。
    """
    provider = deepseek()
    pieces: list[str] = []

    async for piece in provider.chat_model().stream_chat(
        ChatRequest(messages=[Message.text("user", "数到五")], max_tokens=1024, stream=True)
    ):
        pieces.append(piece)

    text = "".join(pieces)
    assert text.strip(), "流式必须产出正文"
    assert len(pieces) >= 1


# --------------------------------------------------------------------------- #
# ④ 工具调用：协议真的通
# --------------------------------------------------------------------------- #


async def test_tool_calling_round_trip(deepseek):
    """带上工具定义时，模型能**真的**发出 ``tool_calls``。

    单测里 ``tools=`` 只验证了「参数有没有透传到上游」（B-11 那条甚至只断言**被拒绝**）。
    「透传了」与「模型真的会用」是两件事 —— 后者取决于厂商协议那条链路真的对。
    """
    provider = deepseek()
    tool = ToolSpec(
        name="get_weather",
        description="查询某地天气",
        parameters={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )

    response = await provider.chat_model().chat(
        ChatRequest(
            messages=[Message.text("user", "北京今天天气怎么样？用工具查。")],
            tools=[tool],
            tool_choice="auto",
            max_tokens=1024,
        )
    )

    assert response.tool_calls, (
        "给了工具且明确要求用它，模型却没发出 tool_calls —— "
        "要么工具定义没送到，要么厂商的协议形状与我们以为的不同"
    )
    call = response.tool_calls[0]
    assert call.name == "get_weather"
    assert call.arguments_raw, "必须保留原始参数字符串（模型可能给出非法 JSON）"


# --------------------------------------------------------------------------- #
# ⑤ 鉴权失败：归一化成哪个异常
# --------------------------------------------------------------------------- #


async def test_bad_key_maps_to_auth_error(deepseek):
    """错 key → ``AuthError``，且 **``retryable=False``**。

    这两件事单测里都测过（用 mock 的 401），但**真实的 401 长什么样**没验过：
    厂商可能用 403、可能在 200 里带一个错误对象、也可能返回别的东西。
    归一化错了的后果是重试风暴 —— 一个永远不会好的错误被反复重试。
    """
    provider = deepseek(api_key="sk-definitely-invalid-key-000")

    with pytest.raises(AuthError) as excinfo:
        await provider.chat_model().chat(
            ChatRequest(messages=[Message.text("user", "hi")], max_tokens=16)
        )

    assert excinfo.value.retryable is False, "鉴权失败**绝不能**被标记为可重试"
    assert "sk-definitely-invalid-key-000" not in str(excinfo.value), (
        "错误信息里不得出现密钥本体（NFR-P-04）"
    )


# --------------------------------------------------------------------------- #
# ⑥ 上游的 index 分片重置（本仓库踩过的第二个坑）
# --------------------------------------------------------------------------- #


def _cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = (sum(a * a for a in left) ** 0.5) * (sum(b * b for b in right) ** 0.5)
    return dot / norm if norm else 0.0


async def test_embedding_batch_order_survives_the_vendor_quirk(siliconflow):
    """批量 ≥9 条时，返回的向量**顺序**仍然与入参一一对应。

    ⚠ 上游的怪癖（实测）：SiliconFlow 在批量 ≥9 条时会把 ``index``
    按 **8 条分片重置** —— 9 条返回 ``[0..7, 0]``，而**数组顺序是对的**。
    适配器原先无条件按 ``index`` 重排，于是把一份正确的响应搅成了
    ``[e0, e8, e1, ...]``，且**不抛任何异常**。

    **为什么这条只能在真实厂商上测**：那个分片重置是上游行为，
    mock 里造它等于把「我们以为的上游」写进断言。

    做法：9 条批量（触发怪癖）与 4 条批量（``index`` 正常，可信）比。
    顺序对了的话，前四条应当逐条高度相似；错位的话余弦会掉到 0.5 上下。
    """
    model = siliconflow().embedding_model()
    texts = [
        "登录接口返回 401", "数据库连接池耗尽", "订单金额计算错误",
        "页面加载超过三秒", "缓存穿透导致慢查询", "用户头像上传失败",
        "定时任务重复执行", "消息队列积压", "导出报表乱码",
    ]

    batch = await model.embed(texts)          # 9 条 → 触发上游的 index 分片重置
    golden = await model.embed(texts[:4])     # 4 条 → index 正常，可作金标准

    similarities = [
        _cosine(batch.vectors[i], golden.vectors[i]) for i in range(4)
    ]
    assert all(s > 0.99 for s in similarities), (
        f"批量返回的向量错位了：逐条余弦 = {[round(s, 4) for s in similarities]}\n"
        "正确顺序下应当接近 1.0；错位（比如第 2 条拿到的是第 9 条的向量）会掉到 0.5 左右。\n"
        "这说明 _order_by_index 没有挡住上游的 index 分片重置。"
    )
