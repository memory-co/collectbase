"""`cb serve` —— 路径 CRUD 的 HTTP 面,外加一个分层文件管理器。

**它没有特权。**写进去的每个提交都要过和别人完全相同的那道闸;规则一条都不
在这里实现,守卫拒了就把那段话原样转出去——页面上显示的字,就是命令行里会
看到的字。

**默认只听 127.0.0.1。**这个接口能往事实层写,是整套设计里信任度最高的位置;
不配 token 就拒绝绑定非回环地址,启动时直接报错。

见 docs/v2/works/server.md。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import secrets
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import api
from .gitrepo import Git

LOOPBACK = ("127.0.0.1", "::1", "localhost")


class ServeError(RuntimeError):
    pass


def _handler(git: Git, token: str | None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "collectbase"

        # ------------------------------------------------------ 基础设施

        def log_message(self, fmt, *args):  # 安静点
            pass

        def _authed(self) -> bool:
            if token is None:
                return True
            given = self.headers.get("X-CB-Token") or _query(self.path).get("token", [""])[0]
            return secrets.compare_digest(given or "", token)

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _fail(self, exc: Exception) -> None:
            status = getattr(exc, "status", 500)
            self._json(status, {"error": str(exc)})

        # ------------------------------------------------------------ GET

        def do_GET(self) -> None:
            if not self._authed():
                return self._json(401, {"error": "需要 token"})
            route = urlparse(self.path).path
            q = _query(self.path)
            try:
                if route == "/":
                    return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                if route == "/api/info":
                    return self._json(200, api.info(git))
                if route == "/api/list":
                    items = api.listdir(git, q.get("path", [""])[0], q.get("view", [None])[0])
                    return self._json(200, {"items": [asdict(i) for i in items]})
                if route == "/api/file":
                    path = q.get("path", [""])[0]
                    data = api.read(git, path, q.get("view", [None])[0])
                    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                    extra = {}
                    if q.get("download"):
                        name = path.rsplit("/", 1)[-1]
                        extra["Content-Disposition"] = f'attachment; filename="{name}"'
                    return self._send(200, data, ctype, extra)
            except Exception as exc:  # noqa: BLE001 —— 全部转成 JSON 错误
                return self._fail(exc)
            self._json(404, {"error": "没有这个接口"})

        # ----------------------------------------------------------- POST

        def do_POST(self) -> None:
            if not self._authed():
                return self._json(401, {"error": "需要 token"})
            if urlparse(self.path).path != "/api/commit":
                return self._json(404, {"error": "没有这个接口"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                ops = [_op(o) for o in payload.get("ops", [])]
                sha = api.commit(git, payload.get("layer", ""), payload.get("message", ""), ops)
                self._json(200, {"commit": sha})
            except Exception as exc:  # noqa: BLE001
                self._fail(exc)

    return Handler


def _op(raw: dict):
    kind = raw.get("op")
    path = raw.get("path", "")
    if kind == "delete":
        return api.Delete(path)
    if kind == "put":
        return api.Put(path, base64.b64decode(raw.get("content", "")))
    raise api.ApiError(f"不认识的操作:{kind}", 400)


def _query(path: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(path).query)


def serve(repo: str | Path, host: str = "127.0.0.1", port: int = 8787,
          token: str | None = None) -> None:
    git = Git(repo)
    if host not in LOOPBACK and not token:
        raise ServeError(
            f"拒绝绑定 {host}:这个接口能往事实层写。\n"
            "  要对外开放就先配一个 token:cb serve --host 0.0.0.0 --token <…>"
        )
    httpd = ThreadingHTTPServer((host, port), _handler(git, token))
    where = f"http://{host}:{port}/"
    print(f"collectbase 在 {where}(仓库 {git.root})")
    if token:
        print("  已启用 token")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n再见")


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>collectbase</title>
<style>
:root { --line:#e2e2e2; --dim:#888; --bg:#fff; --fg:#111; --accent:#2b6cb0; }
@media (prefers-color-scheme: dark) {
  :root { --line:#333; --dim:#999; --bg:#141414; --fg:#e8e8e8; --accent:#7aa7d9; }
}
* { box-sizing: border-box }
body { margin:0; font:14px/1.6 ui-sans-serif,-apple-system,"Segoe UI",sans-serif;
       background:var(--bg); color:var(--fg) }
header { padding:14px 20px; border-bottom:1px solid var(--line); display:flex;
         gap:14px; align-items:center; flex-wrap:wrap }
h1 { font-size:15px; margin:0; font-weight:600; letter-spacing:.02em }
select, button { font:inherit; padding:4px 10px; border:1px solid var(--line);
                 border-radius:6px; background:var(--bg); color:var(--fg) }
button { cursor:pointer }
main { padding:16px 20px 60px }
nav { color:var(--dim); margin-bottom:10px }
nav a { color:var(--accent); text-decoration:none; cursor:pointer }
table { width:100%; border-collapse:collapse }
td, th { text-align:left; padding:7px 10px; border-bottom:1px solid var(--line) }
th { color:var(--dim); font-weight:500; font-size:12px; text-transform:uppercase;
     letter-spacing:.05em }
td.n a { color:var(--fg); text-decoration:none; cursor:pointer }
td.n a:hover { color:var(--accent) }
td.r { text-align:right; color:var(--dim); white-space:nowrap }
.layer { font-size:12px; padding:1px 8px; border:1px solid var(--line);
         border-radius:99px; color:var(--dim) }
.drop { margin-top:18px; border:1.5px dashed var(--line); border-radius:10px;
        padding:26px; text-align:center; color:var(--dim) }
.drop.hot { border-color:var(--accent); color:var(--accent) }
.msg { margin-top:14px; padding:11px 14px; border-radius:8px;
       border:1px solid var(--line); white-space:pre-wrap; font-family:ui-monospace,monospace;
       font-size:13px; display:none }
.msg.on { display:block }
.err { border-color:#c0392b }
.del { color:var(--dim); cursor:pointer; border:0; background:none; font-size:12px }
</style>
<header>
  <h1>collectbase</h1>
  <label>视图 <select id=view></select></label>
  <span id=info style="color:var(--dim)"></span>
</header>
<main>
  <nav id=crumb></nav>
  <table><thead><tr><th>名字<th>层<th class=r>大小<th class=r>修改<th></thead>
  <tbody id=rows></tbody></table>
  <div class=drop id=drop>把文件拖到这里 —— 写入当前视图对应的层</div>
  <div class=msg id=msg></div>
</main>
<script>
const $ = s => document.querySelector(s);
let cwd = "", view = "all", layers = [];

const q = o => new URLSearchParams(o).toString();
const say = (text, bad) => {
  const m = $("#msg"); m.textContent = text; m.className = "msg on" + (bad ? " err" : "");
  if (!bad) setTimeout(() => m.className = "msg", 4000);
};
const human = n => n < 1024 ? n + " B"
  : n < 1048576 ? (n/1024).toFixed(1) + " KB"
  : n < 1073741824 ? (n/1048576).toFixed(1) + " MB" : (n/1073741824).toFixed(1) + " GB";
const when = t => t ? new Date(t*1000).toISOString().slice(0,10) : "";

async function boot() {
  const info = await (await fetch("/api/info")).json();
  layers = info.layers;
  $("#view").innerHTML = ['<option value="all">全部</option>']
    .concat(layers.map(l => `<option value="${l}">${l}</option>`)).join("");
  $("#info").textContent = `${layers.length} 层 · blob 活集 ${info.blob.live}`;
  $("#view").onchange = e => { view = e.target.value; load(); };
  load();
}

async function load() {
  const r = await fetch("/api/list?" + q({path: cwd, view}));
  const data = await r.json();
  if (data.error) return say(data.error, true);

  const parts = cwd ? cwd.split("/") : [];
  $("#crumb").innerHTML = ['<a data-p="">/</a>'].concat(parts.map((p, i) =>
    `<a data-p="${parts.slice(0, i+1).join("/")}">${p}</a>`)).join(" / ");
  $("#crumb").querySelectorAll("a").forEach(a =>
    a.onclick = () => { cwd = a.dataset.p; load(); });

  $("#rows").innerHTML = data.items.map(i => `<tr>
    <td class=n><a data-p="${i.path}" data-d="${i.is_dir}">${i.is_dir ? "📁 " : (i.is_blob ? "▣ " : "")}${i.name}</a>
    <td>${i.layer ? `<span class=layer>${i.layer}</span>` : ""}
    <td class=r>${i.is_dir ? "" : human(i.size)}
    <td class=r>${when(i.modified)}
    <td class=r>${i.is_dir ? "" : `<button class=del data-p="${i.path}" data-l="${i.layer||''}">删除</button>`}
  </tr>`).join("") || "<tr><td colspan=5 style='color:var(--dim)'>这里是空的</td></tr>";

  $("#rows").querySelectorAll("td.n a").forEach(a => a.onclick = () => {
    if (a.dataset.d === "true") { cwd = a.dataset.p; load(); }
    else window.open("/api/file?" + q({path: a.dataset.p, view}), "_blank");
  });
  $("#rows").querySelectorAll("button.del").forEach(b => b.onclick = () =>
    send(b.dataset.l, `删除 ${b.dataset.p}`, [{op:"delete", path:b.dataset.p}]));
}

async function send(layer, message, ops) {
  if (!layer) return say("这个文件还没有归属层,先在命令行里处理", true);
  const r = await fetch("/api/commit", {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify({layer, message, ops}),
  });
  const data = await r.json();
  if (data.error) return say(data.error, true);
  say("提交 " + data.commit.slice(0, 7));
  load();
}

const drop = $("#drop");
drop.ondragover = e => { e.preventDefault(); drop.classList.add("hot"); };
drop.ondragleave = () => drop.classList.remove("hot");
drop.ondrop = async e => {
  e.preventDefault(); drop.classList.remove("hot");
  const layer = view === "all" ? prompt("写到哪一层?" + layers.join(" / "), layers[0]) : view;
  if (!layer) return;
  const files = [...e.dataTransfer.files];
  const ops = await Promise.all(files.map(async f => ({
    op: "put",
    path: (cwd ? cwd + "/" : "") + f.name,
    content: btoa(String.fromCharCode(...new Uint8Array(await f.arrayBuffer()))),
  })));
  send(layer, `上传 ${files.length} 个文件`, ops);
};

boot();
</script>
"""
