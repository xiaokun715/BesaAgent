"""四个内置工具的边界。

**安全相关的两条最该测**：

1. **路径逃逸** —— ``..`` 与符号链接。判定必须在 ``resolve()`` **之后**做，
   否则一个指向范围外的符号链接会通过检查（检查时看到的还是那个「看起来在范围内」的路径）。
2. **输出截断必须标注** —— 截断而不标注，会让模型基于不完整信息做判断，
   而它自己不知道。那比报错更糟：报错它换个做法，静默截断它会基于错的输入继续推理。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from foundation.clock import FakeClock
from tool.base import ToolContext, truncate
from tool.bash import DENYLIST, BashTool
from tool.grep import GrepTool
from tool.read import ReadTool
from tool.registry import build_default_registry
from tool.write import WriteTool


def _ctx(workdir: Path, **over) -> ToolContext:
    base = {
        "scope": "s-1",
        "allowed_paths": (workdir.resolve(),),
        "max_output_bytes": 4096,
        "max_output_lines": 100,
        "clock": FakeClock(),
    }
    base.update(over)
    return ToolContext(**base)


def _ctx_no_scope(**over) -> ToolContext:
    base = {"allowed_paths": (), "max_output_bytes": 4096, "max_output_lines": 100}
    base.update(over)
    return ToolContext(**base)


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #


async def test_read_returns_file_content(tmp_path: Path):
    (tmp_path / "a.txt").write_text("你好\n世界\n", encoding="utf-8")
    result = await ReadTool().run({"path": "a.txt"}, _ctx(tmp_path))
    assert result.outcome == "executed"
    assert "你好" in result.output
    assert not result.truncated


async def test_read_supports_line_range(tmp_path: Path):
    (tmp_path / "a.txt").write_text("1\n2\n3\n4\n5\n", encoding="utf-8")
    result = await ReadTool().run({"path": "a.txt", "start_line": 2, "end_line": 3}, _ctx(tmp_path))
    assert result.output == "2\n3\n"


async def test_read_refuses_paths_outside_the_scope(tmp_path: Path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("秘密", encoding="utf-8")
    try:
        result = await ReadTool().run({"path": str(outside)}, _ctx(tmp_path))
        assert result.outcome == "refused"
        assert "超出可读范围" in result.error
    finally:
        outside.unlink(missing_ok=True)


async def test_read_refuses_dotdot_escape(tmp_path: Path):
    """``..`` 必须在 ``resolve()`` 之后被判掉。"""
    result = await ReadTool().run({"path": "../../../etc/hosts"}, _ctx(tmp_path))
    assert result.outcome == "refused"


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="平台不支持符号链接")
async def test_read_refuses_symlink_escape(tmp_path: Path):
    """**符号链接逃逸必须被拒。**

    这是「先 resolve 再判定」的全部意义：如果先判定再 resolve，
    检查时看到的是那个「在范围内」的链接路径，而真正读到的是范围外的文件。
    """
    target = tmp_path / "inside"
    target.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("不该被读到", encoding="utf-8")

    link = target / "link.txt"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("没有创建符号链接的权限")

    result = await ReadTool().run({"path": "inside/link.txt"}, _ctx(target))
    assert result.outcome == "refused", "通过符号链接读到范围外的文件是不可接受的"


async def test_read_refuses_when_no_scope_is_configured(tmp_path: Path):
    """**范围默认拒绝**：没配就是不许访问。"""
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    result = await ReadTool().run({"path": "a.txt"}, _ctx_no_scope())
    assert result.outcome == "refused"


async def test_read_marks_truncation(tmp_path: Path):
    """截断**必须标注**。"""
    (tmp_path / "big.txt").write_text("x" * 10_000, encoding="utf-8")
    result = await ReadTool().run({"path": "big.txt"}, _ctx(tmp_path, max_output_bytes=100))
    assert result.truncated is True
    assert len(result.output) <= 100


def test_truncate_does_not_split_multibyte_characters():
    """按字节截断必须**回退到字符边界**。

    直接切字节会把一个多字节字符劈成两半，得到无法解码的字节 ——
    而报错会出现在很远的地方（写文件/编码时），离这里很远。
    """
    text = "中" * 100  # 每个 3 字节
    out, cut = truncate(text, max_bytes=10, max_lines=1000)
    assert cut
    assert out.encode("utf-8")  # 能编码回去，说明没劈开字符
    assert "中" in out


# --------------------------------------------------------------------------- #
# grep
# --------------------------------------------------------------------------- #


async def test_grep_finds_matches_with_positions(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\ny = 2\nx = 3\n", encoding="utf-8")
    result = await GrepTool().run({"pattern": r"x = ", "glob": "*.py"}, _ctx(tmp_path))
    assert result.outcome == "executed"
    assert ":1:" in result.output and ":3:" in result.output


async def test_grep_caps_the_number_of_matches(tmp_path: Path):
    """**单次返回条数上限** —— 一次搜索能把整个仓库命中，
    结果塞进上下文会把后续推理挤爆，而模型不知道「你只给了它前几条」。"""
    (tmp_path / "a.txt").write_text("\n".join(f"hit {i}" for i in range(500)), encoding="utf-8")
    result = await GrepTool().run({"pattern": "hit", "max_matches": 10}, _ctx(tmp_path))
    assert result.truncated is True
    assert len(result.output.splitlines()) == 10


async def test_grep_refuses_invalid_regex(tmp_path: Path):
    """正则写错是**参数问题**，该回给模型让它改，不是执行失败。"""
    result = await GrepTool().run({"pattern": "([unclosed"}, _ctx(tmp_path))
    assert result.outcome == "refused"
    assert "正则非法" in result.error


async def test_grep_skips_vcs_directories(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "packed.txt").write_text("needle", encoding="utf-8")
    (tmp_path / "src.txt").write_text("needle", encoding="utf-8")

    result = await GrepTool().run({"pattern": "needle"}, _ctx(tmp_path))
    assert "src.txt" in result.output
    assert ".git" not in result.output


# --------------------------------------------------------------------------- #
# write
# --------------------------------------------------------------------------- #


async def test_write_overwrites(tmp_path: Path):
    result = await WriteTool().run({"path": "a.txt", "content": "新内容"}, _ctx(tmp_path))
    assert result.outcome == "executed"
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "新内容"


async def test_write_appends(tmp_path: Path):
    (tmp_path / "a.txt").write_text("第一行\n", encoding="utf-8")
    await WriteTool().run({"path": "a.txt", "content": "第二行\n", "mode": "append"}, _ctx(tmp_path))
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "第一行\n第二行\n"


async def test_write_refuses_outside_the_scope(tmp_path: Path):
    result = await WriteTool().run({"path": "../evil.txt", "content": "x"}, _ctx(tmp_path))
    assert result.outcome == "refused"


async def test_write_rejects_unknown_mode(tmp_path: Path):
    result = await WriteTool().run({"path": "a.txt", "content": "x", "mode": "truncate"}, _ctx(tmp_path))
    assert result.outcome == "refused"
    assert "未知的 mode" in result.error


# --------------------------------------------------------------------------- #
# bash
# --------------------------------------------------------------------------- #


def test_bash_is_not_in_the_default_registry():
    """**默认关闭**（`DT-6`）。

    它等于把一台无锁的机器交给模型 —— 「忘了关」的代价不可逆。
    """
    assert "bash" not in build_default_registry().names
    assert "bash" in build_default_registry(enabled=["read", "bash"]).names


@pytest.mark.parametrize("command", DENYLIST)
async def test_bash_blocks_denylisted_commands(command: str, tmp_path: Path):
    result = await BashTool().run({"command": command}, _ctx(tmp_path))
    assert result.outcome == "refused"
    assert "拒绝清单" in result.error
    assert "不是安全边界" in result.error, "错误信息里要说清它不是安全边界"


async def test_bash_runs_a_command(tmp_path: Path):
    result = await BashTool().run({"command": "echo hello"}, _ctx(tmp_path))
    assert result.outcome == "executed"
    assert "hello" in result.output


async def test_bash_reports_nonzero_exit_as_failure(tmp_path: Path):
    """非零退出是**执行失败**而不是拒绝 —— 命令跑了，只是它失败了。

    这一区分很重要：上层据此判断「要不要重试」。
    """
    result = await BashTool().run({"command": "exit 3"}, _ctx(tmp_path))
    assert result.outcome == "failed"
    assert "3" in result.error


async def test_bash_timeout_kills_the_process(tmp_path: Path):
    """超时**必须真的杀掉进程**。

    只放弃等待的话子进程会继续跑 —— 它的副作用还在发生，
    而调用方以为「这次超时了、什么都没做」。
    """
    result = await BashTool().run(
        {"command": "sleep 5", "timeout_s": 0.2}, _ctx(tmp_path)
    )
    assert result.outcome == "failed"
    assert "超时" in result.error
    assert "副作用" in result.error, "要说清「超时前它可能已经产生了副作用」"


async def test_bash_refuses_cwd_outside_scope(tmp_path: Path):
    result = await BashTool().run({"command": "echo x", "cwd": "../.."}, _ctx(tmp_path))
    assert result.outcome == "refused"
