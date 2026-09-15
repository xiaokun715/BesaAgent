"""装配层集成测试：从配置到可用运行时。

**这里测的是「接起来之后还对不对」** —— 单个模块的单测全绿，不代表
配置加载出来的字段名与注册表期待的字段名一致。这类错误只在装配层暴露，
而且症状通常是「跑起来发现某个模型永远不被选中」这种很难归因的形态。

**全部经 ``httpx.MockTransport`` 注入，零真实网络请求。** 这一条很重要：
集成测试若真的去打外部端点，会慢、会不稳定、还会在别人的账单上体现。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import httpx
import pytest

from composition.bootstrap import Runtime, build_runtime
from foundation.settings import Settings, load_config
from provider.types import Message


class Router:
    """按 host 分发的假端点，并记录调用。"""

    def __init__(self, routes: dict[str, str]) -> None:
        self._routes = routes
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.calls.append(host)
        label = self._routes.get(host)
        if label is None:
            raise AssertionError(f"未配置的假端点：{host}")
        return httpx.Response(
            200,
            json={
                "model": label,
                "choices": [{"message": {"content": f"来自 {label}"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


def two_host_settings() -> Settings:
    """两个不同端点的模型，都走 openai 适配器。"""
    return Settings(
        env="test",
        data={
            "providers": {
                "alpha": {"base_url": "https://alpha/v1", "api_key_env": "ALPHA_KEY"},
                "beta": {"base_url": "https://beta/v1", "api_key_env": "BETA_KEY"},
            },
            "models": {
                "m-alpha": {"provider": "openai", "vendor": "alpha", "model": "alpha-model"},
                "m-beta": {"provider": "openai", "vendor": "beta", "model": "beta-model"},
            },
            "gateway": {
                "aliases": {"chat.default": {"candidates": ["m-alpha", "m-beta"]}},
                "retry": {
                    "max_attempts_per_candidate": 1,
                    "total_max_attempts": 4,
                    "backoff_base_s": 0.0,
                    "jitter_ratio": 0.0,
                },
            },
        },
        env_vars={"ALPHA_KEY": "sk-alpha-1234567890", "BETA_KEY": "sk-beta-1234567890"},
    )


# --------------------------------------------------------------------------- #
# 用仓库真实的 configs/
# --------------------------------------------------------------------------- #


async def test_runtime_boots_from_repo_configs():
    """仓库自带的 ``configs/`` 必须能直接装配成功 —— 它是 CI 与新人上手的入口。"""
    runtime = build_runtime(env="test", env_vars={})
    try:
        assert runtime.settings.env == "test"
        assert any("base.yaml" in source for source in runtime.settings.sources)
        assert any("test.yaml" in source for source in runtime.settings.sources)

        result = await runtime.gateway.chat("chat.default", [Message.text("user", "你好")])
        assert result.content
        assert result.model_key == "chat-mock"
        assert result.degraded is False
    finally:
        await runtime.aclose()


async def test_repo_configs_are_pinned_to_mock_for_tests():
    """``test.yaml`` 存在的意义是**对 base.yaml 的改动免疫**。

    base 是给人改的（有人会把它切到真实厂商做本地调试），
    而 CI 必须稳定 —— 否则某次「顺手改一下 base」会让 CI 开始真的去调外部 API。
    """
    runtime = build_runtime(env="test", env_vars={})
    try:
        candidates = [spec.key for spec in runtime.registry.candidates("chat.default")]
        assert candidates == ["chat-mock"]
        embedded = [spec.key for spec in runtime.registry.candidates("emb.default")]
        assert embedded == ["emb-mock"]
    finally:
        await runtime.aclose()


async def test_embedding_path_works_end_to_end():
    runtime = build_runtime(env="test", env_vars={})
    try:
        result = await runtime.gateway.embed("emb.default", ["第一段", "第二段"])
        vectors = result.response.vectors
        assert len(vectors) == 2
        assert result.response.dimension == 768
        assert all(len(vector) == 768 for vector in vectors)
    finally:
        await runtime.aclose()


# --------------------------------------------------------------------------- #
# 换模型 = 改配置
# --------------------------------------------------------------------------- #


async def test_swapping_candidate_changes_model_without_code_change():
    """**换模型 = 改配置，业务代码零改动**（FR-G-02 的最终形态）。

    下面两次调用的代码完全相同，只有 candidates 变了。
    """
    router = Router({"alpha": "alpha", "beta": "beta"})

    async def call_with(candidates: list[str]) -> str:
        settings = two_host_settings()
        data = dict(settings.data)
        data["gateway"] = {
            **data["gateway"],
            "aliases": {"chat.default": {"candidates": candidates}},
        }
        runtime = build_runtime(
            Settings(env="test", data=data, env_vars=settings.env_vars),
            provider_options={"transport": router.transport},
        )
        try:
            result = await runtime.gateway.chat("chat.default", [Message.text("user", "hi")])
            return result.content
        finally:
            await runtime.aclose()

    assert await call_with(["m-alpha"]) == "来自 alpha"
    assert await call_with(["m-beta"]) == "来自 beta"


async def test_degradation_across_configured_candidates():
    """主候选失败 → 按配置的链降级到备选，且**标记已降级**。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "alpha":
            return httpx.Response(503, text="alpha 挂了")
        return httpx.Response(
            200,
            json={
                "model": "beta-model",
                "choices": [{"message": {"content": "备选答复"}, "finish_reason": "stop"}],
            },
        )

    runtime = build_runtime(
        two_host_settings(), provider_options={"transport": httpx.MockTransport(handler)}
    )
    try:
        result = await runtime.gateway.chat("chat.default", [Message.text("user", "hi")])
        assert result.content == "备选答复"
        assert result.model_key == "m-beta"
        assert result.degraded is True
    finally:
        await runtime.aclose()


# --------------------------------------------------------------------------- #
# 软失败：缺密钥不阻止启动
# --------------------------------------------------------------------------- #


async def test_missing_credentials_do_not_block_startup():
    """``NFR-G-05``：缺密钥只让**那个模型**不可用，进程照常启动。"""
    settings = Settings(
        env="test",
        data={
            "providers": {"alpha": {"base_url": "https://alpha/v1", "api_key_env": "ALPHA_KEY"}},
            "models": {
                "m-alpha": {"provider": "openai", "vendor": "alpha", "model": "m"},
                "m-mock": {"provider": "mock", "model": "mock-llm"},
            },
            "gateway": {
                "aliases": {"chat.default": {"candidates": ["m-alpha", "m-mock"]}}
            },
        },
        env_vars={},          # 没有任何密钥
    )

    runtime = build_runtime(settings)
    try:
        alpha = runtime.registry.model("m-alpha")
        assert alpha.available is False
        # **保留**在注册表里而不是丢掉 —— 丢掉会让「为什么这个候选从没被用到」
        # 变成一个查不到的空缺，而不是一条能读的原因。
        assert "ALPHA_KEY" in (alpha.unavailable_reason or "")

        # 链路自动落到 mock，照常可用
        result = await runtime.gateway.chat("chat.default", [Message.text("user", "hi")])
        assert result.model_key == "m-mock"

        # **``degraded`` 是 False，这是刻意的语义区分**：
        # 不可用的模型在**选链阶段**就被过滤掉了，从未进入尝试链 ——
        # 它没有被「试过然后失败」，所以不是运行时降级。
        #
        #   degraded=True  → 运行时故障导致的临时降级（503、超时…）
        #   unavailable    → 配置/环境问题（缺密钥），**启动时就已警告**，
        #                    且每次都是如此，不是「这一次出了问题」
        #
        # 混同两者的后果：一个没配密钥的环境里，每次调用的 degraded 都是 True，
        # 这个标记就再也不能用来回答「这次是不是出了故障」。
        assert result.degraded is False
        assert [record.model_key for record in result.attempts] == ["m-mock"]
    finally:
        await runtime.aclose()


async def test_credentials_resolved_from_settings_env_vars():
    """``.env`` 里读到的密钥必须能流到 provider。

    这条容易断：``.env`` 只在**加载期**被读进内存，若不随 ``Settings`` 传下去，
    「把密钥写进 .env」这条最常用的用法会静默失效，表现为「配了但没有密钥」。
    """
    router = Router({"alpha": "alpha"})
    settings = two_host_settings()

    runtime = build_runtime(settings, provider_options={"transport": router.transport})
    try:
        provider = runtime.registry.provider("m-alpha")
        assert provider.api_key_env == "ALPHA_KEY"
        assert provider._resolve_api_key() == "sk-alpha-1234567890"
    finally:
        await runtime.aclose()


# --------------------------------------------------------------------------- #
# vendor ≠ provider：OpenAI 兼容厂商
# --------------------------------------------------------------------------- #


async def test_vendor_selects_endpoint_while_provider_selects_adapter():
    """``provider`` 选适配器，``vendor`` 选端点与凭据。

    这是 DeepSeek / SiliconFlow / Moonshot 这类「走 OpenAI 兼容协议但端点和密钥是自己的」
    厂商的配置方式。若没有 ``vendor``，就只能把 base_url 和 api_key_env 逐个写在
    model 上 —— 啰嗦，且一旦某个 model 忘了写就会静默连到真 OpenAI。
    """
    settings = Settings(
        env="test",
        data={
            "providers": {"deepseek": {"base_url": "https://api.deepseek.com/v1",
                                       "api_key_env": "DEEPSEEK_API_KEY"}},
            "models": {
                "chat-deepseek": {
                    "provider": "openai",      # 适配器
                    "vendor": "deepseek",      # 端点 + 凭据
                    "model": "deepseek-chat",
                }
            },
            "gateway": {"aliases": {"chat.default": {"candidates": ["chat-deepseek"]}}},
        },
        env_vars={"DEEPSEEK_API_KEY": "sk-deepseek-1234567890"},
    )

    runtime = build_runtime(settings)
    try:
        spec = runtime.registry.model("chat-deepseek")
        assert spec.available is True
        assert spec.provider == "deepseek", "尝试记录/报表里应显示 vendor，而不是适配器名"

        provider = runtime.registry.provider("chat-deepseek")
        assert provider.config.base_url == "https://api.deepseek.com/v1"
        # 关键：报错时要指向 DEEPSEEK_API_KEY，而不是适配器自带的 OPENAI_API_KEY。
        # 指错方向会让人去设一个完全无关的变量，然后疑惑「设了怎么还是不行」。
        assert provider.api_key_env == "DEEPSEEK_API_KEY"
    finally:
        await runtime.aclose()


# --------------------------------------------------------------------------- #
# 启动期校验
# --------------------------------------------------------------------------- #


def test_dangling_candidate_is_rejected_at_startup():
    """候选引用了不存在的模型键 → **启动期硬失败**（``C-3``）。

    越晚发现代价越大：运行时报出来时，调用方只会看到「没有可用模型」，
    得反过来猜是哪条配置写错了。
    """
    settings = Settings(
        env="test",
        data={
            "models": {"m1": {"provider": "mock", "model": "m"}},
            "gateway": {"aliases": {"chat.default": {"candidates": ["m1", "typo"]}}},
        },
        env_vars={},
    )

    with pytest.raises(ValueError) as excinfo:
        build_runtime(settings)

    message = str(excinfo.value)
    assert "typo" in message
    assert "m1" in message, "错误信息要列出已定义的键，便于对照"


def test_unknown_vendor_is_rejected_at_startup():
    settings = Settings(
        env="test",
        data={
            "models": {"m1": {"provider": "openaii", "model": "m"}},   # 拼错
            "gateway": {"aliases": {"chat.default": {"candidates": ["m1"]}}},
        },
        env_vars={},
    )

    with pytest.raises(ValueError) as excinfo:
        build_runtime(settings)
    assert "openaii" in str(excinfo.value)


def test_unknown_strategy_is_rejected_at_startup():
    settings = Settings(
        env="test",
        data={
            "models": {"m1": {"provider": "mock", "model": "m"}},
            "gateway": {
                "aliases": {"chat.default": {"candidates": ["m1"], "strategy": ["cheapest"]}}
            },
        },
        env_vars={},
    )

    with pytest.raises(ValueError) as excinfo:
        build_runtime(settings)
    assert "cheapest" in str(excinfo.value)


def test_unknown_alias_lists_what_is_available():
    import asyncio

    from gateway.errors import UnknownAliasError

    runtime = build_runtime(env="test", env_vars={})

    async def go() -> None:
        try:
            await runtime.gateway.chat("chat.defualt", [Message.text("user", "hi")])
        finally:
            await runtime.aclose()

    with pytest.raises(UnknownAliasError) as excinfo:
        asyncio.run(go())
    assert "chat.default" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# Runtime 的生命周期
# --------------------------------------------------------------------------- #


async def test_runtime_is_async_context_manager():
    async with build_runtime(env="test", env_vars={}) as runtime:
        assert isinstance(runtime, Runtime)
        result = await runtime.gateway.chat("chat.default", [Message.text("user", "hi")])
        assert result.content
    # 退出后连接已释放；再关一次不应抛错（幂等）
    await runtime.aclose()
