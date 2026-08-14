#!/usr/bin/env node
/**
 * MoviePilot fnOS 前端服务
 * =========================
 * 作用：
 *   - 提供 MoviePilot 前端静态资源（dist 目录）
 *   - 将 /api 与 /cookiecloud 反向代理到 Python 后端
 *   - SPA 回退到 index.html
 *
 * 环境变量：
 *   MP_FRONTEND_DIR  前端 dist 目录（必填）
 *   MP_BACKEND_HOST  后端地址，默认 127.0.0.1
 *   MP_BACKEND_PORT  后端端口，默认 3001
 *   MP_FRONTEND_PORT 本服务监听端口，默认 3000
 */
"use strict";

const path = require("path");
const fs = require("fs");
const http = require("http");

const FRONTEND_DIR = process.env.MP_FRONTEND_DIR;
const BACKEND_HOST = process.env.MP_BACKEND_HOST || "127.0.0.1";
const BACKEND_PORT = parseInt(process.env.MP_BACKEND_PORT || "3001", 10);
const FRONTEND_PORT = parseInt(process.env.MP_FRONTEND_PORT || "3000", 10);

if (!FRONTEND_DIR) {
  console.error("[moviepilot-frontend] MP_FRONTEND_DIR is not set");
  process.exit(1);
}

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".webp": "image/webp",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".eot": "application/vnd.ms-fontobject",
  ".txt": "text/plain; charset=utf-8",
  ".map": "application/json",
};

function contentType(p) {
  return MIME[path.extname(p).toLowerCase()] || "application/octet-stream";
}

// 幂等方法：连接级失败（多半是复用了被对端静默关闭的 keep-alive 连接）时
// 自动重试一次，避免前端健康检查（如 system/ping）偶发失败误报"正在重新连接"。
const IDEMPOTENT_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

function proxyRequest(req, res, pathname, retried) {
  // 构造安全的后端转发路径：只对 pathname 编码，避免 ERR_UNESCAPED_CHARACTERS。
  // query string 由浏览器已正确 URL 编码，必须原样透传，绝不能再次 encodeURI——
  // 否则 `server=%E9%A3%9E...` 里的 `%` 会被二次编码成 `%25`，后端解码后得到
  // 字面字符串 `%E9%A3%9E...`，导致按 server 名匹配媒体服务器配置失败、
  // 媒体库数据加载失败（如 mediacenter/library?server=飞牛影视）。
  function encodeSafePath(p) {
    let s;
    try {
      s = encodeURI(p);
    } catch (e) {
      s = "/";
    }
    return s;
  }

  // 从 req.url 提取 query string（如 "?a=1&b=2"，无则空串）。
  // 注意：不能使用 req.url.search —— req.url 是字符串，.search 是 String.prototype 的原生方法，
  // 会得到 "function search() { [native code] }"，拼接出畸形路径导致后端 404。
  let queryStr = "";
  try {
    const rawUrl = req.url || "";
    const qIdx = rawUrl.indexOf("?");
    if (qIdx >= 0) queryStr = rawUrl.slice(qIdx);
  } catch (e) {
    queryStr = "";
  }

  let fullPath;
  try {
    // 只对 pathname 做安全编码，query 已由浏览器编码、原样透传，避免二次编码
    fullPath = encodeSafePath(pathname) + queryStr;
  } catch (e) {
    fullPath = "/";
  }
  // 空路径兜底
  if (!fullPath || fullPath === "") fullPath = "/";

  const options = {
    hostname: BACKEND_HOST,
    port: BACKEND_PORT,
    path: fullPath,
    method: req.method,
    headers: Object.assign({}, req.headers, {
      host: `${BACKEND_HOST}:${BACKEND_PORT}`,
    }),
  };

  let proxy;
  try {
    proxy = http.request(options, (pRes) => {
      const headers = Object.assign({}, pRes.headers);
      res.writeHead(pRes.statusCode, headers);
      pRes.pipe(res);
    });
  } catch (err) {
    console.error("[moviepilot-frontend] proxy create error:", err.message);
    if (!res.headersSent) {
      res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Bad Gateway");
    }
    return;
  }

  proxy.on("error", (err) => {
    console.error("[moviepilot-frontend] proxy error:", err.message);
    if (!res.headersSent) {
      // 幂等请求的连接级错误（ECONNRESET/EPIPE/ETIMEDOUT 等，通常源于后端
      // uvicorn 的短 keep-alive 超时静默关闭了复用连接），重试一次即可成功，
      // 避免后端轻微抖动被放大为前端连接状态误判。
      if (!retried && IDEMPOTENT_METHODS.has(req.method)) {
        console.error("[moviepilot-frontend] retrying once:", req.method, pathname);
        try { proxy.destroy(); } catch (e) {}
        proxyRequest(req, res, pathname, true);
        return;
      }
      res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Bad Gateway: backend not reachable");
    } else {
      res.end();
    }
  });

  // 防止 req 读取出错导致崩溃
  req.on("error", (err) => {
    console.error("[moviepilot-frontend] request error:", err.message);
    try { proxy.destroy(); } catch (e) {}
  });

  try {
    req.pipe(proxy);
  } catch (e) {
    console.error("[moviepilot-frontend] pipe error:", e.message);
    if (!res.headersSent) {
      res.writeHead(502);
      res.end();
    }
  }
}

function serveStatic(req, res, pathname) {
  // 归一化，防止路径穿越
  let rel;
  try {
    rel = decodeURIComponent(pathname);
  } catch (e) {
    rel = pathname;
  }
  if (rel.indexOf("\0") !== -1) {
    res.writeHead(400);
    res.end();
    return;
  }
  let filePath = path.normalize(path.join(FRONTEND_DIR, rel));
  if (!filePath.startsWith(path.normalize(FRONTEND_DIR))) {
    res.writeHead(403);
    res.end("Forbidden");
    return;
  }

  if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
    // SPA 回退：非文件请求全部返回 index.html
    const indexFile = path.join(FRONTEND_DIR, "index.html");
    if (fs.existsSync(indexFile)) {
      res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
      fs.createReadStream(indexFile).pipe(res);
      return;
    }
    res.writeHead(404);
    res.end("Not Found");
    return;
  }

  res.writeHead(200, { "Content-Type": contentType(filePath) });
  fs.createReadStream(filePath).pipe(res);
}

const server = http.createServer((req, res) => {
  const pathname = req.url.split("?")[0];

  // API 与 CookieCloud 代理到后端
  if (pathname === "/api" || pathname.startsWith("/api/") ||
      pathname === "/cookiecloud" || pathname.startsWith("/cookiecloud/")) {
    proxyRequest(req, res, pathname);
    return;
  }

  serveStatic(req, res, pathname);
});

server.on("error", (err) => {
  console.error("[moviepilot-frontend] server error:", err.message);
  // 端口被占用(EADDRINUSE)时，短暂等待再退出，给残留进程释放端口留出时间，
  // 避免 supervisor 立即拉起重启再次抢端口形成风暴。
  if (err.code === "EADDRINUSE") {
    console.error("[moviepilot-frontend] port already in use, waiting before exit");
    setTimeout(() => process.exit(1), 3000);
  } else {
    process.exit(1);
  }
});

// 拉长 keep-alive 存活时间，与 gateway-proxy.py 连接池(60s)匹配，
// 避免代理池复用的空闲连接被 Node 默认 keepAliveTimeout(5s) 提前关闭，
// 导致 "request failed, retry fresh: ... Remote end closed connection without response"。
server.keepAliveTimeout = 60000;
server.headersTimeout = 61000;

server.listen(FRONTEND_PORT, "127.0.0.1", () => {
  console.log(`[moviepilot-frontend] serving ${FRONTEND_DIR} on 127.0.0.1:${FRONTEND_PORT}`);
  console.log(`[moviepilot-frontend] backend at ${BACKEND_HOST}:${BACKEND_PORT}`);
});

// 优雅退出
process.on("SIGTERM", () => {
  console.log("[moviepilot-frontend] SIGTERM received, shutting down");
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 3000).unref();
});
process.on("SIGINT", () => {
  process.exit(0);
});
