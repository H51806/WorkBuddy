#!/usr/bin/env python3
"""
WorkBuddy 统一管理器 - 主启动器
================================
启动 Python API 网关 + WebUI 控制台

功能：
  - 一键启动 API 网关
  - 一键启动老控制台（仅保留 /api/* 与 /legacy 兜底，根路径 302 跳转统一面板）
  - 账号池管理
  - 积分查看
  - 成长计划任务
  - 账号切换
"""

import json
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


def port_in_use(host, port):
    """端口上是否已经有服务在听（用来区分「端口被占」和「网关根本没起来」）"""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
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

        # 起不来的最常见原因是端口被上一个没退干净的网关占着；给出可操作提示，
        # 比只丢一句「启动失败」有用得多（真实报错网关子进程已经打在它自己的输出里）。
        why = port_in_use("127.0.0.1", GATEWAY_PORT)
        if why:
            print(f"[错误] 端口 {GATEWAY_PORT} 已被占用：多半是上一次的网关还在后台跑。")
            print(f"       先结束它再启动：Windows 任务管理器里结束 python.exe，")
            print(f"       或在命令行执行  netstat -ano | findstr :{GATEWAY_PORT}  拿到 PID 后")
            print(f"       taskkill /PID <PID> /F")
            print(f"       面板可能已经可用，先试试 http://127.0.0.1:{GATEWAY_PORT}/panel/")
        else:
            print(f"[错误] 网关启动失败（端口 {GATEWAY_PORT} 没起来），上面网关自己打的报错是线索。")
        print(f"       单独跑一次网关可以直接看到完整报错："
              f"\"{sys.executable}\" wb_gateway.py")
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


def panel_url() -> str:
    """统一面板地址（与 wb_webui.py 的 302 目标一致）；config.json 有 api_key 时带上 #key= 片段，免手输密钥。"""
    url = f"http://127.0.0.1:{GATEWAY_PORT}/panel/"
    try:
        with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
            key = (json.load(f).get("api_key") or "").strip()
        if key:
            from urllib.parse import quote
            return url + "#key=" + quote(key, safe="")
    except Exception:
        pass
    return url


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
║  • 统一面板    : http://127.0.0.1:7863/panel/                  ║
║  • API 网关    : http://127.0.0.1:7863                        ║
║  • 老控制台    : http://localhost:8789  (打开即跳到统一面板)   ║
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
    
    # 启动老控制台（数据接口 + /legacy 兜底），浏览器直接打开统一面板
    console_url = panel_url()
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