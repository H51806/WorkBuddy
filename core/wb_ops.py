#!/usr/bin/env python3
"""
WorkBuddy 统一管理器 - 运维小工具（采集进程 / 自检）
=====================================================
从 wb_webui.py 抽出来的公共部分，给两套 HTTP 服务共用：
  - 老控制台 wb_webui.py（:8789，现在只做跳转 + 旧 /api/* 兼容）
  - 统一面板 wb_gateway.py（:7863/panel/，唯一界面）

内容：
  - 采集进程：start_collect / stop_collect / get_collect_status / get_collect_log
  - 自检    ：diagnose_python / diagnose_gateway / diagnose_deps / run_checks
"""

import json
import os
import platform
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent  # 仓库根（本文件在 core/ 下）
CORE_DIR = Path(__file__).parent        # 核心模块所在目录（诊断时按它判断文件是否齐全）
DATA_DIR = BASE_DIR / "wb_data"
GATEWAY_PORT = 7863
GATEWAY_URL = "http://127.0.0.1:%d" % GATEWAY_PORT


def _load_api_key() -> str:
    try:
        with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
            return json.load(f).get("api_key", "") or ""
    except Exception:
        return ""


API_KEY = _load_api_key()

# ===== 采集进程状态 =====
collect_proc = None
collect_log = []
collect_lock = threading.Lock()
COLLECT_LOG_MAX = 2000


def _append_log(line: str) -> None:
    collect_log.append(line)
    if len(collect_log) > COLLECT_LOG_MAX:
        del collect_log[: len(collect_log) - COLLECT_LOG_MAX]


def get_collect_log(n: int = 100):
    """取最近 n 行采集日志（给 /api/logs 与面板的实时日志框用）。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 100
    if n <= 0:
        n = 100
    return collect_log[-n:]


def start_collect(action: str = "collect"):
    """启动采集进程"""
    global collect_proc, collect_log

    with collect_lock:
        if collect_proc and collect_proc.poll() is None:
            return {"success": False, "message": "采集已在运行中"}

        log_file = DATA_DIR / "collect_log.txt"
        log_file.parent.mkdir(exist_ok=True)

        cmd = [sys.executable, str(CORE_DIR / "wb_credits_fetcher.py"), "--auto"]

        try:
            collect_proc = subprocess.Popen(
                cmd,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            collect_log = []

            def read_log():
                proc = collect_proc
                stream = getattr(proc, "stdout", None)
                if stream is None:
                    return
                for line in iter(stream.readline, b''):
                    text = line.decode('utf-8', errors='replace').strip()
                    _append_log(text)
                    try:
                        with open(log_file, 'a', encoding='utf-8') as f:
                            f.write(text + '\n')
                    except Exception:
                        pass

            t = threading.Thread(target=read_log, daemon=True)
            t.start()

            return {"success": True, "message": "采集已启动", "pid": collect_proc.pid}
        except Exception as e:
            return {"success": False, "message": "启动失败: %s" % e}


def stop_collect():
    """停止采集"""
    global collect_proc

    if collect_proc and collect_proc.poll() is None:
        try:
            collect_proc.terminate()
            collect_proc.wait(timeout=5)
        except Exception:
            try:
                collect_proc.kill()
            except Exception:
                pass
        return {"success": True, "message": "采集已停止"}
    return {"success": False, "message": "没有正在运行的采集"}


def _collect_hint() -> str:
    """根据采集日志尾部识别常见失败原因，给出可操作提示。"""
    joined = "\n".join(collect_log[-80:])
    if "spawn EPERM" in joined or "BrowserType.launch" in joined:
        return ("浏览器被当前运行环境限制启动（spawn EPERM）。采集依赖 Playwright + Edge，"
                "请在普通桌面环境运行（双击「一键启动.bat」），不要放在受限沙箱/服务里；"
                "或者改用面板的「账号管理」页扫码登录，那条链路不需要浏览器。")
    if "Traceback" in joined:
        return "采集进程异常退出，详情见 wb_data/fetch_log.txt。"
    return ""


def get_collect_status():
    """采集状态（保留老控制台的字段形状：running / pid / exit_code / log / hint）"""
    global collect_proc

    running = bool(collect_proc and collect_proc.poll() is None)
    exit_code = None
    if collect_proc is not None and not running:
        exit_code = collect_proc.returncode

    result = {
        "running": running,
        "pid": collect_proc.pid if collect_proc else None,
        "exit_code": exit_code,
        "log": collect_log[-30:],
    }
    hint = _collect_hint()
    if hint and not running:
        result["hint"] = hint
    return result


def diagnose_python() -> str:
    """诊断 Python 运行时"""
    lines = [
        "Python %s" % platform.python_version(),
        "executable: %s" % sys.executable,
        "platform: %s" % platform.platform(),
        "pid: %s" % os.getpid(),
        "cwd: %s" % os.getcwd(),
    ]
    return " | ".join(lines)


def diagnose_gateway() -> str:
    """诊断网关连通性（自己给自己发一个 /healthz + /status）"""
    detail = ["gateway url %s" % GATEWAY_URL]
    try:
        req = urllib.request.Request(GATEWAY_URL + "/status", headers={"Authorization": "Bearer %s" % API_KEY})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        detail.append("status=%s" % data.get("status"))
        detail.append("accounts=%s" % data.get("total", 0))
        detail.append("auth_files=%s" % len(data.get("auth_files", [])))
        detail.append("version=%s" % data.get("version"))
    except Exception as e:
        detail.append("status query failed: %s" % e)
    return " | ".join(detail)


def diagnose_deps() -> str:
    """诊断可选依赖与关键文件"""
    deps = []
    for mod in ("playwright", "psutil", "requests"):
        try:
            __import__(mod)
            deps.append("%s=OK" % mod)
        except Exception:
            deps.append("%s=MISSING(optional)" % mod)
    for name in ("wb_gateway.py", "wb_tasks.py", "wb_auth.py", "wb_credits_fetcher.py"):
        # 核心模块现在都在 core/ 下（重启后的目录结构），所以按 CORE_DIR 判断
        deps.append("%s=%s" % (name, "OK" if (CORE_DIR / name).exists() else "MISSING"))
    auth_dir = BASE_DIR / "auths"
    n_auth = len(list(auth_dir.glob("*.json"))) if auth_dir.exists() else 0
    deps.append("auths=%s" % n_auth)
    return " | ".join(deps)


def _files_state():
    """面板「诊断」页用的文件/目录清单"""
    auth_dir = BASE_DIR / "auths"
    n_auth = len(list(auth_dir.glob("*.json"))) if auth_dir.exists() else 0
    static_dir = BASE_DIR / "static"
    static_files = sorted(p.name for p in static_dir.glob("*")) if static_dir.exists() else []
    tasks_log = DATA_DIR / "tasks_log.jsonl"
    collect_log_file = DATA_DIR / "collect_log.txt"
    return {
        "auths": n_auth,
        "static_files": static_files,
        "tasks_log": str(tasks_log) if tasks_log.exists() else "",
        "tasks_log_size": tasks_log.stat().st_size if tasks_log.exists() else 0,
        "collect_log_size": collect_log_file.stat().st_size if collect_log_file.exists() else 0,
    }


def run_checks():
    """给面板「诊断」页用的结构化自检结果。"""
    checks = []

    py = diagnose_python()
    checks.append({"name": "python", "label": "Python 运行时", "ok": True, "detail": py, "hint": ""})

    gw_ok = True
    try:
        with urllib.request.urlopen(GATEWAY_URL + "/healthz", timeout=5) as resp:
            gw_ok = resp.status == 200
    except Exception as e:
        gw_ok = False
        checks.append({"name": "gateway", "label": "网关连通性", "ok": False,
                       "detail": "healthz failed: %s" % e,
                       "hint": "网关没在跑，或端口被改过。面板本身由网关送出，能看到这页通常只是这一瞬间的抖动。"})
    if gw_ok:
        checks.append({"name": "gateway", "label": "网关连通性", "ok": True,
                       "detail": diagnose_gateway(), "hint": ""})

    deps_raw = diagnose_deps()
    missing = [x for x in deps_raw.split(" | ") if x.endswith("(optional)")]
    checks.append({
        "name": "deps", "label": "可选依赖与关键文件", "ok": True, "detail": deps_raw,
        "hint": ("缺 " + "、".join(m.split("=")[0] for m in missing)) if missing else "",
    })

    files = _files_state()
    checks.append({
        "name": "files", "label": "账号与数据文件", "ok": files["auths"] > 0,
        "detail": "auths=%d 个 | static=%s | tasks_log=%d B | collect_log=%d B" % (
            files["auths"], ",".join(files["static_files"]) or "缺失",
            files["tasks_log_size"], files["collect_log_size"]),
        "hint": "" if files["auths"] > 0 else "auths/ 里一个账号都没有：先在「账号管理」页扫码登录。",
    })

    return {"checks": checks, "files": files,
            "python": py, "gateway": diagnose_gateway(), "deps": deps_raw}
