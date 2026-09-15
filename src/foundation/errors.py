"""统一错误体系与错误信封。

**问题**：错误在三处各长一套 —— 适配层抛 ``RuntimeError``（带一句拼出来的文本）、
业务层想区分「模型不支持」和「模型挂了」只能靠字符串匹配、对外出口各自决定 JSON 形状。
结果是同一类失败在 REST / MCP / CLI 三个端口表现不一致，且调用方**无法编程式判定**
该重试还是该放弃。

**做法**：所有模块只抛 :class:`BesaError` 子类；三个对外出口统一用错误信封转换::

    {"error": {"code", "message", "trace_id"}}

**三层错误，不要混**：

===========================  ==========================================  ==================
层                            例子                                        谁消费
===========================  ==========================================  ==================
``foundation.errors``        参数非法 / 未找到 / 冲突 / 超时 / 被取消      所有模块
``src/provider/errors.py``   鉴权失败 / 限流 / 内容拦截 / 上下文超长         gateway
``src/gateway``(边界归一化)  全部候选失败 / 预算耗尽 / 无满足能力的模型      agent / multiagent
===========================  ==========================================  ==================

**判据**：「**是否可重试**」这个判定只有 provider 有资格**表达**（它知道厂商状态码），
只有 gateway 有资格**决策**（它知道还有没有别的候选）。
所以这条语义留在 provider 层，**不上升到 foundation** ——
foundation 里的错误必须是「与领域无关、换个项目也成立」的。

**本模块必须保证**：``trace_id`` 始终在错误里（否则一次跨模型失败无法归因），
且**错误消息里不得出现 API Key**（需求说明书-provider NFR-P-04）。

**取消语义**：调用方取消必须原样传播，**不得**被包装成普通错误 ——
本模块不提供把 ``CancelledError`` 转成 ``BesaError`` 的转换。
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["REDACTED", "redact_secrets"]

#: 替换后的占位符。刻意显眼 —— 日志里看到它就说明有人试图打印密钥。
REDACTED: Final[str] = "***REDACTED***"

#: 密钥特征。**刻意保守**：只匹配高信号模式。
#:
#: 曾考虑加一条「32 位以上字母数字即视为密钥」的兜底规则，**已否决** ——
#: 它会连 UUID 一起脱敏，而 trace_id 正是 UUID 形状。
#: 把 trace_id 抹掉的代价（一次跨模型失败无法归因）远大于收益。
_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    # OpenAI 及绝大多数兼容厂商（DeepSeek / SiliconFlow / DashScope）统一用 sk- 前缀
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    # 已经拼进 Header 的形式
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    # key=value / "api_key": "..." 形式
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|secret|token)\b"
        r"\s*[:=]\s*['\"]?([A-Za-z0-9._\-]{6,})"
    ),
)


def redact_secrets(text: str) -> str:
    """把疑似密钥替换成 :data:`REDACTED`。

    需求说明书-provider NFR-P-04：API Key 不得出现在**日志、异常消息、``repr()``** 中。

    本函数是全仓库**唯一**的脱敏实现 —— 日志过滤器、错误消息构造、HTTP 埋点都调它。
    分散实现的后果是每处覆盖的密钥形状不同，漏掉的那一处就是泄漏点。

    **验收方式很粗暴但有效**：日志里出现 ``sk-`` 前缀即视为缺陷。

    Args:
        text: 任意待脱敏文本。``None`` 会被当作空串（便于直接喂 ``body_text``）。

    Returns:
        脱敏后的文本。
    """
    if not text:
        return ""
    result = str(text)
    for pattern in _PATTERNS:
        if pattern.groups >= 2 and "api" in pattern.pattern.lower():
            # 带捕获组的形式：保留字段名，只替换值 —— 保留字段名才能看出是哪个配置项泄漏了
            result = pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", result)
        else:
            result = pattern.sub(REDACTED, result)
    return result
