#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gateway-proxy.py 注入的 JS polyfill 的离线冒烟测试（Windows 也能跑）。

核心诉求：网关把应用嵌在 `/app/moviepilot` 前缀下，页面里的前端代码却按
根路径发请求，所以代理会往 HTML 的 <head> 注入一段 polyfill 去改写 URL。
这段 polyfill 是**字符串拼出来的 JS**，Python 侧完全不校验语法 ——
一旦拼错（少个分号、多了个引号），表现是"页面照常打开、但某个功能静默失效"，
极难定位。本测试把真实 polyfill 抽出来，做语法与行为两层校验：

  A 语法：抽出的 JS 必须能通过 JS 语法检查（<script> 内层）
  B 覆盖：fetch / XHR / WebSocket **以及 EventSource** 都要被改写
  C 行为：EventSource 绝对路径加前缀、已带前缀不重复加（防双前缀）
  D 兼容：相对路径与 http(s) 绝对 URL 原样透传，第二参数透传
  E 能力保留：EventSource 的原型方法（close）与静态常量（CLOSED 等）不能丢
  F 兜底：EventSource 不存在（老浏览器）时 polyfill 不得抛错
  G 端到端：真实 ProxyHandler 把含 EventSource 包装的 polyfill 注入 HTML

为什么要单独测 EventSource：MoviePilot 的实时消息流（通知中心、系统日志）
用的是 `new EventSource(url)`，它**不走 fetch/XHR**，天然绕过前两者的改写。
历史上这段 polyfill 只覆盖 fetch/XHR/WebSocket，EventSource 是缺的。

依赖：需要 `node`（执行 JS）。没有 node 时明确报 SKIP 并返回 0，不误判失败。
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROXY = ROOT / "app" / "bin" / "gateway-proxy.py"
SANDBOX = ROOT / ".local-build" / "_smoke" / "polyfill"
GATEWAY_PREFIX = "/app/moviepilot"

FAILS = []


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label
          + (f"  <- {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(label)


# ---------------------------------------------------------------------------
# 抽取真实 polyfill（不 import 目标模块：它在 __main__ 外也要读 sys.argv）
# ---------------------------------------------------------------------------
def extract_polyfill() -> tuple[str, str]:
    """返回 (完整片段含 <script>, 内层 JS)。"""
    src = PROXY.read_text(encoding="utf-8")
    prefix = (
        'APPNAME = "moviepilot"\n'
        f'GATEWAY_PREFIX = "{GATEWAY_PREFIX}"\n'
    )
    m = re.search(
        r"def _build_polyfill\(\):.*?\n_POLYFILL_BYTES = _build_polyfill\(\)\.encode\(\)",
        src, re.S,
    )
    if not m:
        raise RuntimeError("未能在 gateway-proxy.py 中定位 _build_polyfill")
    ns: dict = {}
    exec(prefix + m.group(0), ns)  # noqa: S102 —— 只执行本仓库自己的一小段源码
    full = ns["_POLYFILL_BYTES"].decode("utf-8")

    inner = re.match(r"^<script>(.*)</script>$", full, re.S)
    if not inner:
        raise RuntimeError("polyfill 未被 <script> 正确包裹")
    return full, inner.group(1)


def node_ok() -> bool:
    return shutil.which("node") is not None


def node(*args, **kw):
    """执行 node 并强制 UTF-8 解码。

    必须显式指定 encoding/errors：Windows 上 text=True 会按 GBK 解码，
    而子进程输出是 UTF-8，中文/特殊字符会抛 UnicodeDecodeError（在读线程里
    直接炸掉，且异常发生在后台线程、不影响 returncode，容易误判为成功）。
    """
    return subprocess.run(
        ["node", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kw,
    )


# ---------------------------------------------------------------------------
def main() -> int:
    print("== gateway-proxy polyfill 冒烟测试 ==\n")

    if not PROXY.exists():
        check("gateway-proxy.py 存在", False, str(PROXY))
        return 1

    full, inner = extract_polyfill()

    # --- A/B 静态检查 -------------------------------------------------------
    check("A. polyfill 被 <script> 包裹", full.startswith("<script>") and full.endswith("</script>"))
    check("B. 改写 fetch", "window.fetch=function" in inner)
    check("B. 改写 XMLHttpRequest", "XMLHttpRequest.prototype.open=function" in inner)
    check("B. 改写 WebSocket", "window.WebSocket=function" in inner)
    check("B. 改写 EventSource（本次新增，曾缺失）", "window.EventSource=function" in inner)
    check("B. 复制 EventSource 静态常量",
          '_esC=["CONNECTING","OPEN","CLOSED"]' in inner)
    check("B. 网关前缀来自 GATEWAY_PREFIX 常量",
          f'var P="{GATEWAY_PREFIX}";' in inner)

    if not node_ok():
        print("\nSKIP: 未找到 node，无法做语法与行为校验（不计为失败）")
        print(f"\n== 静态检查: {len(FAILS)} failed ==")
        return 1 if FAILS else 0

    SANDBOX.mkdir(parents=True, exist_ok=True)
    inner_js = SANDBOX / "_polyfill_inner.js"
    inner_js.write_text(inner, encoding="utf-8")

    # --- A 语法检查 ---------------------------------------------------------
    r = node("--check", str(inner_js))
    check("A. polyfill JS 语法检查通过", r.returncode == 0,
          (r.stderr or "").strip().splitlines()[:3])

    # --- C/D/E/F 行为检查（Node 沙箱执行真实 polyfill）----------------------
    runner = SANDBOX / "_es_behavior.js"
    runner.write_text(_BEHAVIOR_JS, encoding="utf-8")
    r = node(str(runner))
    ok = r.returncode == 0
    check("C-F. EventSource 行为/兼容/能力保留", ok,
          (r.stdout or "") + (r.stderr or ""))
    if r.stdout:
        for line in r.stdout.strip().splitlines():
            print("      " + line)

    # --- G 端到端：真实 ProxyHandler 注入 HTML ------------------------------
    e2e = SANDBOX / "_proxy_e2e.js"
    e2e.write_text(_E2E_JS, encoding="utf-8")
    r = node(str(e2e))
    check("G. 真实 ProxyHandler 端到端注入 polyfill", r.returncode == 0,
          (r.stdout or "") + (r.stderr or ""))
    if r.stdout:
        for line in r.stdout.strip().splitlines():
            print("      " + line)

    print()
    if FAILS:
        print(f"== 结果: {len(FAILS)} 项失败 ==")
        for f in FAILS:
            print("   - " + f)
        return 1
    print("== 结果: 全部通过 ==")
    return 0


# ---------------------------------------------------------------------------
# 行为测试脚本：在最小浏览器桩上跑真实 polyfill
# ---------------------------------------------------------------------------
_BEHAVIOR_JS = r"""
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");
const inner = fs.readFileSync(path.join(__dirname, "_polyfill_inner.js"), "utf-8");
const P = "/app/moviepilot";

function sandbox() {
  const calls = { es: [] };
  class FakeES {
    constructor(u, o) { this.url = u; this.opts = o; this.readyState = 0; calls.es.push([u, o]); }
    close() { this.readyState = 2; }
  }
  FakeES.CONNECTING = 0; FakeES.OPEN = 1; FakeES.CLOSED = 2;
  class FakeXHR { open() {} }
  class FakeWS { constructor() {} close() {} }
  FakeWS.prototype.send = function () {};
  const window = {
    fetch: function () { return Promise.resolve(); },
    EventSource: FakeES, WebSocket: FakeWS, XMLHttpRequest: FakeXHR,
    location: { protocol: "http:", host: "192.168.0.8:5666" },
  };
  const g = { window, location: window.location, XMLHttpRequest: FakeXHR, console };
  return { window, calls, g };
}
function run(s) { vm.createContext(s.g); vm.runInContext(inner, s.g, { filename: "polyfill.js" }); }

let fail = 0;
function t(name, cond) { console.log((cond ? "  ok   " : "  BAD  ") + name); if (!cond) fail++; }

let s = sandbox(); run(s);
new s.window.EventSource("/api/v1/system/message?role=notification");
t("绝对路径加前缀", s.calls.es[0][0] === P + "/api/v1/system/message?role=notification");

s = sandbox(); run(s);
new s.window.EventSource(P + "/api/v1/system/message");
t("已带前缀不重复加", s.calls.es[0][0] === P + "/api/v1/system/message");

s = sandbox(); run(s);
new s.window.EventSource("api/v1/system/message");
t("相对路径原样透传", s.calls.es[0][0] === "api/v1/system/message");
new s.window.EventSource("http://other/x");
t("http 绝对 URL 原样透传", s.calls.es[1][0] === "http://other/x");

s = sandbox(); run(s);
new s.window.EventSource("/api/x", { withCredentials: true });
t("第二参数透传", s.calls.es[0][1] && s.calls.es[0][1].withCredentials === true);

s = sandbox(); run(s);
const es = new s.window.EventSource("/api/x");
t("保留 close()", typeof es.close === "function");
es.close();
t("close() 生效", es.readyState === 2);
t("保留静态常量 CLOSED", s.window.EventSource.CLOSED === 2);
t("保留静态常量 OPEN", s.window.EventSource.OPEN === 1);

s = sandbox(); delete s.window.EventSource; delete s.g.window.EventSource;
let threw = null;
try { run(s); } catch (e) { threw = e; }
t("EventSource 缺失时不抛错", threw === null);

process.exit(fail === 0 ? 0 : 1);
"""

# ---------------------------------------------------------------------------
# 端到端脚本：真实 ProxyHandler（Windows 无 AF_UNIX，打桩后挂 TCP）
# ---------------------------------------------------------------------------
_E2E_JS = r"""
"use strict";
const { spawn, spawnSync } = require("child_process");
const http = require("http"), path = require("path"), fs = require("fs");

const REPO = path.resolve(__dirname, "..", "..", "..");
const FRONTEND_PORT = 39082, PROXY_PORT = 39083;
const INDEX_HTML = "<!doctype html><html><head><title>MoviePilot</title></head><body>ok</body></html>";
let jsHits = 0;

let fail = 0;
function t(name, cond, extra) {
  console.log((cond ? "  ok   " : "  BAD  ") + name + (cond || !extra ? "" : "  <- " + extra));
  if (!cond) fail++;
}
function get(port, p) {
  return new Promise((res, rej) => {
    const rq = http.request({ host: "127.0.0.1", port, path: p, method: "GET" }, (r) => {
      let b = ""; r.on("data", (d) => (b += d)); r.on("end", () => res({ status: r.statusCode, body: b }));
    });
    rq.on("error", rej); rq.end();
  });
}

const fe = http.createServer((req, res) => {
  if (req.url.startsWith("/api/")) {
    res.writeHead(401, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ detail: "Not authenticated" }));
    return;
  }
  // 静态缓存对照探针：每次响应都带自增计数，用于判断代理是否命中 LRU。
  //   cache-probe.js      普通 .js -> 应当进缓存（两次响应一致）
  //   service-worker.js   稳定文件名 PWA 资源 -> 必须不进缓存（两次响应不同）
  const p = req.url.split("?")[0];
  if (p.endsWith("/cache-probe.js") || p.endsWith("/service-worker.js")) {
    jsHits += 1;
    res.writeHead(200, { "Content-Type": "application/javascript; charset=utf-8" });
    res.end("// hits=" + jsHits);
    return;
  }
  res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
  res.end(INDEX_HTML);
});

(async () => {
  if (spawnSync("python", ["-V"]).status !== 0) { console.log("  ok   (skip: no python)"); process.exit(0); }
  await new Promise((r) => fe.listen(FRONTEND_PORT, "127.0.0.1", r));

  const proxyPath = path.join(REPO, "app", "bin", "gateway-proxy.py").replace(/\\/g, "\\\\");
  const runner = `
import sys, importlib.util, http.server, socket
if not hasattr(socket, "AF_UNIX"):
    socket.AF_UNIX = socket.AF_INET
spec = importlib.util.spec_from_file_location("gw", r"${proxyPath}")
gw = importlib.util.module_from_spec(spec)
sys.argv = ["gateway-proxy.py", "unused.sock", "127.0.0.1", "${FRONTEND_PORT}"]
spec.loader.exec_module(gw)
class H(gw.ProxyHandler): pass
http.server.ThreadingHTTPServer(("127.0.0.1", ${PROXY_PORT}), H).serve_forever()
`;
  const proc = spawn("python", ["-c", runner], { stdio: ["ignore", "pipe", "pipe"] });
  let err = "";
  proc.stderr.on("data", (d) => (err += d.toString()));

  const deadline = Date.now() + 10000;
  let up = false;
  while (Date.now() < deadline) {
    try { await get(PROXY_PORT, "/__ping__"); up = true; break; }
    catch (e) { await new Promise((r) => setTimeout(r, 150)); }
  }

  try {
    t("真实 ProxyHandler 已启动", up, err.slice(0, 300));
    if (!up) return;
    const html = await get(PROXY_PORT, "/app/moviepilot/");
    t("HTML 返回 200", html.status === 200, "status=" + html.status);
    t("注入 <script> polyfill", html.body.includes("window.fetch=function"));
    t("注入含 EventSource 包装", html.body.includes("window.EventSource=function"));
    t("注入含 EventSource 静态常量", html.body.includes('_esC=["CONNECTING","OPEN","CLOSED"]'));
    t("polyfill 在 </head> 之前",
      html.body.indexOf("window.EventSource=function") < html.body.indexOf("</head>"));
    t("前缀常量正确", html.body.includes('var P="/app/moviepilot";'));
    const api = await get(PROXY_PORT, "/app/moviepilot/api/v1/system/message?role=notification");
    t("API 前缀剥离并透传 401（非 502）", api.status === 401, "status=" + api.status);
    // 静态缓存行为：普通 .js 进 LRU，稳定文件名 PWA 资源必须绕过 LRU。
    // 不修的话，升级后浏览器会一直用旧 Service Worker 拉旧 chunk —— 表现是
    // 界面报「服务器返回了无效响应」且"刷新后重试"永远无效。
    const p1 = await get(PROXY_PORT, "/app/moviepilot/cache-probe.js");
    const p2 = await get(PROXY_PORT, "/app/moviepilot/cache-probe.js");
    t("对照：普通 .js 命中 LRU 缓存（两次响应一致）",
      p1.body === p2.body, p1.body + " vs " + p2.body);
    const s1 = await get(PROXY_PORT, "/app/moviepilot/service-worker.js");
    const s2 = await get(PROXY_PORT, "/app/moviepilot/service-worker.js");
    t("service-worker.js 绕过 LRU（两次响应来自后端）",
      s1.body !== s2.body, s1.body + " vs " + s2.body);
  } finally {
    proc.kill(); fe.close();
  }
  process.exit(fail === 0 ? 0 : 1);
})();
"""


if __name__ == "__main__":
    sys.exit(main())
