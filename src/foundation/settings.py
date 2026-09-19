"""配置加载：YAML(base + env 叠加) + ``${ENV:-default}`` 插值 + 环境变量覆盖。

用法::

    from foundation.settings import load_config

    cfg = load_config()              # env 由 BESA_ENV 提供，缺省 dev
    cfg = load_config(env="test")

**加载顺序**：``configs/base.yaml`` → ``configs/<env>.yaml`` **深度合并**。
base 是全环境共享的默认值，各 env 只覆写差异。``test.yaml`` 把能力钉回 mock ——
这是 CI 能在无 DB、无密钥环境跑通的前提。

**插值**：``${VAR}`` / ``${VAR:-fallback}`` 在**加载期**解析::

    postgres:
      dsn: ${BESA_POSTGRES_DSN:-postgresql+asyncpg://besa:besa@127.0.0.1:5432/besa}

``${VAR}`` 缺省且变量未设置时**抛错**，不静默变成空串 —— 见 :func:`_interpolate`。

**为什么密钥不进配置文件**：配置文件会进版本库、会被贴进 issue、会出现在日志里。
让配置**只能写变量名**（``api_key_env: OPENAI_API_KEY``），值由
``foundation.provider.resolve_api_key`` 去环境变量取 ——
这样「配置里永远没有密钥本体」是**结构性**的，而不是靠自觉。

**本模块不做的事**：

- 不决定**哪个模型可用** —— 那是 ``src/gateway/registry.py``；
- 不构造 **provider 实例** —— 那是 ``src/composition/bootstrap.py``；
- 不校验**业务语义**（如「候选链不能为空」）—— 那是 gateway 在启动期的事（``C-3``）。

本模块只做一件事：**把 YAML 变成有类型、错误可定位的配置对象。**
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = ["Settings", "SettingsError", "load_config"]

#: 环境变量名 → 该环境由谁决定
ENV_VAR = "BESA_ENV"

#: 仓库根下放配置的目录名
CONFIG_DIRNAME = "configs"

#: ``.env`` 默认位置（仓库根，已 gitignore）
DOTENV_NAME = ".env"

#: ``${VAR}`` 或 ``${VAR:-default}``
_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

#: 已知的环境变量覆盖：配置路径 → 环境变量名。
#:
#: 只列**运维需要临时改**的那几个（容器编排、CI 注入）。业务参数一律走 YAML ——
#: 否则「这个值到底从哪来的」会有两个答案。
KNOWN_OVERRIDES: Mapping[str, str] = {
    "postgres.dsn": "BESA_POSTGRES_DSN",
    "redis.url": "BESA_REDIS_URL",
    "logging.level": "BESA_LOG_LEVEL",
}


class SettingsError(Exception):
    """配置错误。**必须可定位** —— 消息里带上「哪个文件、哪个字段」。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Settings:
    """加载完成的配置。

    刻意做成**只读 + 按路径取值**，而不是一堆具名字段：
    各模块需要的配置段完全不同（gateway / provider / repo），
    具名字段会让本文件在每次新增模块时都被改一遍，而它是最该稳定的那一层。
    """

    env: str
    data: Mapping[str, Any] = field(default_factory=dict)
    #: 按顺序记录加载了哪些文件 —— 排障时第一个要问的就是「你读的是哪份配置」
    sources: tuple[str, ...] = ()
    #: 解析后的环境变量（进程环境 + ``.env``，进程环境优先）。
    #:
    #: **必须随配置一起带出去**：厂商凭据（``api_key_env``）要在这里查，
    #: 而 ``.env`` 只在加载期被读进内存 —— 不传下去的话，
    #: 「把密钥写进 .env」这条最常用的用法会静默失效，表现为「配了但没有密钥」。
    #: ``repr=False``：它里面有密钥，不能出现在日志或调试器里（NFR-P-04）。
    env_vars: Mapping[str, str] = field(default_factory=dict, repr=False)

    # ---------------------------------------------------------------- 取值
    def get(self, path: str, default: Any = None) -> Any:
        """按**点分路径**取值：``cfg.get("gateway.retry.total_max_attempts")``。

        **支持键名本身含点的情况**，这对本仓库是必需的：
        逻辑模型名写作 ``runtime.default`` / ``emb.default``，于是
        ``cfg.get("gateway.aliases.runtime.default.candidates")`` 里的
        ``runtime.default`` 是一个**键**，不是两级路径。

        取法是**贪心最长匹配**：每一层先尝试用尽可能多的段拼成一个键，
        拼不出来再退一格。于是上面那个路径会这样解析::

            gateway → aliases → "runtime.default" → candidates

        歧义时（同时存在 ``runtime`` 与 ``runtime.default`` 两个键）取**更长**的那个 ——
        更具体的匹配优先级更高，这也符合「键里带点是有意为之」的约定。

        路径不存在返回 ``default``；路径**中途**遇到非映射（比如把
        ``gateway`` 写成了字符串）也返回 ``default``，而不是抛
        ``AttributeError`` —— 后者会把「配置写错层级」伪装成代码 bug。
        """
        parts = path.split(".")
        node: Any = self.data
        index = 0

        while index < len(parts):
            if not isinstance(node, Mapping):
                return default

            for end in range(len(parts), index, -1):
                candidate = ".".join(parts[index:end])
                if candidate in node:
                    node = node[candidate]
                    index = end
                    break
            else:
                return default

        return node

    def require(self, path: str) -> Any:
        """取值，缺失则抛 :class:`SettingsError`（消息里带上已加载的文件）。"""
        missing = object()
        value = self.get(path, missing)
        if value is missing:
            where = ", ".join(self.sources) or "（无）"
            raise SettingsError(f"配置缺少必需的字段 {path!r}（已加载：{where}）")
        return value

    def section(self, name: str) -> Mapping[str, Any]:
        """取一个配置段，保证返回映射（缺失时返回空映射）。

        返回空映射而不是 ``None``，是为了让 `for k, v in cfg.section("models").items()`
        这种写法不需要先判空 —— 空配置和没配置，在调用方看来是同一件事。
        """
        value = self.get(name)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise SettingsError(
                f"配置段 {name!r} 应当是映射，实际是 {type(value).__name__}"
            )
        return value

    # ---------------------------------------------------------------- 常用段
    @property
    def providers(self) -> Mapping[str, Any]:
        return self.section("providers")

    @property
    def models(self) -> Mapping[str, Any]:
        return self.section("models")

    @property
    def gateway(self) -> Mapping[str, Any]:
        return self.section("gateway")

    def __repr__(self) -> str:
        return f"Settings(env={self.env!r}, sources={list(self.sources)!r})"


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #


def load_config(
    env: str | None = None,
    *,
    config_dir: str | Path | None = None,
    project_root: str | Path | None = None,
    env_vars: Mapping[str, str] | None = None,
    dotenv: bool = True,
) -> Settings:
    """加载配置。

    Args:
        env: 环境名。缺省时取 ``BESA_ENV``，再缺省为 ``dev``。
        config_dir: 配置目录。缺省为 ``<project_root>/configs``。
        project_root: 仓库根。缺省从本文件位置向上推断。
        env_vars: 环境变量来源，**仅供测试注入**。
        dotenv: 是否加载仓库根的 ``.env``。

    Raises:
        SettingsError: 配置目录缺失、YAML 语法错误、插值变量未定义。

    Returns:
        :class:`Settings`。``base.yaml`` 必须存在；``<env>.yaml`` 可以不存在
        （等价于「该环境全部用默认值」）。
    """
    source = dict(os.environ if env_vars is None else env_vars)
    if dotenv:
        root = Path(project_root) if project_root else _project_root()
        source = {**_read_dotenv(root / DOTENV_NAME), **source}   # 进程环境优先于 .env

    resolved_env = env or source.get(ENV_VAR) or "dev"
    directory = Path(config_dir) if config_dir else _project_root() / CONFIG_DIRNAME

    base_path = directory / "base.yaml"
    if not base_path.is_file():
        raise SettingsError(f"缺少基础配置文件：{base_path}")

    sources: list[str] = []
    merged: dict[str, Any] = {}
    env_path = directory / f"{resolved_env}.yaml"
    if not env_path.is_file():
        # 用户显式要了某个环境，却没有对应的文件 —— 他会**静默拿到 base 的配置**：
        # ``--env prodd``（拼错）不会报错，只是行为和你以为的不一样。
        # 一条 warning 让这件事可见，同时不阻止「只靠 base 跑」这种合理用法。
        logging.getLogger(__name__).warning(
            "环境 %r 没有对应的配置文件 %s，将只使用 base.yaml 的默认值。"
            "若这是拼写错误，请注意现在的行为与你期望的不同。",
            resolved_env, env_path.name,
        )

    for path in (base_path, env_path):
        if not path.is_file():
            continue
        merged = _deep_merge(merged, _read_yaml(path))
        sources.append(str(path))

    merged = _apply_overrides(merged, source)
    merged = _interpolate(merged, source)

    # env 字段以显式参数为准：配置文件里写错 env 会让日志和实际加载的环境对不上，
    # 而那是最难发现的一类配置问题。
    merged["env"] = resolved_env

    return Settings(
        env=resolved_env, data=merged, sources=tuple(sources), env_vars=source
    )


def _project_root() -> Path:
    """从本文件位置向上找到仓库根。

    判定依据是「存在 ``configs`` 目录或 ``pyproject.toml``」，而不是写死层级 ——
    这样把 ``src`` 挪个位置不会让配置悄悄读不到。
    """
    here = Path(__file__).resolve()
    for candidate in (here.parent, *here.parents):
        if (candidate / CONFIG_DIRNAME).is_dir() or (candidate / "pyproject.toml").is_file():
            return candidate
    return here.parent


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SettingsError(f"无法读取配置文件 {path}：{exc}") from exc

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        # YAML 的原始报错带行列号，保留它 —— 这是唯一能快速定位语法错误的信息
        raise SettingsError(f"配置文件 {path} 语法错误：{exc}") from exc

    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise SettingsError(f"配置文件 {path} 的顶层必须是映射，实际是 {type(loaded).__name__}")
    return loaded


def _read_dotenv(path: Path) -> dict[str, str]:
    """极简 ``.env`` 解析：``KEY=VALUE``，``#`` 开头为注释。

    **刻意不支持变量展开、多行值、export 前缀** —— 那些是 dotenv 库的职责，
    而这里只需要承载「本地开发用的几个密钥」。引入完整实现会让
    「配置文件里没有密钥」这条约束多出一个不受控的旁路。
    """
    if not path.is_file():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """深度合并。**映射递归合并，其余整体替换**（包括列表）。

    列表整体替换而不是拼接，是刻意的：``candidates: [a]`` 覆写成 ``[b]``
    应当**只保留 b**。若做拼接，用户在 dev 里想临时换成 b 会得到 ``[a, b]``，
    而 a 仍然会被优先尝试 —— 一个「改了配置但没生效」的经典陷阱。
    """
    result: dict[str, Any] = dict(base)
    for key, value in override.items():
        current = result.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            result[key] = _deep_merge(current, value)
        else:
            result[key] = value
    return result


def _apply_overrides(data: MutableMapping[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    """把已知环境变量写进配置的对应路径。"""
    result = dict(data)
    for path, var_name in KNOWN_OVERRIDES.items():
        value = env.get(var_name)
        if value:
            _set_path(result, path, value)
    return result


def _set_path(data: MutableMapping[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    node: MutableMapping[str, Any] = data
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, MutableMapping):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _interpolate(value: Any, env: Mapping[str, str], *, path: str = "") -> Any:
    """递归解析 ``${VAR}`` / ``${VAR:-default}``。

    **未定义且未给默认值时抛错，而不是留空或原样保留。**
    三种做法的取舍：

    - 原样保留 ``"${OPENAI_API_KEY}"`` → 它会被当成真正的密钥发出去，
      表现为一个莫名其妙的 401；
    - 静默变成空串 → 表现为「配了但没生效」，同样难查；
    - 抛错 → 启动就失败，且消息里能指出是哪个变量、在哪一段。

    只在 ``${...}`` 上生效，普通字符串（含 ``$`` 的）原样通过。
    """
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            if name in env:
                return env[name]
            if default is not None:
                return default
            where = f"（出现在 {path}）" if path else ""
            raise SettingsError(
                f"配置引用了未定义的环境变量 ${{{name}}}{where}；"
                f"请设置它，或写成 ${{{name}:-默认值}}"
            )

        return _INTERPOLATION.sub(_replace, value)

    if isinstance(value, Mapping):
        return {
            key: _interpolate(item, env, path=f"{path}.{key}" if path else str(key))
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            _interpolate(item, env, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    return value


def _iter_leaf_paths(data: Mapping[str, Any], prefix: str = "") -> Iterator[str]:
    """列出全部叶子路径 —— 供排障与测试用。"""
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, Mapping):
            yield from _iter_leaf_paths(value, path)
        else:
            yield path
