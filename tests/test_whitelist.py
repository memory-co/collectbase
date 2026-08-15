"""git 里只允许两种形态。白名单是 blob 机制反过来读的结果。

--no-verify / cherry-pick / rebase 都不跑 pre-commit,所以没有守卫这一侧,
裸二进制就能直接进写入面。
"""

from __future__ import annotations

import os


def test_裸二进制绕过_pre_commit_也进不去(repo):
    """--no-verify 跳过 pre-commit,blobify 不发生;白名单必须拦下来。"""
    repo.write("project/raw.png", b"\x00" + os.urandom(50_000))
    out = repo.commit("[facts] 裸二进制", no_verify=True)
    assert out.returncode != 0
    assert "二进制" in out.stderr and "blob" in out.stderr


def test_绝对路径软链被拒(repo):
    (repo.root / "project/leak.txt").symlink_to("/etc/hostname")
    out = repo.commit("[facts] 逃逸软链", no_verify=True)
    assert out.returncode != 0
    assert "blob/" in out.stderr


def test_爬出仓库的相对软链被拒(repo):
    (repo.root / "project/leak2.txt").symlink_to("../../../etc/passwd")
    out = repo.commit("[facts] 爬出去", no_verify=True)
    assert out.returncode != 0
    assert "blob/" in out.stderr


def test_submodule_被拒(repo):
    head = repo.head()
    repo.sh("update-index", "--add", "--cacheinfo", f"160000,{head},vendor/sub")
    out = repo.sh("commit", "-q", "--no-verify", "-m", "[facts] submodule", check=False)
    assert out.returncode != 0
    assert "submodule" in out.stderr


def test_纯文本与合法软链照常放行(repo):
    repo.write("project/plain.md", "just text\n")
    assert repo.commit("[facts] 纯文本").returncode == 0
    repo.write("project/pic.png", b"\x00" + os.urandom(1000))
    assert repo.commit("[facts] 图片").returncode == 0
    assert repo.sh("ls-tree", "HEAD", "project/pic.png").stdout.split()[0] == "120000"
