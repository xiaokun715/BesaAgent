"""CLI 端到端测试。

**这是唯一覆盖「从命令行到模型回答」整条链路的测试**。
单测全绿不代表能跑起来 —— 配置文件名、字段名、包路径、异步入口，
任何一处对不上都只有在这里才会暴露。

用 ``--env test``：钉在 mock 上，无需密钥、不碰网络。
"""

from __future__ import annotations

import contextlib
import io
import logging

import pytest

from apps.cli.main import main


def run_cli(*argv: str) -> tuple[int, str, str]:
    """跑一次 CLI，捕获退出码与输出。"""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


@pytest.fixture(autouse=True)
def quiet_logs():
    """CLI 会调 setup_logging，把日志从 stderr 挪走，避免污染断言。"""
    logging.getLogger().handlers.clear()
    yield
    logging.getLogger().handlers.clear()


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #


def test_doctor_lists_aliases_and_models():
    code, out, _ = run_cli("--env", "test", "doctor")

    assert code == 0
    assert "runtime.default" in out
    assert "emb.default" in out
    assert "runtime-mock" in out


def test_doctor_shows_why_a_model_is_unavailable():
    """``doctor`` 的主要价值：把「为什么这个模型没被用到」变成一条能读的原因。

    ``configs/base.yaml`` 里定义了真实厂商的模型，而测试环境没有密钥 ——
    它们应当显示为不可用**并给出原因**，而不是从列表里消失。
    """
    code, out, _ = run_cli("--env", "test", "doctor")

    assert code == 0
    assert "runtime-openai" in out, "不可用的模型必须仍然出现在列表里"
    assert "不可用" in out
    assert "OPENAI_API_KEY" in out, "原因里要指明缺哪个变量"


def test_doctor_does_not_leak_secrets(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-doctor-test-1234567890")
    _, out, err = run_cli("--env", "test", "doctor")

    assert "sk-doctor-test-1234567890" not in out
    assert "sk-doctor-test-1234567890" not in err


# --------------------------------------------------------------------------- #
# runtime
# --------------------------------------------------------------------------- #


def test_chat_prints_answer_and_summary():
    code, out, _ = run_cli("--env", "test", "runtime", "帮我设计登录接口的测试用例")

    assert code == 0
    assert "[mock]" in out
    # 结果摘要必须包含这几个事实 —— 没有它们，用户无法回答「这次到底用了什么」
    assert "runtime-mock" in out
    assert "runtime.default" in out
    assert "trace" in out


def test_chat_stream_mode():
    code, out, _ = run_cli("--env", "test", "runtime", "讲个故事", "--stream")

    assert code == 0
    assert "[mock]" in out
    assert "runtime-mock" in out


def test_config_error_is_reported_without_traceback(monkeypatch):
    """配置错误是**用户可见**的失败，不该以回溯形式抛出 ——
    回溯会淹没「哪个文件的哪个字段错了」这条真正有用的信息。"""
    from foundation.settings import SettingsError

    def explode(*_args, **_kwargs):
        raise SettingsError("配置缺少必需的字段 'gateway.retry'（已加载：base.yaml）")

    monkeypatch.setattr("apps.cli.main.open_runtime", explode)

    code, _, err = run_cli("--env", "test", "runtime", "x")

    assert code == 2, "配置错误用退出码 2，与运行时失败（1）区分开"
    assert "配置错误" in err
    assert "gateway.retry" in err
    assert "Traceback" not in err


def test_secrets_in_config_errors_are_redacted(monkeypatch):
    """配置错误里也可能带出密钥（比如 URL 里嵌了 key）—— 同样要盖住。"""
    from foundation.settings import SettingsError

    def explode(*_args, **_kwargs):
        raise SettingsError("bad url https://x/v1?api_key=sk-leaked1234567890")

    monkeypatch.setattr("apps.cli.main.open_runtime", explode)

    _, _, err = run_cli("--env", "test", "runtime", "x")
    assert "sk-leaked1234567890" not in err


def test_bad_argument_exits_with_usage():
    with pytest.raises(SystemExit) as excinfo:
        run_cli("runtime", "x", "--deadline", "not-a-number")
    assert excinfo.value.code == 2


def test_unknown_command_exits_nonzero():
    with pytest.raises(SystemExit) as excinfo:
        run_cli("nonsense")
    assert excinfo.value.code == 2


def test_missing_subcommand_exits_nonzero():
    with pytest.raises(SystemExit) as excinfo:
        run_cli("--env", "test")
    assert excinfo.value.code == 2
