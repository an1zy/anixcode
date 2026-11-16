import asyncio
import sys

import pytest

from anixcode.tools import ToolContext, ToolError, default_tools

T = default_tools()


def run(name, args, ctx):
    return asyncio.run(T[name].run(args, ctx))


def test_read_numbers_lines_and_pages(tmp_path):
    (tmp_path / "a.py").write_text("\n".join(f"line{i}" for i in range(1, 11)))
    ctx = ToolContext(tmp_path)
    r = run("read", {"path": "a.py", "offset": 3, "limit": 2}, ctx)
    assert "3\tline3" in r.output and "4\tline4" in r.output and "offset=5" in r.output


def test_paths_outside_project_are_refused(tmp_path):
    with pytest.raises(ToolError, match="outside"):
        run("read", {"path": "../../etc/passwd"}, ToolContext(tmp_path))


def test_edit_requires_read_and_unique_match(tmp_path):
    f = tmp_path / "a.py"
    f.write_text("x = 1\ny = 1\n")
    ctx = ToolContext(tmp_path)
    with pytest.raises(ToolError, match="Read"):
        run("edit", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}, ctx)
    run("read", {"path": "a.py"}, ctx)
    with pytest.raises(ToolError, match="2 times"):
        run("edit", {"path": "a.py", "old_string": "= 1", "new_string": "= 2"}, ctx)
    r = run("edit", {"path": "a.py", "old_string": "x = 1", "new_string": "x = 2"}, ctx)
    assert f.read_text() == "x = 2\ny = 1\n" and r.added == 1 and r.removed == 1 and "+x = 2" in r.diff
    r = run("edit", {"path": "a.py", "old_string": "= ", "new_string": "== ", "replace_all": True}, ctx)
    assert f.read_text() == "x == 2\ny == 1\n"


def test_edit_detects_outside_changes(tmp_path):
    import os
    import time
    f = tmp_path / "a.py"
    f.write_text("a\n")
    ctx = ToolContext(tmp_path)
    run("read", {"path": "a.py"}, ctx)
    f.write_text("b\n")
    os.utime(f, (time.time() + 5, time.time() + 5))
    with pytest.raises(ToolError, match="changed"):
        run("edit", {"path": "a.py", "old_string": "b", "new_string": "c"}, ctx)


def test_write_creates_file_with_diff(tmp_path):
    ctx = ToolContext(tmp_path)
    r = run("write", {"path": "src/new.py", "content": "print(1)\n"}, ctx)
    assert (tmp_path / "src" / "new.py").read_text() == "print(1)\n" and r.added == 1
    assert T["write"].preview({"path": "src/new.py", "content": "x"}, ctx) is not None


def test_ls_glob_grep(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("def hello():\n    return 1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.py").write_text("def hello(): pass\n")
    ctx = ToolContext(tmp_path)
    assert "mod.py" in run("ls", {}, ctx).output and "node_modules" not in run("ls", {}, ctx).output
    assert run("glob", {"pattern": "**/*.py"}, ctx).output == "pkg/mod.py"
    out = run("grep", {"pattern": r"def \w+", "include": "*.py"}, ctx).output
    assert "pkg/mod.py:1" in out and "junk" not in out
    with pytest.raises(ToolError):
        run("grep", {"pattern": "("}, ctx)


@pytest.mark.skipif(sys.platform == "win32", reason="posix shell")
def test_bash_output_exit_code_and_timeout(tmp_path):
    ctx = ToolContext(tmp_path)
    r = run("bash", {"command": "echo hi; exit 3"}, ctx)
    assert "hi" in r.output and "exit code 3" in r.output and r.error
    r = run("bash", {"command": "sleep 5", "timeout": 1}, ctx)
    assert "timed out" in r.output


def test_glob_by_name_skips_profile_junk_and_stops_on_time(tmp_path):
    from anixcode.tools import files

    (tmp_path / "AppData" / "x").mkdir(parents=True)
    (tmp_path / "AppData" / "x" / "calc.py").write_text("")
    (tmp_path / "proj" / "deep").mkdir(parents=True)
    (tmp_path / "proj" / "deep" / "calc.py").write_text("")
    ctx = ToolContext(tmp_path)
    assert run("glob", {"pattern": "calc.py"}, ctx).output == "proj/deep/calc.py"
    assert run("glob", {"pattern": "**/calc.py"}, ctx).output == "proj/deep/calc.py"
    old = files.GlobTool.TIME_LIMIT
    files.GlobTool.TIME_LIMIT = -1
    try:
        assert "stopped" in run("glob", {"pattern": "*.x"}, ctx).output
    finally:
        files.GlobTool.TIME_LIMIT = old
