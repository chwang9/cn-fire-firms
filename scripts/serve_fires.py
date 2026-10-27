#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cn-fire-firms 本地服务：托管火点地图 + 代理 FIRMS Area API

为什么需要代理：
  FIRMS Area API 的响应**不带 Access-Control-Allow-Origin**（实测确认），
  浏览器里的 JS 直接 fetch 必然被 CORS 拦截（实测 TypeError: Failed to fetch）。
  所以「在 HTML 界面输入 MAP_KEY 点按钮拉火点」这条路必须由本地进程转发，
  由本进程用 MAP_KEY 请求 FIRMS，再把 CSV 回给页面（同源，无跨域问题）。

安全边界：
  * MAP_KEY 只在请求时转发给 FIRMS，不落盘、不写回 HTML、不出现在日志里。
  * 只监听 127.0.0.1，不对外暴露。
  * 代理只允许固定的 FIRMS Area API 端点，不接受任意 URL（防 SSRF）。

用法：
    python serve_fires.py --dir <地图输出目录> [--port 8765]
    然后浏览器打开 http://127.0.0.1:8765/fire_map.html
"""

import argparse
import http.server
import json
import os
import socketserver
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fire_firms_cn as F  # noqa: E402


class Handler(http.server.SimpleHTTPRequestHandler):
    # 只托管这一个文件。基类原本会把整个工作目录变成可读的文件服务器
    # （实测 GET / 返回完整目录列表、/secret.txt 直接返回内容），职责远超
    # 「转发 Area API + 托管地图页」，因此除 API 端点外全部按白名单放行。
    serve_file = "fire_map.html"

    def log_message(self, fmt, *args):
        # 屏蔽默认访问日志，避免噪音；出错仍由我们自己打印
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _text(self, code, msg):
        body = msg.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        """允许 file:// 打开的地图页访问本代理。

        页面以 file:// 打开时 origin 为 null，属跨源请求，必须有 CORS 头才能读到响应。
        风险评估：本代理从不存储 MAP_KEY（key 由调用方随请求传入），
        因此外部站点无法借此套取用户配额，最坏情况只是转发攻击者自己的 key。
        """
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/fires/health":
            return self._json(200, {"ok": True, "service": "cn-fire-firms"})
        # 透明镜像官方 Area API 路径形态：
        #   /api/area/csv/{MAP_KEY}/{SOURCE}/{BBOX}/{DAYS}[/{DATE}]
        # 路径与官方接口完全一致，浏览器里看到的 URL 就是真实请求形态；
        # 本地前缀 /api 代替主机名，其余原样转发给 FIRMS。
        if parsed.path.startswith("/api/area/csv/"):
            return self._area_mirror(parsed.path)
        return self._serve_only()

    def _forbid(self, reason):
        self._text(404, f"404 Not Found\n\n本服务仅提供：\n"
                        f"  GET /{self.serve_file}                （地图页）\n"
                        f"  GET /api/area/csv/...               （Area API 代理）\n"
                        f"  GET /api/fires/health               （健康检查）\n\n"
                        f"拒绝原因：{reason}")

    def list_directory(self, path):
        """禁止目录列表（基类默认会把整个目录列出来）。"""
        self._forbid("目录列表已禁用")

    def _serve_only(self):
        """只允许访问白名单里的那一个 HTML；其余路径一律 404。

        不用 super().do_GET()：那会退化成通用静态文件服务器，把 --dir 目录下
        的所有（包括无关与敏感的）文件都暴露在 127.0.0.1 端口上。
        """
        parsed = urllib.parse.urlparse(self.path)
        rel = urllib.parse.unquote(parsed.path).lstrip("/")
        if rel in ("", "/", self.serve_file):
            # 只用白名单文件名拼接，不把用户请求的任何路径片段拼进 os.path
            target = os.path.join(getattr(self, "serve_root", os.getcwd()), self.serve_file)
            if not os.path.isfile(target):
                return self._text(404, f"404 Not Found：{self.serve_file} 不在当前目录")
            try:
                with open(target, "rb") as fh:
                    body = fh.read()
            except OSError as e:
                return self._text(500, f"读取失败：{e}")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(body)
            return
        self._forbid("仅托管指定的地图页面")

    def _area_mirror(self, path):
        """把 /api/area/csv/{KEY}/{SOURCE}/{BBOX}/{DAYS}[/{DATE}] 原样转发给 FIRMS。"""
        rest = path[len("/api/area/csv/"):]
        parts = [urllib.parse.unquote(p) for p in rest.split("/") if p != ""]
        if len(parts) < 4:
            return self._json(400, {
                "error": "路径需为 /api/area/csv/{MAP_KEY}/{SOURCE}/{BBOX}/{DAYS}[/{DATE}]"})
        key, source, bbox, days = parts[0], parts[1], parts[2], parts[3]
        date = parts[4] if len(parts) > 4 else ""
        return self._do_area(key, bbox, source, days, date)

    def _do_area(self, key, bbox, source, days, date):
        if not key:
            return self._json(400, {"error": "缺少 MAP_KEY"})
        if source not in F.FIRMS_AREA_SOURCES:
            return self._json(400, {"error": f"非法 SOURCE：{source}",
                                    "allowed": F.FIRMS_AREA_SOURCES})
        try:
            url = F.area_api_url(key, bbox, source, days, date or None)
        except ValueError as e:
            return self._json(400, {"error": str(e)})

        try:
            csv_text, _ = F.fetch_area_csv(key, bbox, source, days,
                                           date or None, timeout=120, retries=2)
        except RuntimeError as e:
            # 把 FIRMS 的原文原因透出（如 Invalid MAP_KEY.），便于用户自查
            return self._json(502, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            return self._json(502, {"error": f"{type(e).__name__}: {e}"})

        # 出站日志：打印实际向 FIRMS 请求的完整 URL（MAP_KEY 脱敏）。
        # 排障时最需要看的就是「到底请求了哪个 URL」，而不是猜。
        print(f"[area] -> {F.area_api_url(key, bbox, source, days, date or None).replace(key, '<MAP_KEY>')}")
        print(f"[area] <- {len(csv_text)} bytes, "
              f"{max(0, len(csv_text.strip().splitlines()) - 1)} 行数据")

        body = csv_text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    ap = argparse.ArgumentParser(description="cn-fire-firms 本地服务（地图托管 + Area API 代理）")
    ap.add_argument("--dir", default=".", help="地图输出目录（含 fire_map.html）")
    ap.add_argument("--file", default="fire_map.html", help="要托管的 HTML 文件名（默认 fire_map.html）")
    ap.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    ap.add_argument("--open", action="store_true", help="启动后自动用默认浏览器打开地图")
    args = ap.parse_args()

    root = os.path.abspath(args.dir)
    html = os.path.join(root, args.file)
    if not os.path.exists(html):
        print(f"✗ 未找到 {html}；请先用 fire_firms_cn.py 生成地图，或用 --dir 指定输出目录")
        return 2

    # serve_root 必须显式带上：托管改由自身的白名单逻辑实现，
    # 不再依赖基类的 directory= ，因此不能依赖进程 cwd。
    handler = type("MapHandler", (Handler,),
                   {"serve_file": args.file, "serve_root": root})
    url = f"http://127.0.0.1:{args.port}/{args.file}"
    with ThreadedServer(("127.0.0.1", args.port), handler) as httpd:
        print(f"本地服务已启动：{url}")
        print(f"  目录：{root}")
        print("  MAP_KEY 仅在请求时转发给 FIRMS，不落盘；Ctrl+C 退出")
        print("  提示：地图页即使以 file:// 打开也能连上本代理（已开 CORS）")
        if args.open:
            try:
                import webbrowser
                webbrowser.open(url)
                print("  已尝试在默认浏览器打开")
            except Exception as e:  # noqa: BLE001
                print(f"  自动打开失败（不影响服务）：{e}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n已退出。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
