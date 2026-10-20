#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WorkBuddy 统一管理器 · 自检脚本

为什么需要它：
之前出现过一类隐蔽故障 —— 页面 HTML 是用普通三引号字符串写在 Python 里的，
JS 里的 '\\n' 被 Python 先转义成了真正的换行，于是 JS 字符串跨行未闭合，
整个 <script> 报 "Invalid or unexpected token"，页面上所有按钮全部点不动，
但 Python 侧 py_compile 完全正常、服务也照常启动，肉眼根本看不出来。

本脚本把这类问题变成一条命令就能查出来：
  1) py_compile 全部模块
  2) 抽出 WebUI 与网关面板里的 JS，外加统一面板的 static/app.js、static/sw.js，
     用 node --check 做真正的语法校验
  3) 检查页面里不该出现的残留（未替换的占位符、被误转义的反斜杠），
     以及 static/ 交付文件是否齐全、manifest.webmanifest 是否合法 JSON、
     static/ 里有没有混进开发期临时验证页（它会随 /panel/static/* 对外暴露）
  4) 起临时服务，冒烟所有 HTTP 接口

用法：
    python selfcheck.py            # 只做静态检查（快，不需要联网）
    python selfcheck.py --online   # 额外冒烟：自己起服务并逐个访问接口
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PY = sys.executable

# Windows 控制台默认是 GBK，代码页 936 编不出 '✓' 这类字符 —— 不打这一行，
# 自检在「全部通过」的最后一句 print 上抛 UnicodeEncodeError、退出码 1，
# 看起来像检查失败（实际是通过的）。stdout 被重定向时 reconfigure 不存在，故 try 包住。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

# 语法检查覆盖全部交付模块，而不是手写几个名字 —— 以前只列了 5 个，另外 13 个
# （含 wb_models.py / wb_checkin.py / wb_credits_fetcher.py 等）改坏了自检也看不出来。
# 用 glob 动态取，以后新增的 wb_*.py 自动纳入；下划线前缀的临时脚本不算。
MODULES = sorted(
    [p.name for p in BASE_DIR.glob("wb_*.py")]
    + ["main.py", "config.py", "browser_check.py", "selfcheck.py", "test_webui.py"]
)

PASS, FAIL, WARN = "  [OK]  ", "  [!!]  ", "  [??]  "

problems = []
warnings = []


def header(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def check_py_compile():
    header("1/4  Python 语法检查 (py_compile)")
    for name in MODULES:
        path = BASE_DIR / name
        if not path.exists():
            print(WARN + f"{name} 不存在，跳过")
            warnings.append(f"{name} 不存在")
            continue
        r = subprocess.run([PY, "-m", "py_compile", str(path)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(PASS + name)
        else:
            print(FAIL + name)
            print((r.stderr or r.stdout).strip()[:800])
            problems.append(f"py_compile 失败: {name}")


def extract_scripts(html):
    return re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)


def get_webui_html():
    sys.path.insert(0, str(BASE_DIR))
    from wb_webui import WebUIHandler
    return WebUIHandler.get_index_html(None)


def get_panel_html():
    sys.path.insert(0, str(BASE_DIR))
    import wb_gateway
    return wb_gateway.PANEL_HTML


def check_pages():
    header("2/4  页面与静态资源 JS 语法检查 (node --check)")
    node = shutil_which("node")
    if not node:
        print(WARN + "没找到 node，跳过 JS 语法校验（这是最重要的一项，建议装上 Node.js）")
        warnings.append("缺少 node，未能校验页面 JS 语法")
        return

    pages = []
    try:
        pages.append(("WebUI 主页", get_webui_html()))
    except Exception as e:
        print(FAIL + f"无法生成 WebUI 主页: {e}")
        problems.append(f"WebUI 主页生成失败: {e}")
    try:
        pages.append(("网关面板", get_panel_html()))
    except Exception as e:
        print(FAIL + f"无法读取网关面板: {e}")
        problems.append(f"网关面板读取失败: {e}")

    tmpdir = BASE_DIR / "wb_data"
    tmpdir.mkdir(parents=True, exist_ok=True)

    for label, html in pages:
        scripts = extract_scripts(html)
        if not scripts:
            print(WARN + f"{label}: 没找到 <script>")
            warnings.append(f"{label} 没有 script")
            continue
        js_file = tmpdir / ("_selfcheck_" + re.sub(r"\W+", "_", label) + ".js")
        js_file.write_text("\n// ---- block ----\n".join(scripts), encoding="utf-8")
        r = subprocess.run([node, "--check", str(js_file)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            print(PASS + f"{label}（{len(scripts)} 个 script，{len(html)} 字符）")
        else:
            print(FAIL + f"{label} JS 语法错误：")
            msg = (r.stderr or r.stdout).strip()
            for line in msg.splitlines()[:12]:
                print("        " + line)
            problems.append(f"{label} JS 语法错误（页面按钮会全部失效）")

    # 统一面板的界面已经搬进 static/*.js（不再内联在 HTML 里），必须单独校验，
    # 否则 app.js 里一个笔误上面这段照样全 PASS，而浏览器打开是一片空白。
    static_js = [
        ("统一面板 app.js", BASE_DIR / "static" / "app.js"),
        ("统一面板 sw.js", BASE_DIR / "static" / "sw.js"),
    ]
    for label, path in static_js:
        if not path.exists():
            print(FAIL + f"{label}: 文件不存在 {path}")
            problems.append(f"{label} 不存在（统一面板会白屏）")
            continue
        r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True)
        if r.returncode == 0:
            print(PASS + f"{label}（{len(path.read_text(encoding='utf-8', errors='replace'))} 字符）")
        else:
            print(FAIL + f"{label} JS 语法错误：")
            for line in (r.stderr or r.stdout).strip().splitlines()[:12]:
                print("        " + line)
            problems.append(f"{label} JS 语法错误（统一面板按钮会全部失效）")


def check_leftovers():
    header("3/4  源码检查（原始字符串 / 密钥占位符）")

    # 关键不变式：承载 HTML 的三引号字符串必须是原始字符串 r"""..."""。
    # 若是普通字符串，JS 里的 '\n' 会被 Python 提前变成真换行，
    # 导致 JS 字符串跨行未闭合 —— 整个 <script> 语法错误、所有按钮失效，
    # 而 py_compile 完全看不出来。同理，密钥占位符必须被替换掉。
    raw_checks = [
        ("wb_gateway.py", r'PANEL_HTML\s*=\s*r"""', "网关面板 PANEL_HTML 是原始字符串"),
        ("wb_webui.py", r'return\s+r"""', "WebUI 主页 HTML 是原始字符串"),
    ]
    for filename, pattern, desc in raw_checks:
        path = BASE_DIR / filename
        if not path.exists():
            print(WARN + f"{filename} 不存在，跳过")
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        if re.search(pattern, src):
            print(PASS + f"{filename}: {desc}")
        else:
            print(FAIL + f"{filename}: {desc} —— 没找到 r\"\"\"，"
                         f"普通字符串会吃掉 JS 的 '\n' 转义，页面按钮会全部失效")
            problems.append(f"{filename} 的 HTML 未使用原始字符串 r\"\"\"")

    # 密钥占位符必须在返回给浏览器前被替换
    for label, getter in (("WebUI 主页", get_webui_html), ("网关面板", get_panel_html)):
        try:
            html = getter()
        except Exception as e:
            print(WARN + f"{label}: 读取失败 {e}")
            continue
        if "__API_KEY__" in html:
            print(FAIL + f"{label}: 密钥占位符 __API_KEY__ 没有被替换")
            problems.append(f"{label} 密钥占位符未替换")
        else:
            print(PASS + f"{label}: 密钥占位符已替换")

    # 统一面板的静态资源由网关直接对外托管（/panel/static/<file>），逐项确认
    static_dir = BASE_DIR / "static"
    required = ["index.html", "app.css", "app.js", "manifest.webmanifest", "sw.js",
                "icon-192.png", "icon-512.png", "icon-maskable-512.png"]
    missing = [n for n in required if not (static_dir / n).is_file()]
    if missing:
        print(FAIL + f"static/ 缺少交付文件: {', '.join(missing)}")
        problems.append(f"static/ 缺少 {', '.join(missing)}")
    else:
        print(PASS + f"static/ 交付文件齐全（{len(required)} 个，共 "
                     f"{sum((static_dir / n).stat().st_size for n in required)} 字节）")

    try:
        json.loads((static_dir / "manifest.webmanifest").read_text(encoding="utf-8"))
        print(PASS + "manifest.webmanifest 是合法 JSON")
    except Exception as e:
        print(FAIL + f"manifest.webmanifest 解析失败: {e}")
        problems.append(f"manifest.webmanifest 非法: {e}")

    # static/ 会被原样对外托管：开发期验证页（_preview_/_report_/_probe）不能留在里面
    strays = sorted(p.name for p in static_dir.glob("_*"))
    if strays:
        print(WARN + f"static/ 里有开发期临时文件（会被 /panel/static/* 对外暴露）: {', '.join(strays)}")
        warnings.append("static/ 内有临时文件: " + ", ".join(strays))
    else:
        print(PASS + "static/ 无开发期临时文件")


def _get(url, timeout=8):
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", "replace")


def check_online():
    header("4/4  在线冒烟（本地接口）")
    api_key = ""
    try:
        sys.path.insert(0, str(BASE_DIR))
        import wb_webui
        api_key = getattr(wb_webui, "API_KEY", "")
    except Exception as e:
        print(WARN + f"读不到 API_KEY: {e}")

    def authed(url, timeout=8):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + api_key})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")

    endpoints = [
        ("网关 /healthz", "http://127.0.0.1:7863/healthz", None),
        ("网关 /status", "http://127.0.0.1:7863/status", authed),
        ("网关 /api/auth-files", "http://127.0.0.1:7863/api/auth-files", authed),
        ("网关 /tasks/list", "http://127.0.0.1:7863/tasks/list", authed),
        ("网关面板 /panel/", "http://127.0.0.1:7863/panel/", None),
        ("网关面板 app.css", "http://127.0.0.1:7863/panel/static/app.css", None),
        ("网关面板 app.js", "http://127.0.0.1:7863/panel/static/app.js", None),
        ("网关面板 manifest", "http://127.0.0.1:7863/panel/static/manifest.webmanifest", None),
        ("网关面板 icon-192", "http://127.0.0.1:7863/panel/static/icon-192.png", None),
        ("网关面板 sw.js", "http://127.0.0.1:7863/panel/sw.js", None),
        ("网关面板 /panel/api/logs", "http://127.0.0.1:7863/panel/api/logs?n=5", authed),
        ("网关面板 /panel/api/diagnose", "http://127.0.0.1:7863/panel/api/diagnose", authed),
        ("控制台 / (302 → 统一面板)", "http://127.0.0.1:8789/", None),
        ("控制台 /legacy", "http://127.0.0.1:8789/legacy", None),
        ("控制台 /api/status", "http://127.0.0.1:8789/api/status", None),
        ("控制台 /favicon.ico", "http://127.0.0.1:8789/favicon.ico", None),
    ]

    for label, url, fn in endpoints:
        try:
            status, body = (fn or _get)(url)
            extra = ""
            if url.endswith("/panel/api/diagnose"):
                try:
                    data = json.loads(body)
                    checks = data.get("checks") or []
                    extra = f" 通过 {sum(1 for c in checks if c.get('ok'))}/{len(checks)}"
                except Exception:
                    pass
            if url.endswith("/tasks/list"):
                try:
                    data = json.loads(body)
                    extra = f" 任务数={len(data.get('tasks') or [])}"
                except Exception:
                    pass
            print(PASS + f"{label} -> HTTP {status}{extra}")
        except urllib.error.HTTPError as e:
            print(FAIL + f"{label} -> HTTP {e.code}")
            problems.append(f"{label} 返回 HTTP {e.code}")
        except Exception as e:
            print(FAIL + f"{label} -> {type(e).__name__}: {e}")
            problems.append(f"{label} 访问失败: {e}")


def shutil_which(cmd):
    exts = os.environ.get("PATHEXT", ".EXE;.CMD;.BAT").split(os.pathsep)
    for d in os.environ.get("PATH", "").split(os.pathsep):
        cand = Path(d) / cmd
        if cand.is_file():
            return str(cand)
        for ext in exts:
            c2 = Path(d) / (cmd + ext.lower())
            if c2.is_file():
                return str(c2)
            c3 = Path(d) / (cmd + ext)
            if c3.is_file():
                return str(c3)
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true",
                    help="额外冒烟本地 HTTP 接口（需服务已在运行）")
    args = ap.parse_args()

    print("WorkBuddy 统一管理器 · 自检")
    print(f"目录: {BASE_DIR}")
    print(f"Python: {PY}")

    check_py_compile()
    check_pages()
    check_leftovers()
    if args.online:
        check_online()
    else:
        print("\n（加 --online 可额外冒烟本地 HTTP 接口）")

    header("结果")
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print("  ✗ " + p)
    else:
        print("  ✓ 静态检查全部通过")
    if warnings:
        print(f"\n{len(warnings)} 条提醒：")
        for w in warnings:
            print("  - " + w)

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
