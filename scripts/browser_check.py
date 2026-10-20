#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WorkBuddy 统一管理器 · 浏览器端功能自检

selfcheck.py 查的是“服务器有没有把正确的页面发出来”，
本脚本查的是“浏览器真的能点动吗” —— 用真实 Edge 打开控制台，
逐个点击标签和按钮，报告每个模块的实际输出与所有 JS 报错。

它会检查：
  1) 7 个标签是否都能切换（有没有卡在仪表盘）
  2) 任务中心扫描是否返回任务清单
  3) 账号保活是否给出明确结果
  4) 诊断是否输出运行时信息
  5) 实时面板 iframe 是否加载、是否报 401 / .reduce 崩溃
  6) 控制台是否有任何 JS 报错、任何 HTTP 4xx/5xx

注意：界面已合并到统一面板，老控制台根路径 8789/ 现在会 302 跳到
http://127.0.0.1:7863/panel/。所以本脚本默认打 8789/legacy（老界面兜底），
末尾再补一次统一面板的冒烟（11 个视图逐页看 #view 是否渲染出内容）。

用法（需先启动 一键启动.bat，另开一个窗口运行）：
    python browser_check.py
"""

import sys
from pathlib import Path

#: 仓库根：本文件在 scripts/ 下，核心模块在 core/。
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "core"))

try:
    from wb_playwright_transport_patch import apply_patch
    apply_patch()
except Exception as e:  # 补丁只是为了在受限环境下也能跑，失败不影响正常使用
    print("（传输补丁未应用：%s）" % e)

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("缺少 playwright，请先运行：pip install playwright && playwright install msedge")
    sys.exit(2)

URL = "http://localhost:8789/legacy"          # 老界面兜底入口（8789/ 根路径已 302 跳到统一面板）
PANEL_URL = "http://127.0.0.1:7863/panel/"    # 统一面板（本项目唯一界面）
PANEL_PAGES = ["overview", "pool", "accounts", "models", "tokens", "credits",
               "tasks", "collect", "diagnose", "checkin", "settings"]
errors, console, bad = [], [], []


def api_key():
    """读 config.json 的 api_key，用于给面板 URL 拼 #key= 片段（拼不上也能开，只是会 401）。"""
    try:
        import json
        cfg = json.loads((BASE_DIR / "config.json").read_text(encoding="utf-8"))
        return str(cfg.get("api_key") or "")
    except Exception:
        return ""


def main():
    with sync_playwright() as p:
        try:
            b = p.chromium.launch(headless=True, channel="msedge")
        except Exception as e:
            print("无法启动 Edge：%s" % e)
            print("提示：若报 spawn EPERM，说明当前环境禁止启动子进程，请在普通命令行窗口里运行本脚本。")
            return 2

        pg = b.new_page()
        pg.on("console", lambda m: console.append(f"[{m.type}] {m.text}"))
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("requestfailed", lambda r: errors.append(f"REQFAIL {r.url} {r.failure}"))
        pg.on("response", lambda r: bad.append(f"HTTP {r.status} {r.url}") if r.status >= 400 else None)
        pg.on("dialog", lambda d: d.accept())

        resp = pg.goto(URL, wait_until="load", timeout=20000)
        print("控制台 HTTP:", resp.status if resp else None)
        pg.evaluate("window.__alerts=[]; window.alert=function(m){window.__alerts.push(String(m));};")

        print("\n--- 1) 标签切换 ---")
        n = pg.eval_on_selector_all(".tab", "els => els.length")
        print("  标签数:", n)
        for i in range(n):
            label = pg.eval_on_selector_all(".tab", f"els => els[{i}].textContent").strip()
            pg.eval_on_selector_all(".tab", f"els => els[{i}].click()")
            pg.wait_for_timeout(250)
            panel = pg.evaluate("document.querySelector('.panel.active')?.id")
            print(f"  #{i} {label} -> {panel}")

        def click_tab(idx):
            pg.eval_on_selector_all(".tab", f"els => els[{idx}].click()")
            pg.wait_for_timeout(300)

        print("\n--- 2) 任务中心 ---")
        click_tab(3)
        pg.evaluate("taskScan()")
        pg.wait_for_timeout(3500)
        txt = pg.evaluate("document.getElementById('tasks-log')?.innerText") or ""
        print("  ", txt[:200].replace("\n", " | "))

        print("\n--- 3) 账号保活 ---")
        click_tab(4)
        pg.evaluate("keepaliveRun()")
        pg.wait_for_timeout(3000)
        txt = pg.evaluate("document.getElementById('keepalive-log')?.innerText") or ""
        print("  ", txt[:200].replace("\n", " | "))

        print("\n--- 4) 诊断 ---")
        click_tab(6)
        pg.evaluate("runDiagnose()")
        pg.wait_for_timeout(4000)
        txt = pg.evaluate("document.getElementById('diagnose-output')?.innerText") or ""
        print("  ", txt[:200].replace("\n", " | "))

        print("\n--- 5) 仪表盘 / 刷新 Token ---")
        click_tab(0)
        pg.evaluate("refreshTokens()")
        pg.wait_for_timeout(2500)
        print("  弹窗:", pg.evaluate("window.__alerts"))
        txt = pg.evaluate("document.getElementById('pool-info')?.innerText") or ""
        print("  账号池:", txt[:160].replace("\n", " "))

        print("\n--- 6) 实时面板 iframe ---")
        click_tab(5)
        pg.wait_for_timeout(3500)
        for f in pg.frames:
            if "7863/panel" in f.url:
                print("  iframe:", f.url[:60])
                print("  统计:", f.evaluate(
                    "[document.getElementById('stat-total')?.textContent,"
                    " document.getElementById('stat-available')?.textContent,"
                    " document.getElementById('stat-success')?.textContent].join(' / ')"))
                f.evaluate("switchTab('accounts', null)")
                pg.wait_for_timeout(1200)
                acc = (f.evaluate("document.getElementById('accounts-table')?.innerText") or "")
                print("  账号表:", acc[:160].replace("\n", " | "))

        print("\n--- 7) 统一面板 /panel/（唯一界面）---")
        try:
            k = api_key()
            purl = PANEL_URL + ("#key=" + k if k else "")
            presp = pg.goto(purl, wait_until="load", timeout=20000)
            pg.wait_for_timeout(1500)
            print("  面板 HTTP:", presp.status if presp else None)
            info = pg.evaluate(
                "[document.querySelectorAll('#nav .nav-item').length,"
                " document.querySelectorAll('#view *').length,"
                " (document.getElementById('pageTitle') || {}).textContent || '']")
            print("  导航项:", info[0], " 首屏 #view 节点:", info[1], " 标题:", info[2])
            if info[1] < 5:
                print("  ✗ 首屏 #view 没有渲染出内容（JS 报错 / 401 / 后端不可达）")
                errors.append("统一面板首屏 #view 为空")
            for page in PANEL_PAGES:
                pg.evaluate("location.hash = 'p=" + page + "'")
                pg.wait_for_timeout(420)
                cnt = pg.evaluate("document.querySelectorAll('#view *').length")
                title = pg.evaluate("(document.getElementById('pageTitle') || {}).textContent || ''")
                good = cnt > 5
                print("    %-9s 节点=%-6s %-4s %s" % (page, cnt, "✓" if good else "✗", title))
                if not good:
                    errors.append("统一面板 %s 视图渲染为空" % page)
        except Exception as e:
            print("  ✗ 统一面板访问失败:", e)
            errors.append("统一面板访问失败: %s" % e)

        print("\n--- console 报错 ---")
        errs = [c for c in console if c.startswith("[error]") or c.startswith("[warning]")]
        print("  （无）" if not errs else "\n".join("  " + c for c in errs[-20:]))
        print("--- page errors ---")
        print("  （无）" if not errors else "\n".join("  " + e for e in errors[-20:]))
        print("--- HTTP 4xx/5xx ---")
        uniq = sorted(set(bad))
        print("  （无）" if not uniq else "\n".join("  " + x for x in uniq))

        ok = not errs and not errors and not uniq
        print("\n结果:", "✓ 全部模块可正常点击" if ok else "✗ 存在上述问题")
        b.close()
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
