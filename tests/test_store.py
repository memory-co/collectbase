"""blob 的异地副本:远端走纯 CAS,拉回来前验哈希。"""

from __future__ import annotations

import os

import pytest

from collectbase import blob, store


def test_远端_key_是纯内容寻址():
    local = "blob/2026/07/23/" + "a" * 64 + ".png"
    assert store.remote_key(local) == "aa/aa/" + "a" * 64 + ".png"


def test_同一份内容跨天只占一份远端空间():
    sha = "b" * 64
    assert store.remote_key(f"blob/2026/07/23/{sha}.png") == \
        store.remote_key(f"blob/2026/09/01/{sha}.png")


def test_不认识的地址会说清楚():
    with pytest.raises(store.StoreError, match="不认识"):
        store.open_store("ftp://nope/x")


def test_push_再_pull_能把库整个恢复(repo, tmp_path):
    repo.on("facts").write("project/a.png", b"\x00" + os.urandom(2000))
    repo.commit("[facts] 截图 a", stay=True)
    repo.on("facts").write("project/b.png", b"\x00" + os.urandom(2000))
    repo.commit("[facts] 截图 b", stay=True)

    remote = store.open_store(f"file://{tmp_path / 'remote'}")
    up = store.push(repo.git, remote)
    assert len(up.moved) == 2 and up.ok

    # git clean -xdf 把库删光
    import shutil
    shutil.rmtree(repo.root / "blob")
    assert not blob.verify(repo.git).ok

    down = store.pull(repo.git, remote)
    assert len(down.moved) == 2 and down.ok
    assert blob.verify(repo.git).ok


def test_push_是幂等的(repo, tmp_path):
    repo.on("facts").write("project/a.png", b"\x00" + os.urandom(500))
    repo.commit("[facts] 截图", stay=True)
    remote = store.open_store(f"file://{tmp_path / 'r'}")
    assert len(store.push(repo.git, remote).moved) == 1
    again = store.push(repo.git, remote)
    assert again.moved == [] and again.skipped == 1


def test_pull_不覆盖哈希对不上的本地文件(repo, tmp_path):
    """那是有人绕过机制改了字节的现场,不是能自动修复的东西。"""
    repo.on("facts").write("project/a.png", b"\x00" + os.urandom(500))
    repo.commit("[facts] 截图", stay=True)
    remote = store.open_store(f"file://{tmp_path / 'r'}")
    store.push(repo.git, remote)

    target = next((repo.root / "blob").rglob("*.png"))
    target.chmod(0o644)
    target.write_bytes(b"\x00tampered")

    report = store.pull(repo.git, remote)
    assert not report.ok
    assert "哈希对不上" in next(iter(report.failed.values()))
    assert target.read_bytes() == b"\x00tampered", "现场必须保留"


def test_远端不覆盖已有对象(repo, tmp_path):
    """内容寻址:同 key 必然同内容,写一次就不再改。"""
    remote_dir = tmp_path / "r"
    remote = store.open_store(f"file://{remote_dir}")
    repo.on("facts").write("project/a.png", b"\x00" + os.urandom(500))
    repo.commit("[facts] 截图", stay=True)
    store.push(repo.git, remote)

    key = next(iter(remote.list()))
    (remote_dir / key).write_bytes(b"\x00someone-else")
    store.push(repo.git, remote)
    assert (remote_dir / key).read_bytes() == b"\x00someone-else", "put 不该覆盖"
