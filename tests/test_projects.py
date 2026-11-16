import subprocess
from pathlib import Path

import pytest

from anixcode import projects


def test_find_repos_skips_junk_and_does_not_descend(tmp_path):
    (tmp_path / "a" / ".git").mkdir(parents=True)
    (tmp_path / "a" / "nested" / ".git").mkdir(parents=True)  # inside a repo: not listed
    (tmp_path / "group" / "b" / ".git").mkdir(parents=True)
    (tmp_path / "node_modules" / "c" / ".git").mkdir(parents=True)
    (tmp_path / ".hidden" / "d" / ".git").mkdir(parents=True)
    names = sorted(p.name for p in projects.find_repos([tmp_path]))
    assert names == ["a", "b"]


def test_remember_mru(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    recent = projects.remember([], a)
    recent = projects.remember(recent, b)
    recent = projects.remember(recent, a)
    assert recent == [str(a.resolve()), str(b.resolve())]
    many = []
    for i in range(30):
        many = projects.remember(many, tmp_path / str(i))
    assert len(many) == projects.MAX_RECENT


def test_unsuitable_and_urls(tmp_path):
    assert projects.is_unsuitable(Path.home())
    assert projects.is_unsuitable(Path(Path.home().anchor))
    assert not projects.is_unsuitable(tmp_path)
    assert projects.GIT_URL.match("https://github.com/an1zy/ai-rpc")
    assert projects.GIT_URL.match("git@github.com:an1zy/ai-rpc.git")
    assert not projects.GIT_URL.match("C:\\Users\\me\\proj")
    assert projects.repo_name("https://github.com/an1zy/ai-rpc.git") == "ai-rpc"
    assert projects.repo_name("git@github.com:an1zy/anixcode") == "anixcode"


@pytest.mark.skipif(subprocess.run(["git", "--version"], capture_output=True).returncode != 0, reason="no git")
def test_clone_local_repo(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    target = projects.clone(str(src), tmp_path / "out")
    assert target == tmp_path / "out" / "src" and (target / ".git").exists()
    with pytest.raises(RuntimeError, match="уже существует"):
        projects.clone(str(src), tmp_path / "out")
