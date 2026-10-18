#!/usr/bin/env python3
"""测试 WebUI 启动"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wb_webui import WebUIHandler
from http.server import ThreadingHTTPServer

print("启动 WebUI 测试...")
server = ThreadingHTTPServer(('127.0.0.1', 8789), WebUIHandler)
print("WebUI 启动在 http://localhost:8789")
print("按 Ctrl+C 停止...")
try:
    server.serve_forever()
except KeyboardInterrupt:
    print("\n停止...")
    server.shutdown()