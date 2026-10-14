#!/usr/bin/env python3
"""
WorkBuddy 统一管理器 - 主启动器
================================
启动 Python API 网关 + WebUI 控制台

功能：
  - 一键启动 API 网关
  - 一键启动 Web 控制台
  - 账号池管理
  - 积分查看
  - 成长计划任务
  - 账号切换
"""

import os
import sys
import socket
import subprocess
import threading
import time
import signal
import webbrowser
from pathlib import Path
from http.server import ThreadingHTTPServer
from datetime import datetime

# ===== 配置 =====
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "wb_data"
GATEWAY_PORT = 7863
CONSOLE_PORT = 8789

# 状态
shutdown_event = threading.Event()


def ensure_dirs():
    """确保目录存在"""
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "request-logs").mkdir(exist_ok=True)


def wait_for_http(host, port, timeout=6):
    """等待本地 HTTP 服务可连接"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def start_gateway():
    """启动 Python API 网关（独立子进程，避免线程异常导致窗口退出）"""
    print(f"[网关] 启动 Python API 网关...")
    print(f"[网关] 端口: {GATEWAY_PORT}")

    try:
        gateway_proc = subprocess.Popen(
            [sys.executable, "-u", str(BASE_DIR / "wb_gateway.py")],
            cwd=str(BASE_DIR),
        )

        if wait_for_http("127.0.0.1", GATEWAY_PORT, timeout=8):
            print("[网关] 已启动")
            return True

        print("[错误] 网关启动失败，退出")
        return False
    except Exception as e:
        print(f"[错误] 网关启动失败: {e}")
        return False


def start_console(port=CONSOLE_PORT):
    """启动 WebUI 控制台"""
    print(f"[控制台] 启动 WebUI...")
    print(f"[控制台] 地址: http://localhost:{port}")
    
    try:
        # 动态导入 WebUI 模块
        sys.path.insert(0, str(BASE_DIR))
        from wb_webui import WebUIHandler
        
        server = ThreadingHTTPServer(("127.0.0.1", port), WebUIHandler)
        server.daemon_threads = True
        
        # 在单独线程中运行
        def serve():
            server.serve_forever()
        
        t = threading.Thread(target=serve, daemon=True)
        t.start()
        
        print(f"[控制台] 已启动")
        
        return True
    except Exception as e:
        print(f"[控制台] 启动失败: {e}")
        return False


def open_console_browser(url, delay=1.5):
    """延迟在默认浏览器中打开控制台（等 HTTP 服务真正可连接之后）"""
    def _open():
        time.sleep(delay)
        try:
            opened = webbrowser.open(url)
            if not opened:
                print(f"[提示] 未能自动打开浏览器，请手动访问: {url}")
        except Exception as e:
            print(f"[提示] 自动打开浏览器失败({e})，请手动访问: {url}")

    threading.Thread(target=_open, daemon=True).start()


def print_banner():
    """打印启动横幅"""
    print("""
╔═══════════════════════════════════════════════════════════════╗
║          WorkBuddy 统一管理器 v1.0                             ║
║                                                               ║
║  • API 网关    : http://127.0.0.1:7863                        ║
║  • 管理面板    : http://127.0.0.1:7863/panel/                  ║
║  • Web 控制台  : http://localhost:8789                         ║
║                                                               ║
║  按 Ctrl+C 停止所有服务                                        ║
╚═══════════════════════════════════════════════════════════════╝
    """)


def main():
    """主函数"""
    ensure_dirs()
    print_banner()
    
    # 启动网关
    if not start_gateway():
        print("[错误] 网关启动失败，退出")
        return
    
    # 启动控制台
    console_url = f"http://localhost:{CONSOLE_PORT}/"
    if start_console():
        print(f"\n[控制台] 请在浏览器打开: {console_url}")
        open_console_browser(console_url)
    else:
        print("[错误] 控制台启动失败（端口可能被占用）")
    
    # 保持运行
    print("\n[运行中] 按 Ctrl+C 退出")
    try:
        while not shutdown_event.is_set():
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[停止] 正在关闭服务...")
    
    # 清理
    print("[完成] 所有服务已停止")


if __name__ == "__main__":
    # Windows 信号处理
    if os.name == 'nt':
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    
    main()