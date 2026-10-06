"""参数注入检测（``tool/injection.py``，`FR-T-14`）。

**为什么这组的断言都盯着「零外部调用」**：注入检测是**调用前的**一关 ——
它的价值在于「那个参数根本没走到执行」。一条「被拒了但已经发出去请求」的用例
是自相矛盾的。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tool.injection import InjectionPolicy, Inspector, looks_like_path_argument

SRC = Path(__file__).resolve().parents[3] / "src" / "tool"


def _inspector(**over) -> Inspector:
    base = {
        "check_path_traversal": True,
        "url_allowlist": (),
        "block_private_networks": True,
        "denied_paths": (),
    }
    base.update(over)
    return Inspector(InjectionPolicy(**base))


# --------------------------------------------------------------------------- #
# 路径穿越（结构性）
# --------------------------------------------------------------------------- #


def test_relative_path_inside_scope_is_fine(tmp_path: Path):
    inspector = _inspector()
    verdict = inspector.check({"path": "a.txt"}, allowed_paths=(tmp_path.resolve(),))
    assert verdict.safe


def test_dotdot_escape_is_refused(tmp_path: Path):
    """``..`` 必须在**归一化之后**判 —— 黑名单挡不住它的各种写法。"""
    inspector = _inspector()
    verdict = inspector.check({"path": "../../etc/passwd"}, allowed_paths=(tmp_path.resolve(),))
    assert not verdict.safe
    assert verdict.kind == "path_traversal"
    assert verdict.argument == "path"


def test_absolute_path_outside_scope_is_refused(tmp_path: Path):
    inspector = _inspector()
    verdict = inspector.check({"path": "C:/Windows/System32"}, allowed_paths=(tmp_path.resolve(),))
    assert not verdict.safe


def test_no_scope_configured_means_no_path_check(tmp_path: Path):
    """没配范围时**不做穿越判定** —— 那是权限层的职责（默认拒绝），不是这一层。

    两层都判会导致同一条错误有两个来源，而排障时不知道看哪个。
    """
    inspector = _inspector()
    assert inspector.check({"path": "../x"}, allowed_paths=()).safe


@pytest.mark.parametrize("name", ["path", "file_path", "target_dir", "cwd", "output_file"])
def test_path_like_parameter_names_are_recognized(name: str):
    assert looks_like_path_argument(name)


def test_non_path_parameters_are_not_treated_as_paths(tmp_path: Path):
    """``pattern`` 里出现 ``../`` 不该被当成路径穿越 —— 它就是一个正则片段。"""
    inspector = _inspector()
    assert inspector.check({"pattern": "../"}, allowed_paths=(tmp_path.resolve(),)).safe


# --------------------------------------------------------------------------- #
# 敏感路径（直接拒绝，不指望脱敏兜底）
# --------------------------------------------------------------------------- #


def test_denied_path_is_refused_even_inside_the_scope(tmp_path: Path):
    """``.env`` **在授权范围内也拒绝**。

    脱敏是**尽力**（密钥格式多变），把安全押在「你想全了格式」上是不行的 ——
    所以这类路径根本不该被读。
    """
    inspector = _inspector(denied_paths=(".env", "**/*.pem"))
    (tmp_path / ".env").write_text("X=1", encoding="utf-8")

    verdict = inspector.check({"path": ".env"}, allowed_paths=(tmp_path.resolve(),))
    assert not verdict.safe
    assert verdict.kind == "denied_path"
    assert "不该被读" in verdict.detail


# --------------------------------------------------------------------------- #
# URL / SSRF（白名单 + 内网判定）
# --------------------------------------------------------------------------- #


def test_external_url_is_refused_when_allowlist_is_empty(tmp_path: Path):
    """**空白名单 = 不允许任何外部地址**（`C-T-8`）。

    反过来（空 = 允许全部）会让「忘了配」的默认行为恰好是最危险的那一种。
    """
    verdict = _inspector().check({"url": "https://example.com/x"})
    assert not verdict.safe
    assert verdict.kind == "url"
    assert "url_allowlist 为空" in verdict.detail


def test_allowlisted_host_passes():
    inspector = _inspector(url_allowlist=("api.deepseek.com",))
    assert inspector.check({"url": "https://api.deepseek.com/v1/chat"}).safe


def test_subdomain_of_allowlisted_host_passes():
    inspector = _inspector(url_allowlist=("deepseek.com",))
    assert inspector.check({"url": "https://api.deepseek.com/v1/chat"}).safe


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",   # 云元数据服务
        "http://127.0.0.1:6379/",                     # 本机 Redis
        "http://localhost:8000/admin",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://[::1]/",
    ],
)
def test_private_targets_are_refused(url: str):
    """内网地址**即使名字看起来无害也要拦** —— 云元数据服务是这里最值钱的目标。"""
    inspector = _inspector(url_allowlist=("example.com",))
    verdict = inspector.check({"url": url})
    assert not verdict.safe
    assert verdict.kind == "url"


def test_non_http_scheme_is_refused():
    verdict = _inspector(url_allowlist=("example.com",)).check({"url": "file:///etc/passwd"})
    assert not verdict.safe
    assert "不支持的协议" in verdict.detail


# --------------------------------------------------------------------------- #
# 关于 command：**刻意不做运行时检测**
# --------------------------------------------------------------------------- #


def test_no_tool_composes_shell_strings_from_arguments():
    """**结构性断言**：没有任何工具把参数拼进 shell 命令。

    「命令注入」在本模块不该靠**运行时黑名单**解决 —— 黑名单依赖你想全了
    ``;`` ``&&`` ``|`` 的各种变体，而你想不全。结构性答案是「**不拼字符串**」，
    而它的守卫就是这条：源码里不得出现「把变量插进传给 subprocess 的字符串」。

    这条会红，说明有人引入了真正的注入面 —— 那时该做的是改成参数化调用
    （``create_subprocess_exec`` + 参数列表），而不是加一条黑名单。
    """
    offenders: list[str] = []
    risky = re.compile(
        r"create_subprocess_shell\(\s*(f[\"']|[^\"')]*\.format\(|[^\"')]*\s*%\s*|.*\+\s*)"
    )
    for path in SRC.rglob("*.py"):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if risky.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}: {line.strip()}")

    assert not offenders, (
        "有工具把参数拼进了 shell 命令 —— 那是真的注入面：\n  "
        + "\n  ".join(offenders)
        + "\n改法：用参数列表（create_subprocess_exec）而不是拼字符串。"
    )


def test_short_command_fragment_is_not_blocked():
    """短命令片段不该被误伤 —— 这条钉住「我们没做黑名单」这个决定。

    如果哪天有人加了 ``;`` / ``&&`` 的黑名单，这条会红，
    提醒他那不是结构性防御，且会误伤合法用法。
    """
    assert _inspector().check({"command": "ls -la; echo done"}).safe
