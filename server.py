#!/usr/bin/env python3
"""轻量 HTTP 服务器 — 提供 output 目录的静态文件访问（feed.xml 等）"""

import http.server
import os
import socketserver
import sys

# 额外的 MIME 类型
_EXTRA_MIME = {
    ".xml": "application/rss+xml; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


class RSSHandler(http.server.SimpleHTTPRequestHandler):
    """为 feed.xml 等文件返回正确的 Content-Type"""

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def guess_type(self, path):
        ext = os.path.splitext(path)[1].lower()
        if ext in _EXTRA_MIME:
            return _EXTRA_MIME[ext]
        return super().guess_type(path)

    def log_message(self, fmt, *args):
        # 简洁日志: 时间 请求行
        sys.stderr.write(f"[HTTP] {self.log_date_time_string()} {fmt % args}\n")


def serve(output_dir: str, host: str = "0.0.0.0", port: int = 8080):  # noqa: S104
    """启动 HTTP 服务器（阻塞调用）"""
    os.chdir(output_dir)

    with socketserver.TCPServer((host, port), RSSHandler) as httpd:
        httpd.allow_reuse_address = True
        print(f"[HTTP] 服务目录: {output_dir}")
        print(f"[HTTP] 监听: http://{host}:{port}")
        print(f"[HTTP] RSS Feed: http://{host}:{port}/feed.xml")
        httpd.serve_forever()


if __name__ == "__main__":
    project_dir = os.path.dirname(os.path.abspath(__file__))
    output_dir = os.path.join(project_dir, "output")
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    serve(output_dir, "0.0.0.0", port)  # noqa: S104
