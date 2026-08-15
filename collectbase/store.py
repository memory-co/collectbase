"""blob 的异地副本。

仓库不自足:`git clone` 只带走软链不带字节,`git clean -xdf` 会删光 `blob/`。
这里给它一个可以拉回来的地方。

后端只有四个方法。一份 S3 兼容实现覆盖 S3 / OSS / R2 / MinIO,外加一个
``file://``(就是个目录,可以是 NAS 挂载点)。**不做多后端抽象层**,四个方法
而已。

远端走**纯内容寻址**,本地保留日期分片:

    本地   blob/2026/07/23/f1a4f247…ac.png     为了能当媒体库浏览
    远端   f1/a4/f1a4f247…ac.png               为了去重

同一份内容在不同日子被添加会产生两个本地路径,远端照抄就要付两份存储费。
恢复不需要额外映射——软链目标里同时含有日期路径和 sha256。

见 docs/v2/works/server.md §4。
"""

from __future__ import annotations

import posixpath
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol
from urllib.parse import urlparse

from . import blob
from .gitrepo import Git


class StoreError(RuntimeError):
    pass


class Store(Protocol):
    def exists(self, key: str) -> bool: ...
    def put(self, key: str, path: Path) -> None: ...
    def get(self, key: str, path: Path) -> None: ...
    def list(self, prefix: str = "") -> Iterable[str]: ...


def remote_key(local_path: str) -> str:
    """``blob/2026/07/23/<sha>.png`` → ``<sha[0:2]>/<sha[2:4]>/<sha>.png``"""
    name = posixpath.basename(local_path)
    sha = blob.sha_of_store_path(local_path)
    if sha is None:
        raise StoreError(f"{local_path} 的文件名不是 sha256,不该在库里")
    return f"{sha[:2]}/{sha[2:4]}/{name}"


# ------------------------------------------------------------------ 后端

@dataclass
class FileStore:
    """一个目录。NAS 挂载点、外接盘、或者只是另一块盘上的备份。"""

    root: Path

    def _at(self, key: str) -> Path:
        return self.root / key

    def exists(self, key: str) -> bool:
        return self._at(key).is_file()

    def put(self, key: str, path: Path) -> None:
        dest = self._at(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            return  # 内容寻址:同 key 必然同内容,永不覆盖
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copyfile(path, tmp)
        tmp.replace(dest)

    def get(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._at(key), path)

    def list(self, prefix: str = "") -> Iterable[str]:
        base = self.root / prefix if prefix else self.root
        if not base.is_dir():
            return []
        return sorted(
            p.relative_to(self.root).as_posix() for p in base.rglob("*") if p.is_file()
        )


@dataclass
class S3Store:
    """S3 兼容。boto3 是可选依赖,没装就在这里说清楚,而不是甩个 ImportError。"""

    bucket: str
    prefix: str = ""
    endpoint: str | None = None
    access_key_id: str | None = None
    access_key_secret: str | None = None

    def __post_init__(self) -> None:
        try:
            import boto3  # noqa: F401
        except ImportError:
            raise StoreError(
                "S3 后端需要 boto3:pip install 'collectbase[s3]'"
            ) from None

    @property
    def _client(self):
        import boto3

        if not hasattr(self, "_cached"):
            self._cached = boto3.client(
                "s3",
                endpoint_url=self.endpoint,
                aws_access_key_id=self.access_key_id,
                aws_secret_access_key=self.access_key_secret,
            )
        return self._cached

    def _key(self, key: str) -> str:
        return f"{self.prefix.rstrip('/')}/{key}" if self.prefix else key

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except ClientError:
            return False

    def put(self, key: str, path: Path) -> None:
        if self.exists(key):
            return  # 写一次就不再改
        self._client.upload_file(str(path), self.bucket, self._key(key))

    def get(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, self._key(key), str(path))

    def list(self, prefix: str = "") -> Iterable[str]:
        token, full = None, self._key(prefix) if prefix else self.prefix
        cut = len(self.prefix.rstrip("/")) + 1 if self.prefix else 0
        while True:
            kw = {"Bucket": self.bucket, "Prefix": full}
            if token:
                kw["ContinuationToken"] = token
            page = self._client.list_objects_v2(**kw)
            for obj in page.get("Contents", []):
                yield obj["Key"][cut:]
            if not page.get("IsTruncated"):
                return
            token = page.get("NextContinuationToken")


def open_store(url: str, **extra) -> Store:
    """``file:///srv/blobs`` 或 ``s3://bucket/prefix``。"""
    parsed = urlparse(url)
    if parsed.scheme in ("", "file"):
        return FileStore(Path(parsed.path or url).expanduser())
    if parsed.scheme in ("s3", "oss", "r2"):
        return S3Store(
            bucket=parsed.netloc,
            prefix=parsed.path.lstrip("/"),
            endpoint=extra.get("endpoint"),
            access_key_id=extra.get("access_key_id"),
            access_key_secret=extra.get("access_key_secret"),
        )
    raise StoreError(f"不认识的存储地址:{url}(支持 file:// 和 s3://)")


# ------------------------------------------------------------------ 同步

@dataclass
class SyncReport:
    moved: list[str]
    skipped: int
    failed: dict[str, str]

    @property
    def ok(self) -> bool:
        return not self.failed


def push(git: Git, store: Store) -> SyncReport:
    """把活集里还没上传的 blob 传上去。幂等:key 是内容哈希,存在就跳过。"""
    moved, skipped, failed = [], 0, {}
    for path in sorted(blob.live_set(git)):
        local = git.root / path
        if not local.is_file():
            failed[path] = "本地缺这个文件"
            continue
        key = remote_key(path)
        try:
            if store.exists(key):
                skipped += 1
                continue
            store.put(key, local)
            moved.append(path)
        except Exception as exc:  # 上传失败不阻塞任何东西,记下来下轮重试
            failed[path] = str(exc)
    return SyncReport(moved, skipped, failed)


def pull(git: Git, store: Store) -> SyncReport:
    """把活集里本地缺的 blob 拉回来,**写盘前验 sha256**。

    已存在且哈希正确的跳过;已存在但哈希不对的**报错而不是覆盖**——那是
    `cb check` I4 要人看的现场,不是能自动"修复"的东西。
    """
    moved, skipped, failed = [], 0, {}
    for path in sorted(blob.live_set(git)):
        local = git.root / path
        want = blob.sha_of_store_path(path)
        if local.is_file():
            if want and blob.sha256_of(local.read_bytes()) != want:
                failed[path] = "本地这份哈希对不上,不覆盖 —— 先看看是谁改的"
            else:
                skipped += 1
            continue
        key = remote_key(path)
        try:
            tmp = local.with_suffix(local.suffix + ".part")
            store.get(key, tmp)
            if want and blob.sha256_of(tmp.read_bytes()) != want:
                tmp.unlink(missing_ok=True)
                failed[path] = "拉回来的内容哈希对不上"
                continue
            tmp.replace(local)
            local.chmod(0o444)
            moved.append(path)
        except Exception as exc:
            failed[path] = str(exc)
    return SyncReport(moved, skipped, failed)
