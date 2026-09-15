"""``foundation.logging`` 的单测。

**重点是脱敏**：它是一条安全保证（``NFR-P-04``），
而安全保证最容易在重构中**静默失效** —— 没人会因为「日志里多了个密钥」而发现测试变红，
除非有一条测试专门盯着它。
"""

from __future__ import annotations

import io
import logging

import pytest

from foundation.logging import (
    current_trace_id,
    reset_trace_id,
    set_trace_id,
    setup_logging,
)

SECRET = "sk-abcdef1234567890xyz"


@pytest.fixture(autouse=True)
def clean_trace_id():
    """每个用例从「没有 trace_id」开始。

    ``set_trace_id`` 的作用域是**进程**（CLI 每次调用起一个新进程，所以生产上不需要清理），
    但在同一个测试进程里它会跨用例泄漏 —— 上一条 CLI 测试设过的 trace_id
    会出现在这里的日志行上。
    """
    token = set_trace_id("")
    yield
    reset_trace_id(token)


@pytest.fixture
def captured() -> io.StringIO:
    """装好日志并把输出收进内存。"""
    buffer = io.StringIO()
    setup_logging(logging.INFO, stream=buffer)
    return buffer


def test_writes_to_the_given_stream(captured: io.StringIO):
    logging.getLogger("demo").info("你好")
    assert "你好" in captured.getvalue()


def test_repeated_setup_does_not_duplicate_lines(captured: io.StringIO):
    """**幂等** —— 重复调用只更新，不叠加 handler。

    叠加的后果是一条日志打三遍，而人们会因此把级别调高（或干脆不看日志），
    于是「写了日志」和「看得到日志」又脱节了。
    """
    logging.getLogger("demo").info("只应出现一次")
    setup_logging(logging.INFO, stream=captured)
    setup_logging(logging.INFO, stream=captured)

    assert captured.getvalue().count("只应出现一次") == 1


def test_level_filtering(captured: io.StringIO):
    setup_logging(logging.WARNING, stream=captured)
    logging.getLogger("demo").info("不该出现")
    logging.getLogger("demo").warning("该出现")

    output = captured.getvalue()
    assert "不该出现" not in output
    assert "该出现" in output


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #


def test_secret_in_message_is_redacted(captured: io.StringIO):
    logging.getLogger("demo").info("调用失败，key=%s", SECRET)
    output = captured.getvalue()

    assert "sk-" not in output, "日志里出现 sk- 前缀即视为缺陷"
    assert "REDACTED" in output


def test_secret_in_bare_message_is_redacted(captured: io.StringIO):
    logging.getLogger("demo").info(f"Authorization: Bearer {SECRET}")
    assert "sk-" not in captured.getvalue()


def test_secret_in_dict_args_is_redacted(captured: io.StringIO):
    """``logger.info("%(k)s", {"k": secret})`` 这种写法同样要盖住。

    只处理 ``record.msg`` 会漏掉**整类**「把值放进 args」的写法，
    而那种写法在结构化日志里很常见。
    """
    logging.getLogger("demo").info("%(key)s", {"key": SECRET})
    assert "sk-" not in captured.getvalue()


def test_redaction_also_covers_exceptions(captured: io.StringIO):
    """异常消息同样不放过 —— ``logger.exception`` 是最常带出原始报文的地方。"""
    logger = logging.getLogger("demo")
    try:
        raise RuntimeError(f"upstream said: invalid key {SECRET}")
    except RuntimeError:
        logger.exception("调用失败")

    assert "sk-" not in captured.getvalue()


def test_trace_id_is_in_the_line(captured: io.StringIO):
    token = set_trace_id("abc123")
    try:
        logging.getLogger("demo").info("带 trace 的一行")
    finally:
        reset_trace_id(token)

    assert "[abc123]" in captured.getvalue()


def test_trace_id_is_absent_by_default(captured: io.StringIO):
    logging.getLogger("demo").info("没有 trace 的一行")
    assert "[-]" in captured.getvalue()


def test_trace_id_is_context_local(captured: io.StringIO):
    """**协程安全**：并发调用之间不得串 trace_id。

    全局变量会让 A 调用的 trace_id 出现在 B 调用的日志行上 ——
    那比没有 trace_id 更糟，因为它会把人引向错误的结论。
    """
    import asyncio

    async def worker(name: str) -> str:
        token = set_trace_id(name)
        try:
            await asyncio.sleep(0)          # 让出，制造交错
            return current_trace_id()
        finally:
            reset_trace_id(token)

    async def main() -> list[str]:
        return list(await asyncio.gather(*(worker(f"trace-{i}") for i in range(5))))

    assert asyncio.run(main()) == [f"trace-{i}" for i in range(5)]


def test_unknown_level_falls_back_to_info(captured: io.StringIO):
    setup_logging("NOT_A_LEVEL", stream=captured)
    logging.getLogger("demo").info("仍然可见")
    assert "仍然可见" in captured.getvalue()


def test_httpx_is_quieted(captured: io.StringIO):
    """httpx 每个请求打一条 INFO，会淹没有用信息。"""
    assert logging.getLogger("httpx").level >= logging.WARNING
