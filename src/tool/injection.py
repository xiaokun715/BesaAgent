"""参数是不可信输入（`FR-T-14`）。

**模型的参数必须当成不可信输入。** 这不是防御理论 ——
``bash`` 的存在意味着参数最终会变成**真实的系统调用**。

## 三个结构性检测 + 一个诚实承认做不到的

=================================  ==================  ==========================================
类别                                能做到什么           为什么不能靠黑名单
=================================  ==================  ==========================================
``path_traversal``                 归一化后判范围       黑名单挡不住 ``....//``、URL 编码、符号链接的组合
``url``                            **白名单**（默认全拒） 内网地址能写成 10 进制 / 16 进制 / 省略写法
``denied_path``                    glob 匹配            这是**已知**的敏感路径，本来就该列举，不靠猜
``command``                        —— **不做运行时检测**  见下
=================================  ==================  ==========================================

## 关于 ``command``：为什么这里没有它

「命令注入」在**本模块的现状下不存在** —— 因为没有任何工具把参数**拼进**一条 shell 命令。
``bash`` 的 ``command`` 参数**本身就是**那条命令（它由权限、``side_effect=destructive``、
默认关闭、以及沙箱共同管住），不存在「注入到更大的命令里」这回事。

真正会引入这个问题的是**将来某个工具**去拼字符串，比如::

    subprocess.run(f"psql -c '{sql}'")        # ← 这才是注入面

而它的结构性答案是「**不拼字符串**」（参数化调用或显式 quote），
**不是**一个运行时的黑名单 —— 黑名单依赖你想全了 ``;`` ``&&`` ``|`` 的各种变体，而你想不全。

所以这条纪律落在**编码规范 + 一条结构测试**上（见 ``tests/unit/tool/test_injection.py``
里那条「没有工具拼 shell 字符串」的断言），而不是落在这个文件里。
:data:`~tool.types.InjectionKind` 里保留 ``command`` 是给将来的工具用的词汇。
"""

from __future__ import annotations

import ipaddress
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from tool.types import InjectionVerdict

__all__ = ["InjectionPolicy", "Inspector", "looks_like_path_argument"]

_log = logging.getLogger(__name__)

#: 名字里含这些词的参数，按**路径**对待。
#:
#: 用命名约定而不是「让每个工具声明」：工具已经在 schema 里写了 ``path`` / ``cwd``，
#: 再来一份声明就有了两个口径，而它们会漂移。
_PATH_HINTS: tuple[str, ...] = ("path", "file", "dir", "cwd", "target", "output", "source")

#: 这些主机名一律拒绝 —— 它们指向本机或元数据服务。
#: **不靠 IP 判定**（``localhost`` 不是 IP 字面量），所以两者都要查。
_BLOCKED_HOSTS: frozenset[str] = frozenset(
    {"localhost", "metadata.google.internal", "metadata", "instance-data"}
)

_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})


@dataclass(frozen=True)
class InjectionPolicy:
    """注入检测的配置（``tool.injection`` / ``tool.output.deny_paths``）。"""

    check_path_traversal: bool = True
    check_urls: bool = True
    #: URL 白名单。**空 = 不允许任何外部地址**（默认拒绝，与 `C-T-8` 一致）
    url_allowlist: tuple[str, ...] = ()
    block_private_networks: bool = True
    #: 敏感路径 glob。命中的**直接拒绝**，不指望脱敏兜底（`DT-14`）
    denied_paths: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, cfg: Mapping[str, object] | None) -> InjectionPolicy:
        data = dict(cfg or {})
        injection = dict(data.get("injection") or {})  # type: ignore[arg-type]
        output = dict(data.get("output") or {})  # type: ignore[arg-type]

        allowlist = injection.get("url_allowlist") or ()
        denied = output.get("deny_paths") or ()

        return cls(
            check_path_traversal=bool(injection.get("check_path_traversal", True)),
            check_urls=True,
            url_allowlist=tuple(str(x) for x in allowlist),  # type: ignore[union-attr]
            block_private_networks=bool(injection.get("block_private_networks", True)),
            denied_paths=tuple(str(x) for x in denied),  # type: ignore[union-attr]
        )


def looks_like_path_argument(name: str) -> bool:
    """参数名是否像路径。"""
    lowered = name.lower()
    return any(hint in lowered for hint in _PATH_HINTS)


@dataclass
class Inspector:
    """按策略扫一遍参数。

    **无状态**（除了不可变策略），可以被并发使用。
    """

    policy: InjectionPolicy = field(default_factory=InjectionPolicy)

    def check(
        self,
        arguments: Mapping[str, object],
        *,
        allowed_paths: Sequence[Path] = (),
    ) -> InjectionVerdict:
        """扫一遍。第一个命中的就返回 —— 一次只报一条，让模型聚焦修它。"""
        for name, value in arguments.items():
            if isinstance(value, str):
                verdict = self._check_string(name, value, allowed_paths=allowed_paths)
                if not verdict.safe:
                    return verdict
            elif isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, str):
                        verdict = self._check_string(name, item, allowed_paths=allowed_paths)
                        if not verdict.safe:
                            return verdict
        return InjectionVerdict()

    # ---------------------------------------------------------------- 逐项
    def _check_string(
        self, name: str, value: str, *, allowed_paths: Sequence[Path]
    ) -> InjectionVerdict:
        # URL 先判：它比路径更具体（`http://x/../../` 两种含义都有，按 URL 处理更准）
        if self.policy.check_urls:
            verdict = self._check_url(name, value)
            if not verdict.safe:
                return verdict

        if looks_like_path_argument(name):
            return self._check_path(name, value, allowed_paths=allowed_paths)

        return InjectionVerdict()

    def _check_url(self, name: str, value: str) -> InjectionVerdict:
        if "://" not in value:
            return InjectionVerdict()

        parsed = urlparse(value)
        if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
            return InjectionVerdict(
                kind="url",
                argument=name,
                detail=f"不支持的协议 {parsed.scheme!r}；只允许 {sorted(_ALLOWED_SCHEMES)}",
            )

        host = (parsed.hostname or "").lower()
        if not host:
            return InjectionVerdict(kind="url", argument=name, detail="URL 没有主机名")

        if self.policy.block_private_networks:
            blocked, why = _is_private_target(host)
            if blocked:
                return InjectionVerdict(
                    kind="url",
                    argument=name,
                    detail=f"目标是内网/本机地址（{why}）：{host}",
                )

        if not self.policy.url_allowlist:
            # **默认拒绝**（`C-T-8`）：白名单空 = 不允许任何外部地址。
            # 反过来（空 = 允许全部）会让「忘了配」的默认行为恰好是最危险的那种。
            return InjectionVerdict(
                kind="url",
                argument=name,
                detail=(
                    "参数里出现了外部地址，但 tool.injection.url_allowlist 为空"
                    "（**空 = 不允许任何外部地址**）"
                ),
            )

        if not any(
            host == allowed.lower() or host.endswith("." + allowed.lower())
            for allowed in self.policy.url_allowlist
        ):
            return InjectionVerdict(
                kind="url",
                argument=name,
                detail=f"主机 {host!r} 不在白名单里：{list(self.policy.url_allowlist)}",
            )
        return InjectionVerdict()

    def _check_path(
        self, name: str, value: str, *, allowed_paths: Sequence[Path]
    ) -> InjectionVerdict:
        raw = value.strip()
        if not raw:
            return InjectionVerdict()

        candidate = Path(raw)
        # 相对路径是**相对于第一个可写/可读范围**解析的 —— 与工具那边的口径一致。
        # 口径不一致的话，这里判过的路径到了工具里会变成另一个东西。
        if not candidate.is_absolute() and allowed_paths:
            candidate = allowed_paths[0] / candidate
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = candidate

        # 敏感路径：**直接拒绝**，不指望脱敏兜底（`DT-14`）
        for pattern in self.policy.denied_paths:
            if resolved.match(pattern) or candidate.match(pattern):
                return InjectionVerdict(
                    kind="denied_path",
                    argument=name,
                    detail=(
                        f"命中了敏感路径规则 {pattern!r}。\n"
                        "这类文件**不该被读** —— 脱敏是尽力而为，"
                        "把安全押在「你想全了密钥格式」上是不行的。"
                    ),
                )

        if not self.policy.check_path_traversal or not allowed_paths:
            return InjectionVerdict()

        within = any(
            resolved == allowed or allowed in resolved.parents for allowed in allowed_paths
        )
        if not within:
            return InjectionVerdict(
                kind="path_traversal",
                argument=name,
                detail=(
                    f"{resolved} 不在授权范围内（{', '.join(str(p) for p in allowed_paths)}）。\n"
                    "注意比较的是**归一化之后**的路径 —— 符号链接与 `..` 都已经展开。"
                ),
            )
        return InjectionVerdict()


def _is_private_target(host: str) -> tuple[bool, str]:
    """主机是不是指向本机 / 内网。

    **先判字面量再判 IP**：``localhost`` 不是 IP 字面量，
    而 ``169.254.169.254`` 是（元数据服务 —— 拿到它等于拿到云主机的临时凭据）。
    """
    if host in _BLOCKED_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        return True, "本机或内部域名"

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False, ""

    if address.is_loopback:
        return True, "回环地址"
    if address.is_link_local:
        # 169.254.0.0/16 —— 云元数据服务在这个段里，是最值得拦的一个
        return True, "链路本地地址（云元数据服务在这个段）"
    if address.is_private:
        return True, "私有地址段"
    if address.is_reserved or address.is_multicast:
        return True, "保留/组播地址"
    return False, ""
