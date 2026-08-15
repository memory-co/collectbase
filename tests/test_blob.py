"""二进制不进 git,只进 blob/;git 里留一条相对软链。"""

from __future__ import annotations

import os
import subprocess

from collectbase import blob


def sizeof(repo, rev, path) -> int:
    return int(repo.sh("cat-file", "-s", f"{rev}:{path}").stdout.strip())


def mode(repo, rev, path) -> str:
    return repo.sh("ls-tree", rev, path).stdout.split()[0]


# ------------------------------------------------------------------- 判据

def test_判据与_file_命令一致():
    """.jsonl 是 application/x-ndjson,按 mime-type 判会把事实层整个搬走。"""
    corpus = {
        b'{"a":1}\n{"a":2}\n': False,          # jsonl
        b"def f():\n    pass\n": False,        # python
        b"": False,                            # 空文件:file 报 binary,但不该入库
        b"\x89PNG\r\n\x1a\n\x00\x00": True,
        b"x" * 200_000: False,                 # 大文本仍是文本
    }
    for data, want in corpus.items():
        assert blob.is_binary(data) is want


def test_判据与系统_file_逐项吻合(tmp_path):
    samples = {
        "a.jsonl": b'{"a":1}\n',
        "b.py": b"print(1)\n",
        "c.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
        "d.bin": bytes(range(256)),
        "e.md": b"# hi\n",
    }
    for name, data in samples.items():
        p = tmp_path / name
        p.write_bytes(data)
        got = subprocess.run(
            ["file", "-b", "--mime-encoding", str(p)], capture_output=True, text=True
        ).stdout.strip()
        assert blob.is_binary(data) is (got == "binary"), name


def test_超大文本按阈值兜底():
    assert blob.is_binary(b"x" * 100, size=200, threshold=100) is True
    assert blob.is_binary(b"x" * 100, size=50, threshold=100) is False


# ------------------------------------------------------------------- 路径

def test_软链是相对路径且指回_blob():
    target = blob.link_target("project/log/screen.png", "blob/2026/07/23/abc.png")
    assert target == "../../blob/2026/07/23/abc.png"
    assert blob.points_into_store("project/log/screen.png", target)


def test_逃逸软链被识别():
    assert not blob.points_into_store("project/x.txt", "/etc/hostname")
    assert not blob.points_into_store("project/x.txt", "../../../etc/passwd")
    assert not blob.points_into_store("project/x.txt", "../project/api.md")


# ---------------------------------------------------------------- 端到端

def test_二进制提交后_git_里只有软链(repo):
    repo.write("project/screen.png", os.urandom(300_000))
    assert repo.commit("[facts] 截图").returncode == 0

    assert mode(repo, "HEAD", "project/screen.png") == "120000"
    assert sizeof(repo, "HEAD", "project/screen.png") < 120
    target = repo.sh("cat-file", "blob", "HEAD:project/screen.png").stdout.strip()
    assert target.startswith("../../blob/") or target.startswith("../blob/")
    assert (repo.root / "project" / target).resolve().exists()


def test_改文件产生新_blob_旧提交仍可解析(repo):
    repo.write("project/screen.png", b"\x00" + b"a" * 1000)
    repo.commit("[facts] v1")
    old = repo.head()
    # 已 blobify 的路径是指向 444 blob 的软链:直接写会穿过去撞上只读位,
    # 换内容要先删掉软链。
    (repo.root / "project/screen.png").unlink()
    repo.write("project/screen.png", b"\x00" + b"b" * 2000)
    repo.commit("[facts] v2")

    for rev in (old, "HEAD"):
        target = repo.sh("cat-file", "blob", f"{rev}:project/screen.png").stdout.strip()
        assert (repo.root / "project" / target).resolve().read_bytes()
    assert len(list((repo.root / "blob").rglob("*.png"))) == 2


def test_活集来自历史而不是工作区(repo):
    repo.write("project/a.png", b"\x00" + b"a" * 100)
    repo.commit("[facts] a")
    (repo.root / "project/a.png").unlink()
    repo.write("project/a.png", b"\x00" + b"b" * 100)
    repo.commit("[facts] b")
    live = blob.live_set(repo.git)
    assert len(live) == 2, "旧提交的软链必须仍然算活的"


def test_verify_能发现字节被换掉(repo):
    repo.write("project/shot.png", b"\x00" + b"real" * 100)
    repo.commit("[facts] 证据")
    assert blob.verify(repo.git).ok

    target = next((repo.root / "blob").rglob("*.png"))
    target.chmod(0o644)
    target.write_bytes(b"\x00" + b"fake" * 100)
    report = blob.verify(repo.git)
    assert not report.ok and report.mismatched


def test_gc_只删没人引用的(repo):
    repo.write("project/keep.png", b"\x00keep")
    repo.commit("[facts] keep")
    stray = repo.root / "blob/2026/01/01"
    stray.mkdir(parents=True)
    (stray / ("0" * 64 + ".png")).write_bytes(b"\x00orphan")

    dead = blob.gc(repo.git, dry_run=True)
    assert len(dead) == 1 and dead[0].endswith("0.png")
    blob.gc(repo.git)
    assert not (stray / ("0" * 64 + ".png")).exists()
    assert blob.verify(repo.git).ok


def test_误_add_的原始对象被定点清除(repo):
    """git add 那一刻原始字节已经进了对象库;换成软链后要把它删掉。"""
    data = b"\x00" + os.urandom(200_000)
    repo.write("project/big.iso", data)
    repo.sh("add", "-A")
    orphan = repo.sh("rev-parse", ":project/big.iso").stdout.strip()
    assert repo.sh("cat-file", "-s", orphan).stdout.strip() == str(len(data))

    repo.sh("commit", "-q", "-m", "[facts] iso", check=False)
    assert mode(repo, "HEAD", "project/big.iso") == "120000"
    assert repo.sh("cat-file", "-e", orphan, check=False).returncode != 0, "孤儿对象应已被删除"
