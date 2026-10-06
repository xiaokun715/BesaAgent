"""沙箱（``tool/sandbox.py``，`FR-T-15`）。

**这组最重要的一条是 ``clean_env``**：不继承宿主环境。
继承的话 ``bash env`` 能把宿主上的密钥全打出来 —— 而它只需要一个 ``env`` 参数就能避免。

**另一条是「诚实标注」**：soft 模式下有几项限制**没有真正生效**，
它们必须能被读出来。让人以为「配了就安全」比不配更危险。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from foundation.clock import FakeClock
from tool.base import ToolContext
from tool.sandbox import (
    ENFORCED_IN_SOFT,
    LIMITS_NEEDING_ISOLATION,
    Sandbox,
    SandboxLimits,
)

SECRET_VAR = "BESA_TEST_SECRET_KEY"

#: 用**同一个解释器**跑子命令，而不是 ``echo`` / ``ping`` 这类平台专有命令。
#: 这样断言的是「子进程真的看到了什么」，而不是「这个平台的 shell 怎么拼字符串」——
#: 后者在 CI（大概率是 Linux）上会整组挂掉。
PY = f'"{sys.executable}"'


def _ctx(root: Path, **over) -> ToolContext:
    base = {
        "allowed_paths": (root.resolve(),),
        "max_output_bytes": 4096,
        "max_output_lines": 100,
        "timeout_s": 5.0,
        "clock": FakeClock(),
    }
    base.update(over)
    return ToolContext(**base)


# --------------------------------------------------------------------------- #
# 干净环境 —— 这条最要紧
# --------------------------------------------------------------------------- #


def test_clean_env_does_not_inherit_secrets(tmp_path: Path, monkeypatch):
    """**宿主的环境变量一个都不该被子进程看到。**

    这是整个沙箱里性价比最高的一条：一个 ``env`` 参数，挡住的是
    「模型让 ``bash env`` 把宿主的密钥全打出来」。
    """
    monkeypatch.setenv(SECRET_VAR, "sk-should-not-leak")
    env = Sandbox().environment(_ctx(tmp_path))
    assert SECRET_VAR not in env


def test_clean_env_keeps_what_is_needed_to_run(tmp_path: Path, monkeypatch):
    """白名单要够长到命令**跑得起来** —— Windows 上缺 ``SystemRoot`` 连 cmd 都起不来。"""
    monkeypatch.setenv("PATH", "/usr/bin")
    env = Sandbox().environment(_ctx(tmp_path))
    assert "PATH" in env


def test_clean_env_redirects_temp_into_the_workdir(tmp_path: Path):
    """临时目录指到工作目录里 —— 别让工具写宿主的 ``/tmp``。"""
    env = Sandbox().environment(_ctx(tmp_path))
    assert str(tmp_path.resolve()) in env["TMPDIR"]


def test_clean_env_can_be_turned_off_but_says_so(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(SECRET_VAR, "visible")
    sandbox = Sandbox(SandboxLimits(clean_env=False))
    assert SECRET_VAR in sandbox.environment(_ctx(tmp_path))


async def test_subprocess_cannot_see_host_env(tmp_path: Path, monkeypatch):
    """端到端：真的起一个子进程，把它的 ``os.environ`` 原样打出来看。

    **比「用 echo 打一个变量」更硬**：它断言的是子进程**整份环境**里都没有那个密钥，
    而不是「拼接之后那一行里没有」。
    """
    monkeypatch.setenv(SECRET_VAR, "sk-should-not-leak")
    sandbox = Sandbox(SandboxLimits(clean_env=True))

    outcome = await sandbox.run_process(
        f'{PY} -c "import os,json;print(json.dumps(dict(os.environ)))"',
        ctx=_ctx(tmp_path, max_output_bytes=100_000),
    )

    child_env = json.loads(outcome.output)
    assert SECRET_VAR not in child_env, "宿主的环境变量被子进程看到了"
    assert "sk-should-not-leak" not in outcome.output


# --------------------------------------------------------------------------- #
# 超时：必须真的杀掉
# --------------------------------------------------------------------------- #


async def test_timeout_kills_the_process(tmp_path: Path):
    """**超时必须真的杀掉**。

    只放弃等待的话，子进程会继续跑 —— 它的副作用还在发生，
    而调用方以为「这次超时了、什么都没做」。
    """
    sandbox = Sandbox(SandboxLimits(clean_env=True))
    outcome = await sandbox.run_process(
        f'{PY} -c "import time;time.sleep(30)"', ctx=_ctx(tmp_path), timeout_s=0.5
    )

    assert outcome.timed_out is True
    assert outcome.killed is True, "超时后必须 kill，不能只是放弃等待"


async def test_output_is_capped(tmp_path: Path):
    sandbox = Sandbox(SandboxLimits(clean_env=True))
    outcome = await sandbox.run_process(
        f'{PY} -c "print(\'x\' * 10000)"',
        ctx=_ctx(tmp_path, max_output_bytes=200, max_output_lines=1000),
    )
    assert outcome.truncated is True
    assert len(outcome.output.encode("utf-8")) <= 200


async def test_normal_command_reports_its_exit_code(tmp_path: Path):
    sandbox = Sandbox(SandboxLimits(clean_env=True))
    ok = await sandbox.run_process(f'{PY} -c "pass"', ctx=_ctx(tmp_path))
    bad = await sandbox.run_process(f'{PY} -c "raise SystemExit(7)"', ctx=_ctx(tmp_path))
    assert ok.returncode == 0
    assert bad.returncode == 7


# --------------------------------------------------------------------------- #
# 诚实标注 —— soft 模式不是安全边界
# --------------------------------------------------------------------------- #


def test_soft_mode_reports_what_is_not_enforced():
    """soft 模式下有几项**没有真正生效**，必须能被读出来。

    「让人以为配了就安全」比不配更危险 —— 这条与 ``bash`` 拒绝清单同源。
    """
    limits = SandboxLimits(mode="soft")
    unenforced = limits.unenforced()
    assert set(unenforced) == LIMITS_NEEDING_ISOLATION
    assert "max_cpu_s" in unenforced and "max_memory_mb" in unenforced


def test_isolated_mode_claims_no_gaps():
    """隔离模式下不该再报告「未强制」—— 否则那个告警会变成背景噪音。"""
    assert SandboxLimits(mode="isolated").unenforced() == ()


def test_describe_says_it_is_not_a_security_boundary():
    """``describe()`` 供 ``doctor`` 与启动日志用，它必须把边界说清楚。"""
    text = Sandbox(SandboxLimits(mode="soft")).describe()
    assert "soft" in text
    assert "不是安全边界" in text
    assert "max_cpu_s" in text


def test_describe_in_isolated_mode_has_no_warning():
    text = Sandbox(SandboxLimits(mode="isolated")).describe()
    assert "不是安全边界" not in text


def test_constructing_a_soft_sandbox_warns_once(caplog):
    """构造期**告警一次**（而不是每次调用都刷屏）。"""
    import logging

    with caplog.at_level(logging.WARNING, logger="tool.sandbox"):
        Sandbox(SandboxLimits(mode="soft"))
    assert any("没有真正生效" in r.message for r in caplog.records)


def test_from_config_reads_the_yaml_shape():
    limits = SandboxLimits.from_config(
        {
            "limits": {"timeout_s": 5, "max_output_bytes": 100, "max_cpu_s": 1},
            "sandbox": {"mode": "isolated", "clean_env": False, "allow_network": True},
        }
    )
    assert limits.mode == "isolated"
    assert limits.timeout_s == 5.0
    assert limits.max_output_bytes == 100
    assert limits.clean_env is False
    assert limits.allow_network is True


def test_from_config_rejects_unknown_mode():
    with pytest.raises(ValueError, match="未知的 sandbox.mode"):
        SandboxLimits.from_config({"sandbox": {"mode": "container"}})


def test_enforced_set_matches_the_documented_table():
    """``ENFORCED_IN_SOFT`` 要与文档里那张表一致 —— 它是给文档核对用的。"""
    assert "timeout_s" in ENFORCED_IN_SOFT
    assert "max_output_bytes" in ENFORCED_IN_SOFT
    assert "clean_env" in ENFORCED_IN_SOFT
    assert not (ENFORCED_IN_SOFT & LIMITS_NEEDING_ISOLATION), (
        "一个限制不能既「已强制」又「需要硬隔离」"
    )
