"""token 估算 —— **全平台只有一份口径**。

**为什么单列一个文件**：估算这件事有两个调用方，而它们必须用同一个数：

- ``gateway`` 自己在发起调用前估一次（决定有没有超预算）；
- ``src/context`` 在建上下文时估一次（决定该压多少、砍掉哪些段）。

两边各写一份的后果是**它们会漂移**，而漂移的表现是「预算算不准」——
**不报错**，只是偶尔超窗被厂商拒，或者白留一半预算。这类问题几乎无法从症状反推原因。

**为什么是字符数除以常数，而不是真正的 tokenizer**：
真实 tokenizer 要绑定具体厂商（`tiktoken` 绑 OpenAI、Qwen 有自己的词表），
而 ``NFR-C-01`` 要求 context 不认识任何厂商。**这个估算只用于预算的粗略切分**，
真正的账由 gateway 的用量记录来记（`FR-G-08`）。所以宁可要一个**保守、统一、
且与厂商无关**的经验值。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = ["CHARS_PER_TOKEN", "estimate_messages_tokens", "estimate_text_tokens"]

#: 经验值：约 4 个字符 ≈ 1 个 token。
#:
#: **保守方向**：中文按 1 字 ≈ 1 token 算会更准，但这里刻意取一个**偏小**的除数 ——
#: 高估 token 只会让我们多压一点（浪费），低估会让请求超窗（**被厂商拒**）。
#: 不对称的代价决定了取哪一边。
CHARS_PER_TOKEN = 4


def estimate_text_tokens(text: str) -> int:
    """一段文本的 token 估算。空串得 0，非空至少 1。"""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_messages_tokens(messages: Sequence[Any]) -> int:
    """一组消息的 token 估算。

    消息可以是 ``provider.types.Message``，也可以是任何有 ``content`` 属性的东西 ——
    用 ``getattr`` 取值是为了让 ``src/context`` **不必 import provider** 就能估。
    """
    total = 0
    for message in messages:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            total += len(content)
        else:
            total += sum(len(getattr(part, "text", "")) for part in (content or ()))
    return max(1, total // CHARS_PER_TOKEN)
