"""HTTP 面:它只是 api 的一个调用方,自己不实现任何规则。"""

from __future__ import annotations

import base64
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from collectbase import serve


@pytest.fixture
def server(repo):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve._handler(repo.git, None))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def call(path, payload=None, raw=False):
        req = urllib.request.Request(base + path)
        if payload is not None:
            req.data = json.dumps(payload).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as r:
                body = r.read()
                return (r.status, body if raw else json.loads(body))
        except urllib.error.HTTPError as e:
            return (e.code, json.loads(e.read()))

    yield call
    httpd.shutdown()


def test_页面能打开(server):
    status, body = server("/", raw=True)
    assert status == 200 and b"collectbase" in body


def test_info_列出层(server):
    status, data = server("/api/info")
    assert status == 200 and data["layers"] == ["facts", "notes", "beliefs"]


def test_列目录带归属层(server):
    status, data = server("/api/list?path=project")
    names = {i["name"]: i for i in data["items"]}
    assert names["api.md"]["layer"] == "facts"


def test_上传再读回来(server, repo):
    data = base64.b64encode(b"hello\n").decode()
    status, out = server("/api/commit", {
        "layer": "notes", "message": "上传",
        "ops": [{"op": "put", "path": "project/up.md", "content": data}],
    })
    assert status == 200, out
    assert "project/up.md" in repo.own("refs/heads/layer/notes")

    status, body = server("/api/file?path=project/up.md", raw=True)
    assert status == 200 and body == b"hello\n"


def test_上传二进制_读回来是字节不是软链(server, repo):
    raw = b"\x00" + os.urandom(2000)
    server("/api/commit", {
        "layer": "facts", "message": "一张图",
        "ops": [{"op": "put", "path": "project/p.png", "content": base64.b64encode(raw).decode()}],
    })
    assert repo.sh("ls-tree", "layer/facts", "project/p.png").stdout.split()[0] == "120000"
    status, body = server("/api/file?path=project/p.png", raw=True)
    assert status == 200 and body == raw


def test_越界的写被守卫拒_信息原样转出(server):
    status, out = server("/api/commit", {
        "layer": "beliefs", "message": "顺手改事实",
        "ops": [{"op": "put", "path": "project/api.md",
                 "content": base64.b64encode(b"x").decode()}],
    })
    assert status == 409
    assert "属于层 [facts]" in out["error"]


def test_删除(server, repo):
    server("/api/commit", {"layer": "notes", "message": "加",
        "ops": [{"op": "put", "path": "t.md", "content": base64.b64encode(b"x").decode()}]})
    status, _ = server("/api/commit", {"layer": "notes", "message": "删",
        "ops": [{"op": "delete", "path": "t.md"}]})
    assert status == 200
    assert "t.md" not in repo.own("refs/heads/layer/notes")


def test_读不存在的文件是_404(server):
    status, out = server("/api/file?path=nope.md")
    assert status == 404 and "没有这个文件" in out["error"]


def test_不给_token_不许绑定非回环地址(tmp_path):
    with pytest.raises(serve.ServeError, match="拒绝绑定"):
        serve.serve(tmp_path, host="0.0.0.0", port=0)
