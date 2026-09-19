"""``foundation.settings`` 的单测。

多数用例用 ``tmp_path`` 自建配置目录而不是读仓库的 ``configs/`` ——
测加载器本身时，不能让它依赖「当前仓库的配置长什么样」，
否则改一行 base.yaml 就会有测试变红，而那是**配置**的问题、不是**加载器**的问题。
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from foundation.settings import Settings, SettingsError, load_config


def write_config(directory: Path, name: str, content: str) -> None:
    (directory / name).write_text(textwrap.dedent(content), encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "configs"
    directory.mkdir()
    return directory


# --------------------------------------------------------------------------- #
# 叠加
# --------------------------------------------------------------------------- #


def test_base_then_env_overlay(config_dir: Path):
    write_config(config_dir, "base.yaml", """
        a: 1
        nested:
          x: base
          y: keep
    """)
    write_config(config_dir, "dev.yaml", """
        nested:
          x: dev
    """)

    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)

    assert cfg.get("a") == 1
    assert cfg.get("nested.x") == "dev"
    assert cfg.get("nested.y") == "keep", "未覆写的键必须保留"
    assert cfg.env == "dev"


def test_missing_env_file_is_fine(config_dir: Path):
    """``<env>.yaml`` 不存在等价于「该环境全部用默认值」，不是错误。"""
    write_config(config_dir, "base.yaml", "a: 1")
    cfg = load_config("staging", config_dir=config_dir, env_vars={}, dotenv=False)
    assert cfg.get("a") == 1


def test_missing_base_is_an_error(tmp_path: Path):
    directory = tmp_path / "empty"
    directory.mkdir()
    with pytest.raises(SettingsError) as excinfo:
        load_config("dev", config_dir=directory, env_vars={}, dotenv=False)
    assert "base.yaml" in str(excinfo.value)


def test_lists_are_replaced_not_concatenated(config_dir: Path):
    """**列表整体替换**，不拼接。

    拼接会让「在 dev 里把候选链换成 b」变成 ``[a, b]`` —— 而 a 仍然优先被尝试，
    表现为「改了配置但没生效」。这是最难查的一类配置问题。
    """
    write_config(config_dir, "base.yaml", """
        gateway:
          aliases:
            runtime.default:
              candidates: [a, b]
    """)
    write_config(config_dir, "dev.yaml", """
        gateway:
          aliases:
            runtime.default:
              candidates: [c]
    """)

    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)
    assert cfg.get("gateway.aliases.runtime.default.candidates") == ["c"]


def test_yaml_syntax_error_names_the_file(config_dir: Path):
    write_config(config_dir, "base.yaml", "a: [1, 2\n")
    with pytest.raises(SettingsError) as excinfo:
        load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)
    assert "base.yaml" in str(excinfo.value)


def test_non_mapping_toplevel_is_rejected(config_dir: Path):
    write_config(config_dir, "base.yaml", "- a\n- b\n")
    with pytest.raises(SettingsError) as excinfo:
        load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)
    assert "顶层" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 插值
# --------------------------------------------------------------------------- #


def test_interpolation_uses_default_when_unset(config_dir: Path):
    write_config(config_dir, "base.yaml", 'dsn: ${BESA_DSN:-fallback://x}')
    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)
    assert cfg.get("dsn") == "fallback://x"


def test_interpolation_uses_env_when_set(config_dir: Path):
    write_config(config_dir, "base.yaml", 'dsn: ${BESA_DSN:-fallback://x}')
    cfg = load_config(
        "dev", config_dir=config_dir, env_vars={"BESA_DSN": "real://y"}, dotenv=False
    )
    assert cfg.get("dsn") == "real://y"


def test_undefined_variable_without_default_is_an_error(config_dir: Path):
    """**抛错而不是留空或原样保留**。

    - 原样保留 ``"${OPENAI_API_KEY}"`` → 它会被当真正的密钥发出去，表现为莫名其妙的 401；
    - 静默变空串 → 表现为「配了但没生效」；
    - 抛错 → 启动即失败，且消息能指出是哪个变量、在哪个字段。
    """
    write_config(config_dir, "base.yaml", """
        providers:
          openai:
            base_url: ${MISSING_BASE_URL}
    """)
    with pytest.raises(SettingsError) as excinfo:
        load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)

    message = str(excinfo.value)
    assert "MISSING_BASE_URL" in message
    assert "providers.openai.base_url" in message, "要指出出现在哪个字段"


def test_interpolation_reaches_nested_lists(config_dir: Path):
    write_config(config_dir, "base.yaml", """
        gateway:
          aliases:
            runtime.default:
              candidates: ["${A:-a}", "b"]
    """)
    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)
    assert cfg.get("gateway.aliases.runtime.default.candidates") == ["a", "b"]


def test_plain_dollar_is_untouched(config_dir: Path):
    """只有 ``${...}`` 是插值语法，普通的 ``$`` 原样通过。"""
    write_config(config_dir, "base.yaml", 'note: "价格 $100"')
    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)
    assert cfg.get("note") == "价格 $100"


# --------------------------------------------------------------------------- #
# 环境变量覆盖与 .env
# --------------------------------------------------------------------------- #


def test_known_override_beats_yaml(config_dir: Path):
    write_config(config_dir, "base.yaml", """
        postgres:
          dsn: from-yaml
    """)
    cfg = load_config(
        "dev",
        config_dir=config_dir,
        env_vars={"BESA_POSTGRES_DSN": "from-env"},
        dotenv=False,
    )
    assert cfg.get("postgres.dsn") == "from-env"


def test_dotenv_is_loaded_and_process_env_wins(tmp_path: Path, config_dir: Path):
    write_config(config_dir, "base.yaml", 'dsn: ${MY_DSN:-fallback}')
    (tmp_path / ".env").write_text("MY_DSN=from-dotenv\n# 注释行\n", encoding="utf-8")

    cfg = load_config(
        "dev", config_dir=config_dir, project_root=tmp_path, env_vars={}, dotenv=True
    )
    assert cfg.get("dsn") == "from-dotenv"

    # 进程环境优先于 .env —— 容器编排与 CI 靠这条覆盖本地文件
    cfg2 = load_config(
        "dev",
        config_dir=config_dir,
        project_root=tmp_path,
        env_vars={"MY_DSN": "from-process"},
        dotenv=True,
    )
    assert cfg2.get("dsn") == "from-process"


def test_env_vars_travel_with_settings(config_dir: Path):
    """解析后的环境变量必须随 ``Settings`` 一起带出去。

    厂商凭据（``api_key_env``）要在这里查，而 ``.env`` 只在加载期被读进内存 ——
    不传下去的话，「把密钥写进 .env」这条最常用的用法会静默失效。
    """
    write_config(config_dir, "base.yaml", "a: 1")
    cfg = load_config(
        "dev", config_dir=config_dir, env_vars={"MY_KEY": "secret"}, dotenv=False
    )
    assert cfg.env_vars["MY_KEY"] == "secret"


def test_env_vars_never_in_repr(config_dir: Path):
    """环境变量里有密钥，**不得**出现在 ``repr`` 里（NFR-P-04）。"""
    write_config(config_dir, "base.yaml", "a: 1")
    cfg = load_config(
        "dev", config_dir=config_dir, env_vars={"MY_KEY": "sk-secret-123456"}, dotenv=False
    )
    assert "sk-secret-123456" not in repr(cfg)


def test_env_name_from_variable(config_dir: Path):
    write_config(config_dir, "base.yaml", "a: 1")
    write_config(config_dir, "prod.yaml", "a: 2")

    cfg = load_config(config_dir=config_dir, env_vars={"BESA_ENV": "prod"}, dotenv=False)
    assert cfg.env == "prod"
    assert cfg.get("a") == 2


# --------------------------------------------------------------------------- #
# 取值
# --------------------------------------------------------------------------- #


def test_get_returns_default_for_missing_path(config_dir: Path):
    write_config(config_dir, "base.yaml", "a: 1")
    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)

    assert cfg.get("nope") is None
    assert cfg.get("nope.deeper") is None
    assert cfg.get("a.deeper") is None, "路径中途遇到标量应返回默认值，而不是抛 AttributeError"


def test_get_addresses_keys_that_contain_dots(config_dir: Path):
    """逻辑模型名写作 ``runtime.default``，它在配置树里是**一个键**而非两级路径。

    这是本仓库的常态（``gateway.aliases.runtime.default``），
    所以点分路径必须支持它 —— 否则 ``cfg.get("gateway.aliases.runtime.default")``
    会静默返回 ``None``，而调用方看不出是自己写错了还是配置没有。
    """
    write_config(config_dir, "base.yaml", """
        gateway:
          aliases:
            runtime.default:
              candidates: [a]
            emb.default:
              candidates: [b]
    """)
    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)

    assert cfg.get("gateway.aliases.runtime.default.candidates") == ["a"]
    assert cfg.get("gateway.aliases.emb.default.candidates") == ["b"]
    # 不带点的普通路径不受影响
    assert set(cfg.get("gateway.aliases")) == {"runtime.default", "emb.default"}


def test_get_prefers_the_more_specific_key(config_dir: Path):
    """歧义时取更长的键 —— 「键里带点」是有意为之，具体匹配优先。"""
    cfg = Settings(
        env="t", data={"a": {"b": {"c": "两层"}, "b.c": "点键"}}
    )
    assert cfg.get("a.b.c") == "点键"
    assert cfg.get("a.b") == {"c": "两层"}


def test_require_reports_what_was_loaded(config_dir: Path):
    write_config(config_dir, "base.yaml", "a: 1")
    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)

    with pytest.raises(SettingsError) as excinfo:
        cfg.require("gateway.retry.total_max_attempts")

    assert "gateway.retry.total_max_attempts" in str(excinfo.value)
    assert "base.yaml" in str(excinfo.value)


def test_section_returns_empty_mapping_when_absent(config_dir: Path):
    """缺失返回空映射而不是 ``None`` —— 让 ``.items()`` 不需要先判空。"""
    write_config(config_dir, "base.yaml", "a: 1")
    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)

    assert dict(cfg.section("models")) == {}
    assert dict(cfg.section("gateway")) == {}


def test_section_rejects_non_mapping(config_dir: Path):
    write_config(config_dir, "base.yaml", "models: 不是映射")
    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)

    with pytest.raises(SettingsError) as excinfo:
        cfg.section("models")
    assert "映射" in str(excinfo.value)


def test_settings_is_frozen(config_dir: Path):
    """不可变 —— 配置在装配期被改掉会让「这个值哪来的」无从回答。"""
    write_config(config_dir, "base.yaml", "a: 1")
    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)

    with pytest.raises(Exception):
        cfg.env = "other"  # type: ignore[misc]


def test_sources_record_what_was_loaded(config_dir: Path):
    write_config(config_dir, "base.yaml", "a: 1")
    write_config(config_dir, "dev.yaml", "b: 2")
    cfg = load_config("dev", config_dir=config_dir, env_vars={}, dotenv=False)

    assert len(cfg.sources) == 2
    assert any("base.yaml" in source for source in cfg.sources)
    assert any("dev.yaml" in source for source in cfg.sources)


def test_settings_direct_construction_works():
    """允许手工构造 —— 测试里常常不需要真的读文件。"""
    cfg = Settings(env="test", data={"gateway": {"retry": {"total_max_attempts": 7}}})
    assert cfg.get("gateway.retry.total_max_attempts") == 7
