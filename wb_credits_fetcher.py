#!/usr/bin/env python3
"""
WorkBuddy 多账号积分自动采集器
================================
功能：
  1. 自动登录各 WorkBuddy 账号，抓取积分/能量/Buddy 等数据
  2. 数据保存为本地 JSON 文件（不经过任何第三方服务器）
  3. 支持多账号批量采集
  4. 采集完成后自动生成 HTML 报告（打开即看，数据已嵌入）

使用方法：
  1. 安装依赖：pip install playwright
  2. 初始化浏览器：playwright install chromium
  3. 编辑 config.py 填入账号信息
  4. 运行：python wb_credits_fetcher.py

安全说明：
  - 所有数据仅存储在你本地电脑
  - 凭证存在同目录的 config.py 中（建议加入 .gitignore）
  - 浏览器在可见模式运行，你可全程观察操作过程

作者：CodeBuddy Agent
日期：2026-10-12
版本：v1.4 (修复报告数据不显示 + 二维码自动弹出)
"""

import json
import os
import sys

# Windows 安全策略补丁（修复 asyncio 子进程创建问题）
if os.name == 'nt':
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from wb_playwright_transport_patch import apply_patch
        apply_patch()
    except ImportError:
        pass
import time
import logging
from datetime import datetime
from pathlib import Path

# 版本号
FETCHER_VERSION = "3.5"

# ===== 配置 =====
DATA_DIR = Path(__file__).parent / "wb_data"
DATA_FILE = DATA_DIR / "credits_history.json"
CONFIG_FILE = Path(__file__).parent / "config.py"

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("wb-fetcher")


def ensure_data_dir():
    """确保数据目录存在"""
    DATA_DIR.mkdir(exist_ok=True)


def attach_file_log():
    """
    把日志同时写入 wb_data/fetch_log.txt（便于事后追溯采集失败原因）
    - 避免重复挂载
    - 文件超过 2MB 时自动轮转为 fetch_log.old.txt
    """
    try:
        ensure_data_dir()
        log_file = DATA_DIR / "fetch_log.txt"
        for h in log.handlers:
            if isinstance(h, logging.FileHandler) and str(getattr(h, "baseFilename", "")).endswith("fetch_log.txt"):
                return
        try:
            if log_file.exists() and log_file.stat().st_size > 2 * 1024 * 1024:
                log_file.replace(DATA_DIR / "fetch_log.old.txt")
        except Exception:
            pass
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        log.addHandler(fh)
    except Exception:
        pass


def load_config():
    """
    从 config.py 加载账号配置
    config.py 格式示例：

    ACCOUNTS = [
        {
            "name": "主号",
            "phone": "138****8888",
            "login_method": "phone",       # "phone" 或 "wechat"
            "phone_number": "13800138000", # 完整手机号（用于短信验证码登录）
            "password": "your_password",   # 密码（如有）
        },
        {
            "name": "工作号",
            "phone": "139****9999",
            "login_method": "wechat",      # 微信扫码登录
        },
    ]
    """
    if not CONFIG_FILE.exists():
        log.warning(f"配置文件不存在: {CONFIG_FILE}")
        log.info("正在创建配置模板...")
        create_config_template()
        sys.exit(1)

    # 安全地加载配置
    namespace = {}
    try:
        src = CONFIG_FILE.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            src = CONFIG_FILE.read_text(encoding="gbk")
        except Exception as e:
            log.error(f"config.py 无法读取（编码异常）: {e}")
            sys.exit(1)

    try:
        exec(compile(src, str(CONFIG_FILE), "exec"), namespace)
        accounts = namespace.get("ACCOUNTS", [])
        if not accounts:
            log.error("config.py 的 ACCOUNTS 是空的 —— 没有可采集的账号")
            try:
                # Windows 控制台可能是 GBK，直接 print 中文会乱码
                sys.stdout.reconfigure(errors="replace")
            except Exception:
                pass
            print("\n[提示] 积分采集依赖 config.py 里的 ACCOUNTS 清单，当前为空。")
            print("       1) 如果你只想管理账号/聊天：不用跑采集器，直接在")
            print("          http://127.0.0.1:7863/panel/ 的「账号管理」页扫码登录即可；")
            print("       2) 如果你要采集积分：打开 config.py，把 ACCOUNTS 里注释掉的")
            print("          条目取消注释并填入你的账号，然后重新运行。")
            sys.exit(1)
        return accounts
    except SyntaxError as e:
        # v3.6.1：直接指出第几行错了，并把出错那几行打出来
        ln = e.lineno or 1
        log.error(f"config.py 第 {ln} 行有语法错误：{e.msg}")
        lines = src.split("\n")
        for i in range(max(0, ln - 3), min(len(lines), ln + 2)):
            log.error(("  >> " if i == ln - 1 else "     ") + f"{i + 1:>4} | {lines[i]}")
        print(f"\n[错误] config.py 第 {ln} 行有语法错误：{e.msg}")
        print("       请用记事本打开 config.py，检查上面标 >> 的那一行。")
        print("       常见原因：多打了空格 / 少了一个逗号 / 引号没配对。")
        sys.exit(1)
    except Exception as e:
        log.error(f"配置文件加载失败: {e}")
        sys.exit(1)


def create_config_template():
    """创建配置文件模板"""
    template = '''# WorkBuddy 账号配置
# 每个账号填写一条记录
# 此文件包含敏感信息，请勿上传到公开仓库！

ACCOUNTS = [
    # 示例 1：手机号登录
    {
        "name": "主号",                    # 账号别名（用于识别）
        "phone": "138****8888",           # 显示用的脱敏手机号
        "login_method": "phone",          # 登录方式: "phone" 或 "wechat"
        "phone_number": "",               # 完整手机号（必填，用于 phone 登录）
        "password": "",                   # 密码（如需要）
    },
    # 示例 2：微信扫码登录（需手动扫码）
    {
        "name": "工作号",
        "phone": "139****9999",
        "login_method": "wechat",         # 微信扫码，运行时会弹出二维码
    },
    # 复制上面的模板添加更多账号...
]
'''
    CONFIG_FILE.write_text(template, encoding="utf-8")
    log.info(f"已创建配置模板: {CONFIG_FILE}")
    log.info("请编辑该文件填入你的账号信息后重新运行")


def load_history():
    """
    加载历史数据（带保护）
    - 文件为空 → 按新文件处理
    - 解析失败 → 先把原文件另存为「损坏备份」，绝不静默覆盖导致数据丢失
    """
    if DATA_FILE.exists():
        try:
            raw = DATA_FILE.read_text(encoding="utf-8")
            if raw.strip():
                return json.loads(raw)
            log.warning("历史数据文件为空，按新文件处理")
        except Exception as e:
            try:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                broken = DATA_DIR / f"credits_history.损坏备份_{stamp}.json"
                broken.write_text(
                    DATA_FILE.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8",
                )
                log.warning(f"历史数据解析失败，原文件已另存为: {broken.name}")
            except Exception:
                pass
            log.warning(f"历史数据加载失败，将创建新数据文件: {e}")
    return {"accounts": {}, "snapshots": []}


def _history_record_count(d):
    """统计历史数据里的总记录条数（用于判断数据是否变少）"""
    try:
        return sum(len(a.get("records") or []) for a in (d.get("accounts") or {}).values())
    except Exception:
        return 0


def save_history(data):
    """
    保存历史数据（带保护）
    - 写入前自动备份上一版为 credits_history.bak.json
    - 关键保护：如果当前文件的数据比备份更少（如被意外清空），
      绝不用它覆盖备份，避免历史数据被"空数据"顶掉
    - 原子写入：先写临时文件再替换，避免中途中断产生半截损坏文件
    """
    ensure_data_dir()

    # 1) 备份上一版（带"防倒退"保护）
    try:
        if DATA_FILE.exists() and DATA_FILE.stat().st_size > 0:
            bak = DATA_DIR / "credits_history.bak.json"
            should_backup = True
            if bak.exists():
                try:
                    cur = json.loads(DATA_FILE.read_text(encoding="utf-8", errors="replace"))
                    old = json.loads(bak.read_text(encoding="utf-8", errors="replace"))
                    if _history_record_count(cur) < _history_record_count(old):
                        # 当前数据比备份更少 → 疑似被清空/重置，保留备份不覆盖
                        should_backup = False
                        log.warning("检测到数据记录数减少，已保留历史备份（不覆盖）")
                except Exception:
                    pass
            if should_backup:
                bak.write_text(
                    DATA_FILE.read_text(encoding="utf-8", errors="replace"),
                    encoding="utf-8",
                )
    except Exception:
        pass

    # 2) 原子写入
    tmp = DATA_FILE.parent / (DATA_FILE.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DATA_FILE)


def fetch_account_data(page, account):
    """
    抓取单个账号的数据
    返回包含积分、能量等信息的字典
    """
    name = account.get("name", "未知")
    log.info(f"正在抓取账号 [{name}] 的数据...")

    result = {
        "name": name,
        "phone": account.get("phone", ""),
        "timestamp": datetime.now().isoformat(),
        "credits": None,
        "energy": None,
        "buddy_count": None,
        "tasks_completed": [],
        "error": None,
    }

    try:
        # 导航到个人中心 / 成长计划页面
        log.info("  -> 导航到成长中心...")
        page.goto(
            "https://www.workbuddy.cn/profile/growth-center",
            wait_until="domcontentloaded",
            timeout=60000
        )
        page.wait_for_timeout(5000)
        try:
            page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass

        # 截图保存（用于调试）
        screenshot_path = DATA_DIR / f"screenshot_{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        page.screenshot(path=str(screenshot_path))
        log.info(f"  -> 已截图: {screenshot_path.name}")

        # 提取页面文本数据
        data_extracted = page.evaluate("""() => {
            const allText = document.body.innerText;
            const numberMatches = allText.match(/\\d+[,.]?\\d*\\s*(Credits?|积分|分|能量|Energy)/gi);
            const results = {};
            if (numberMatches) results.raw_matches = numberMatches;
            results.page_text = allText.substring(0, 3000);
            return results;
        }""")

        if data_extracted.get("raw_matches"):
            log.info(f"  -> 匹配到数值: {data_extracted['raw_matches']}")

        result["raw_data"] = data_extracted

        # 解析积分和能量
        page_text = data_extracted.get("page_text", "")
        import re

        # Credits: 取所有匹配值的最大值作为余额
        all_credits_matches = re.findall(r'(\d[\d,]*)\s*[Cc]redits?', page_text)
        if all_credits_matches:
            all_credit_values = [int(c.replace(",", "")) for c in all_credits_matches]
            result["credits"] = max(all_credit_values)
            result["all_credits_found"] = all_credit_values
            log.info(f"  -> 找到所有 Credits 值: {all_credit_values}，取最大值 = {result['credits']}")
        else:
            m = re.search(r'积分[：:\s]*(\d[\d,]*)', page_text)
            if m:
                result["credits"] = int(m.group(1).replace(",", ""))

        # Energy: 取最大值
        all_energy_matches = re.findall(r'(\d+)\s*能量', page_text)
        if all_energy_matches:
            all_energy_values = [int(e) for e in all_energy_matches]
            result["energy"] = max(all_energy_values)
            result["all_energy_found"] = all_energy_values
            log.info(f"  -> 找到所有能量值: {all_energy_values}，取最大值 = {result['energy']}")
        else:
            m = re.search(r'能量[：:\s]*(\d+)', page_text)
            if m:
                result["energy"] = int(m.group(1))

        log.info(f"  -> 最终解析结果: Credits={result['credits']}, Energy={result['energy']}")

        # v3.8.2：登录态已就绪（无论本次刚扫码还是复用），顺带抓真实身份
        try:
            nick, phone = fetch_identity(page, name)
            result["real_name"] = nick
            result["real_phone"] = phone
        except Exception:
            pass

    except Exception as e:
        result["error"] = str(e)
        log.error(f"  -> 抓取失败: {e}")

    return result


def _safe_name(name):
    """账号名 → 安全的文件名片段"""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (name or "unknown"))


def fetch_identity(page, name):
    """
    v3.8.2：扫码/登录成功后顺带抓「真实昵称 + 手机号」，
    写入 wb_data/identity_账号名.json，供管理器界面展示真实身份
    （替代 config.py 里写死的别名和占位手机号）。失败不影响主流程。
    """
    import re
    nick, phone = None, None
    try:
        text = page.evaluate(
            "() => (document.body && document.body.innerText || '')")
        for line in (text or "").split("\n"):
            line = line.strip()
            if ("个人版" in line or "专业版" in line) and len(line) <= 30:
                cand = line.replace("个人版", "").replace("专业版", "").strip()
                if cand:
                    nick = cand
                    break
    except Exception:
        pass
    try:
        el = page.query_selector('text="账号设置"')
        if el and el.is_visible():
            el.click()
            page.wait_for_timeout(2500)
            text2 = page.evaluate(
                "() => (document.body && document.body.innerText || '')")
            m = re.search(r'(1[3-9]\d{9})|(\d{3}[\*＊]{2,6}\d{4})', text2 or "")
            if m:
                raw = m.group(0)
                phone = (raw[:3] + "****" + raw[7:]) if re.fullmatch(r'1[3-9]\d{9}', raw) else raw
    except Exception:
        pass
    if nick or phone:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / f"identity_{_safe_name(name)}.json").write_text(
                json.dumps({"nickname": nick, "phone": phone,
                            "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M")},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
            log.info(f"  -> 已获取真实身份: 昵称={nick or '-'} 手机号={phone or '-'}")
        except Exception:
            pass
    return nick, phone


def check_logged_in(page):
    """
    检测当前页面是否处于已登录状态（多信号综合判定）
    信号：
      1. 页面出现可见的「登录 / 注册」按钮 → 未登录
      2. 页面文本包含登录后特征词（退出登录 / 账号设置 / 个人版 等）→ 已登录
      3. 存在用户头像等元素 → 已登录
    """
    try:
        # 信号 1：可见的登录按钮 → 未登录（精确匹配，避免误判「退出登录」类文本）
        for sel in ['text="登录 / 注册"', 'text="登录/注册"']:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    return False
            except Exception:
                continue

        # 信号 2：页面文本包含登录后特征词 → 已登录
        # （注意：仅在明确是登录后才会出现的词，避免营销文案误判）
        try:
            body_text = page.evaluate(
                "() => (document.body && document.body.innerText || '').substring(0, 8000)"
            )
            for kw in ['退出登录', '切换账号', '账号设置', '个人版', '我的账号']:
                if kw in body_text:
                    return True
        except Exception:
            pass

        # 信号 3：用户元素（头像等）
        for sel in ['[class*="avatar"]', '[class*="Avatar"]', '[class*="user"]', '[class*="User"]']:
            try:
                el = page.query_selector(sel)
                if el:
                    return True
            except Exception:
                continue

        return False
    except Exception:
        return False


def login_workbuddy(browser, account, headless=False):
    """
    登录 WorkBuddy 账号（支持登录态持久化：扫码一次后免扫）
    支持：手机号登录、微信扫码登录
    headless=True 时（定时任务），若登录态失效则快速跳过（无法扫码）
    返回 (page, context)
    """
    name = account.get("name", "未知")
    method = account.get("login_method", "phone")

    # 登录态存储文件（每个账号一个）
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    state_file = DATA_DIR / f"login_state_{safe_name}.json"

    # 尝试复用已保存的登录态
    if state_file.exists():
        log.info(f"  -> 发现已保存的登录态: {state_file.name}")
        try:
            context = browser.new_context(storage_state=str(state_file))
        except Exception as e:
            log.warning(f"  -> 登录态加载失败，将重新登录: {e}")
            context = browser.new_context()
    else:
        context = browser.new_context()

    page = context.new_page()
    # 非无头模式下，将窗口置顶显示（让用户能立刻看到浏览器弹出来了）
    if not headless:
        try:
            page.bring_to_front()
        except Exception:
            pass

    log.info(f"=== 开始登录账号 [{name}]（方式: {method}）===")

    try:
        page.goto("https://www.workbuddy.cn/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        try:
            page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass

        # ===== 先检测是否已有登录态（免扫码路径） =====
        if state_file.exists():
            if headless:
                # 无头模式（定时任务）：直接信任已保存的登录态，跳过页面检测
                # （页面检测是启发式的，可能误判；抓取阶段会实际验证）
                log.info("  -> [无头模式] 使用已保存的登录态")
                page.wait_for_timeout(1000)
                return page, context

            # 可见模式：直接访问成长中心验证登录态（最可靠的方式）
            log.info("  -> 检测到已保存的登录态，正在验证（访问成长中心）...")
            try:
                page.goto(
                    "https://www.workbuddy.cn/profile/growth-center",
                    wait_until="domcontentloaded", timeout=45000
                )
                page.wait_for_timeout(3000)
                # 最多重试 3 次检测（页面可能渲染较慢）
                is_valid = False
                for attempt in range(3):
                    is_valid = check_logged_in(page)
                    if is_valid:
                        break
                    page.wait_for_timeout(2000)
                url_ok = ("growth" in page.url) or ("profile" in page.url)
                if is_valid and url_ok:
                    log.info("  -> 登录态仍然有效，跳过扫码/登录！")
                    return page, context
                log.info(f"  -> 登录态验证未通过，需要重新登录（当前页面: {page.url}）")
            except Exception as e:
                log.warning(f"  -> 登录态验证出错: {e}")

            # 回到首页，准备重新登录
            try:
                page.goto("https://www.workbuddy.cn/",
                                wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
            except Exception:
                pass

        # 无头模式（定时任务）下无法扫码，直接快速跳过
        if headless:
            log.error("  -> [无头模式] 无有效登录态且无法扫码，跳过该账号")
            log.error("  -> 请手动运行一次「一键采集.bat」重新扫码登录")
            context.close()
            return None, None

        if method == "phone":
            phone = account.get("phone_number", "")
            password = account.get("password", "")

            if not phone:
                log.error(f"  [{name}] 未配置手机号，跳过")
                context.close()
                return None, None

            log.info(f"  -> 使用手机号登录: {phone[:3]}****{phone[-4:]}")

            try:
                login_btn = page.query_selector('text="登录 / 注册"')
                if not login_btn:
                    login_btn = page.query_selector('text="登录/注册"')
                if login_btn:
                    login_btn.click()
                    page.wait_for_timeout(1500)
            except:
                pass

            phone_input = page.query_selector('input[type="tel"], input[placeholder*="手机"], input[name="phone"]')
            if phone_input:
                phone_input.fill(phone)
                page.wait_for_timeout(500)

            if password:
                pwd_input = page.query_selector('input[type="password"]')
                if pwd_input:
                    pwd_input.fill(password)
                    submit_btn = page.query_selector('button[type="submit"], [class*="login"], [class*="submit"]')
                    if submit_btn:
                        submit_btn.click()
                        page.wait_for_timeout(3000)
            else:
                code_btn = page.query_selector('text=获取验证码, text=发送验证码')
                if code_btn:
                    code_btn.click()
                    log.info(f"  -> 已发送验证码到 {phone[:3]}****{phone[-4:]}, 请在浏览器中输入...")
                    log.info("  -> 等待验证码输入（最多 120 秒）...")
                    for i in range(120):
                        page.wait_for_timeout(1000)
                        try:
                            if check_logged_in(page):
                                log.info("  -> 检测到登录成功！")
                                break
                        except Exception:
                            pass
                        if i % 10 == 0 and i > 0:
                            log.info(f"  -> 还在等待验证码... ({120-i}s)")

        elif method == "wechat":
            # 微信扫码登录：先自动打开登录框，让二维码显示出来
            log.info(f"  -> 正在打开登录窗口...")

            # 尝试自动点击页面上的「登录 / 注册」按钮，触发二维码显示
            # （使用精确匹配，避免误点「退出登录」等其他元素）
            clicked = False
            login_selectors = [
                'text="登录 / 注册"',
                'text="登录/注册"',
                'text="登录"',
                'button:has-text("登录 / 注册")',
                '[class*="login-btn"]',
                '[class*="loginBtn"]',
                '[class*="login-modal"] button',
            ]
            for sel in login_selectors:
                if clicked:
                    break
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        clicked = True
                        log.info(f"  -> 已自动点击登录按钮（选择器: {sel}）")
                        page.wait_for_timeout(2000)
                except Exception:
                    continue

            if not clicked:
                log.info("  -> 未找到登录按钮，请手动点击页面右上角的「登录 / 注册」")

            # 尝试点击「微信登录」Tab（切换二维码为微信扫码）
            wechat_selectors = [
                'text=微信登录',
                'text=微信扫码登录',
                '[class*="wechat"]',
                '[class*="wx-"]',
            ]
            for sel in wechat_selectors:
                try:
                    tab = page.query_selector(sel)
                    if tab and tab.is_visible():
                        tab.click()
                        log.info("  -> 已切换到微信扫码方式")
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue

            log.info(f"  -> 二维码应已显示，请用微信扫码登录...")
            log.info("  -> 等待扫码（最多 180 秒）...")

            logged_in = False
            for i in range(180):
                page.wait_for_timeout(1000)

                # 多信号综合检测登录成功
                try:
                    if check_logged_in(page):
                        log.info("  -> 检测到扫码登录成功！")
                        logged_in = True
                        break
                except Exception:
                    pass

                if i % 15 == 0 and i > 0:
                    log.info(f"  -> 还在等待扫码... ({180-i}s)")

            if not logged_in:
                log.warning("  -> 扫码等待超时，但将继续尝试抓取（可能已登录但未检测到）")

        current_url = page.url
        log.info(f"  -> 当前页面: {current_url}")
        page.wait_for_timeout(2000)

        # ===== 登录成功，保存登录态供下次复用 =====
        try:
            context.storage_state(path=str(state_file))
            log.info(f"  -> 登录态已保存: {state_file.name}（下次采集免扫码）")
        except Exception as e:
            log.warning(f"  -> 登录态保存失败（不影响本次采集）: {e}")

        return page, context

    except Exception as e:
        log.error(f"  -> 登录过程出错: {e}")
        try:
            context.close()
        except Exception:
            pass
        return None, None


def generate_html_report(history):
    """
    生成一份带数据的独立 HTML 报告
    数据直接嵌入 HTML，浏览器打开即看，无需手动导入 JSON
    使用字符串拼接（而非 f-string），避免 Python {} 和 JS {} 转义冲突
    """
    data_json = json.dumps(history, ensure_ascii=False, indent=2)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_short = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ---- CSS ----
    css = """
  :root { --bg:#0a0c14; --card:#12151f; --border:#252a3e; --text:#e4e7f0; --text-dim:#6b7194;
    --accent:#7c5cfc; --accent2:#5ce7ff; --green:#2ed573; --red:#ff6b81; --yellow:#ffa502; --radius:14px; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
    background:var(--bg); color:var(--text); min-height:100vh; line-height:1.6; }
  .header { background:linear-gradient(135deg,#0f1220,#1a1833); border-bottom:1px solid var(--border);
    padding:24px 32px; display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:16px; }
  .header h1 { font-size:20px; font-weight:700; }
  .header h1 span { color:var(--accent); }
  .header-time { color:var(--text-dim); font-size:13px; }
  .stats-row { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; padding:24px 32px 0; }
  .stat-card { background:var(--card); border:1px solid var(--border); border-radius:var(--radius);
    padding:18px 22px; position:relative; overflow:hidden; }
  .stat-card::after { content:''; position:absolute; top:0; left:0; right:0; height:3px;
    border-radius:var(--radius) var(--radius) 0 0; }
  .stat-card.credits::after { background:linear-gradient(90deg,var(--accent),var(--accent2)); }
  .stat-card.energy::after { background:linear-gradient(90deg,var(--green),#7bed9f); }
  .stat-card.accounts::after { background:linear-gradient(90deg,var(--yellow),#ffeaa7); }
  .stat-card.records::after { background:linear-gradient(90deg,var(--red),#ffcccc); }
  .stat-label { font-size:12px; color:var(--text-dim); text-transform:uppercase; letter-spacing:1px; margin-bottom:6px; }
  .stat-value { font-size:28px; font-weight:800; letter-spacing:-1px; }
  .stat-sub { font-size:12px; color:var(--text-dim); margin-top:4px; }
  .main { padding:24px 32px 40px; max-width:1400px; margin:0 auto; width:100%; }
  .tabs { display:flex; gap:4px; margin-bottom:20px; border-bottom:1px solid var(--border); padding-bottom:0; }
  .tab { padding:10px 22px; cursor:pointer; font-size:14px; font-weight:600;
    color:var(--text-dim); border:none; background:none; border-bottom:2px solid transparent;
    transition:all .2s; border-radius:10px 10px 0 0; }
  .tab:hover { color:var(--text); background:rgba(255,255,255,.03); }
  .tab.active { color:var(--accent); border-bottom-color:var(--accent); }
  .chart-container { background:var(--card); border:1px solid var(--border); border-radius:var(--radius);
    padding:24px; margin-bottom:24px; }
  .chart-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; }
  .chart-title { font-size:15px; font-weight:700; }
  .chart-actions { display:flex; gap:8px; }
  .btn-xs { padding:5px 12px; border-radius:8px; border:1px solid var(--border);
    background:transparent; color:var(--text-dim); cursor:pointer; font-size:12px; transition:all .2s; }
  .btn-xs:hover { border-color:var(--accent); color:var(--accent); }
  .btn-xs.active { background:var(--accent); border-color:var(--accent); color:#fff; }
  canvas { max-height:350px; }
  .account-section { margin-top:28px; }
  .section-title { font-size:16px; font-weight:700; margin-bottom:16px; display:flex; align-items:center; gap:8px; }
  .account-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); gap:16px; }
  .acc-card { background:var(--card); border:1px solid var(--border); border-radius:var(--radius);
    padding:20px; transition:all .2s; }
  .acc-card:hover { border-color:var(--accent); transform:translateY(-2px); }
  .acc-name { font-size:16px; font-weight:700; margin-bottom:4px; }
  .acc-phone { font-size:12px; color:var(--text-dim); margin-bottom:14px; }
  .acc-metrics { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
  .metric { text-align:center; padding:10px; background:var(--bg); border-radius:10px; }
  .metric-val { font-size:22px; font-weight:800; }
  .metric-label { font-size:11px; color:var(--text-dim); margin-top:2px; }
  .metric.credits .metric-val { color:var(--accent); }
  .metric.energy .metric-val { color:var(--green); }
  .acc-history { margin-top:14px; padding-top:14px; border-top:1px solid var(--border); }
  .history-item { display:flex; justify-content:space-between; align-items:center; padding:6px 0; font-size:12px; }
  .history-time { color:var(--text-dim); }
  .history-change { font-weight:600; }
  .change-up { color:var(--green); } .change-down { color:var(--red); } .change-same { color:var(--text-dim); }
  .footer { text-align:center; padding:20px; color:var(--text-dim); font-size:11px; border-top:1px solid var(--border); }
  @media (max-width:650px) {
    .header { padding:16px 20px; } .stats-row { padding:16px 20px 0; }
    .main { padding:16px 20px 30px; } .account-grid { grid-template-columns:1fr; }
    .stat-value { font-size:22px; }
  }
"""

    # ---- Body HTML ----
    body = """
<div class="header">
  <div>
    <h1><span>&#128202;</span> WorkBuddy 积分报告</h1>
    <div class="header-time" id="lastUpdate">加载中...</div>
  </div>
  <div style="font-size:12px;color:var(--text-dim)">自动生成于 """ + now_str + """</div>
</div>
<div class="stats-row" id="statsRow"></div>
<div class="main">
  <div class="account-section" style="margin-top:0">
    <div class="section-title">&#128100; 各账号当前状态</div>
    <div class="account-grid" id="accountGrid"></div>
  </div>
  <div class="chart-container" style="margin-top:28px">
    <div class="chart-header">
      <span class="chart-title">Credits 积分变化趋势</span>
      <div class="chart-actions">
        <button class="btn-xs active" data-range="all">全部</button>
        <button class="btn-xs" data-range="7">近 7 天</button>
        <button class="btn-xs" data-range="30">近 30 天</button>
      </div>
    </div>
    <canvas id="creditsChart"></canvas>
  </div>
  <div class="chart-container">
    <div class="chart-header"><span class="chart-title">能量值变化趋势</span></div>
    <canvas id="energyChart"></canvas>
  </div>
</div>
<div class="footer">数据仅存储于本地 &middot; 由 wb_credits_fetcher.py 自动生成</div>
"""

    # ---- JavaScript ----
    js = """
<script>
// ===== 嵌入数据（采集脚本自动生成） =====
const EMBEDDED_DATA = """ + data_json + """;

const PALETTE = ['#7c5cfc','#5ce7ff','#2ed573','#ffa502','#ff6b81','#a55eea','#00d2d3','#ff9ff3','#54a0ff','#5f27cd'];
function getColor(i) { return PALETTE[i % PALETTE.length]; }

let creditsChart=null, energyChart=null;

function escHtml(s) { const d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function fmt(iso) {
  if(!iso) return '-';
  const d=new Date(iso);
  return d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}
function fmtS(iso) {
  if(!iso) return '-';
  const d=new Date(iso);
  return d.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
}

function renderStats() {
  const accounts = Object.keys(EMBEDDED_DATA.accounts || {});
  let tc=0, te=0, tr=0, lt=null;
  accounts.forEach(n => {
    const acc = EMBEDDED_DATA.accounts[n];
    const recs = acc.records || [];
    if(recs.length > 0) {
      const last = recs[recs.length-1];
      if(last.credits) tc += last.credits;
      if(last.energy) te += last.energy;
      if(!lt || new Date(last.timestamp) > new Date(lt)) lt = last.timestamp;
    }
    tr += recs.length;
  });
  document.getElementById('statsRow').innerHTML =
    '<div class="stat-card credits"><div class="stat-label">总 Credits</div><div class="stat-value">'+tc.toLocaleString()+'</div><div class="stat-sub">'+accounts.length+' 个账号合计</div></div>'+
    '<div class="stat-card energy"><div class="stat-label">总能量值</div><div class="stat-value">'+te+'</div><div class="stat-sub">'+accounts.length+' 个账号合计</div></div>'+
    '<div class="stat-card accounts"><div class="stat-label">监控账号</div><div class="stat-value">'+accounts.length+'</div><div class="stat-sub">个活跃账号</div></div>'+
    '<div class="stat-card records"><div class="stat-label">历史记录</div><div class="stat-value">'+tr+'</div><div class="stat-sub">条采集记录</div></div>';
  document.getElementById('lastUpdate').textContent = lt ? '最后更新: '+new Date(lt).toLocaleString('zh-CN') : '无数据';
}

function rebuildChart(canvasId, datasets, baseOpts, yLabel, rangeDays) {
  const ctx = document.getElementById(canvasId);
  const existing = canvasId==='creditsChart'?creditsChart:energyChart;
  if(existing) existing.destroy();
  const allTS = new Set();
  Object.keys(EMBEDDED_DATA.accounts||{}).forEach(name => {
    let recs = EMBEDDED_DATA.accounts[name].records||[];
    if(rangeDays!=='all') { const c=new Date(); c.setDate(c.getDate()-parseInt(rangeDays)); recs=recs.filter(r=>new Date(r.timestamp)>=c); }
    recs.forEach(r => allTS.add(fmtS(r.timestamp)));
  });
  const labels = Array.from(allTS).sort();
  const finalDS = datasets.map(ds => {
    const name = ds.label;
    let recs = (EMBEDDED_DATA.accounts[name]?.records||[]);
    if(rangeDays!=='all') { const c=new Date(); c.setDate(c.getDate()-parseInt(rangeDays)); recs=recs.filter(r=>new Date(r.timestamp)>=c); }
    const map={}; recs.forEach((r,i) => { map[fmtS(r.timestamp)] = ds.data[i]; });
    return { ...ds, data: labels.map(l => map[l] ?? null) };
  });
  const opts = JSON.parse(JSON.stringify(baseOpts));
  opts.scales.y.title = { display:true, text:yLabel, color:'#6b7194' };
  const chart = new Chart(ctx, { type:'line', data:{ labels, datasets: finalDS }, options:opts });
  if(canvasId==='creditsChart') creditsChart=chart; else energyChart=chart;
}

function renderCharts(rangeDays) {
  if(!rangeDays) rangeDays='all';
  const accounts = Object.keys(EMBEDDED_DATA.accounts||{});
  if(accounts.length===0) return;
  const cDS=[], eDS=[];
  accounts.forEach((name,idx) => {
    let recs = (EMBEDDED_DATA.accounts[name].records||[]).filter(r=>r.credits!==null||r.energy!==null);
    if(recs.length===0) return;
    if(rangeDays!=='all') { const c=new Date(); c.setDate(c.getDate()-parseInt(rangeDays)); recs=recs.filter(r=>new Date(r.timestamp)>=c); }
    const color = getColor(idx);
    cDS.push({label:name, data:recs.map(r=>r.credits??null), borderColor:color, backgroundColor:color+'15',
      fill:false, tension:0.3, pointRadius:3, pointHoverRadius:6});
    eDS.push({label:name, data:recs.map(r=>r.energy??null), borderColor:color, backgroundColor:color+'15',
      fill:false, tension:0.3, pointRadius:3, pointHoverRadius:6});
  });
  const baseOpts = {
    responsive:true, maintainAspectRatio:true,
    interaction:{ intersect:false, mode:'index' },
    plugins:{ legend:{ labels:{ color:'#6b7194', usePointStyle:true, padding:16, font:{size:12} } },
      tooltip:{ backgroundColor:'#181d2b', titleColor:'#e4e7f0', bodyColor:'#e4e7f0', borderColor:'#252a3e', borderWidth:1, padding:12 } },
    scales:{ x:{ ticks:{ color:'#6b7194', maxRotation:45, font:{size:11} }, grid:{ color:'#252a3e20' } },
      y:{ ticks:{ color:'#6b7194' }, grid:{ color:'#252a3e20' } } }
  };
  rebuildChart('creditsChart', cDS, baseOpts, 'Credits', rangeDays);
  rebuildChart('energyChart', eDS, baseOpts, 'Energy', rangeDays);
}

function renderAccounts() {
  const accounts = Object.entries(EMBEDDED_DATA.accounts||{});
  const container = document.getElementById('accountGrid');
  if(accounts.length===0) { container.innerHTML='<div style="text-align:center;padding:40px;color:var(--text-dim)">暂无数据</div>'; return; }
  let html='';
  accounts.forEach(([name,acc]) => {
    const recs = acc.records||[];
    const last = recs.length>0 ? recs[recs.length-1] : null;
    const prev = recs.length>1 ? recs[recs.length-2] : null;
    const cC = last?.credits??'-'; const pC = prev?.credits??null;
    const cD = pC!=null&&cC!='-'?cC-pC:null;
    const cE = last?.energy??'-'; const pE = prev?.energy??null;
    const eD = pE!=null&&cE!='-'?cE-pE:null;
    let hHtml='';
    [...recs].reverse().slice(0,5).forEach((r,i) => {
      const pR = recs[recs.length-2-i];
      const d = pR?.credits!=null&&r.credits!=null?r.credits-pR.credits:null;
      const dc = d>0?'change-up':d<0?'change-down':'change-same';
      const dt = d!=null?(d>0?'+':'')+d:'-';
      hHtml+='<div class="history-item"><span class="history-time">'+fmt(r.timestamp)+'</span><span class="history-change '+dc+'">'+dt+'</span></div>';
    });
    const cDClass = cD!=null?(cD>0?'change-up':cD<0?'change-down':''):'';
    const cDText = (cD!=null)?('('+(cD>0?'+':'')+cD+')'):'';
    const eDClass = eD!=null?(eD>0?'change-up':eD<0?'change-down':''):'';
    const eDText = (eD!=null)?('('+(eD>0?'+':'')+eD+')'):'';
    html+='<div class="acc-card"><div class="acc-name">'+escHtml(name)+'</div><div class="acc-phone">'+escHtml(acc.phone||'-')+'</div>'+
      '<div class="acc-metrics"><div class="metric credits"><div class="metric-val">'+(typeof cC==='number'?cC.toLocaleString():cC)+'</div>'+
      '<div class="metric-label">Credits '+(cDText!=''?'<span class="'+cDClass+'">'+cDText+'</span>':'')+'</div></div>'+
      '<div class="metric energy"><div class="metric-val">'+(typeof cE==='number'?cE:cE)+'</div>'+
      '<div class="metric-label">能量 '+(eDText!=''?'<span class="'+eDClass+'">'+eDText+'</span>':'')+'</div></div></div>'+
      '<div class="acc-history"><div style="font-size:11px;color:var(--text-dim);margin-bottom:6px">最近变动</div>'+
      (hHtml||'<div style="font-size:12px;color:var(--text-dim)">暂无记录</div>')+'</div></div>';
  });
  container.innerHTML = html;
}

function renderAll() { renderStats(); renderCharts(); renderAccounts(); }

// Range buttons
document.querySelectorAll('[data-range]').forEach(btn => btn.addEventListener('click', () => {
  document.querySelectorAll('[data-range]').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderCharts(btn.dataset.range);
}));

// Init
renderAll();
</script>
</body>
</html>"""

    # ---- 拼装完整 HTML ----
    html_parts = []
    html_parts.append('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="UTF-8">\n')
    html_parts.append('<meta name="viewport" content="width=device-width, initial-scale=1.0">\n')
    html_parts.append('<title>WorkBuddy 积分报告 - ' + now_short + '</title>\n')
    html_parts.append('<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>\n')
    html_parts.append('<style>' + css + '</style>\n</head>\n<body>')
    html_parts.append(body)
    html_parts.append(js)

    html = '\n'.join(html_parts)

    report_path = Path(__file__).parent / "wb_data" / ("report_" + datetime.now().strftime('%Y%m%d_%H%M%S') + ".html")
    ensure_data_dir()
    report_path.write_text(html, encoding="utf-8")
    log.info(f"  -> 已生成 HTML 报告: {report_path.name}")

    # 同时复制一份固定名称的最新报告
    latest_report = Path(__file__).parent / "wb_data" / "latest_report.html"
    latest_report.write_text(html, encoding="utf-8")

    return report_path


def run_all(accounts, auto_mode=False, headless=False):
    """运行全部账号的采集（auto_mode=True 用于定时任务；headless=True 时不显示浏览器窗口）"""
    from playwright.sync_api import sync_playwright

    history = load_history()
    results = []

    log.info(f"共 {len(accounts)} 个账号待采集")
    log.info("=" * 50)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, channel="msedge")

        for idx, account in enumerate(accounts):
            name = account.get("name", f"账号{idx+1}")
            log.info(f"\n[{idx+1}/{len(accounts)}] 处理账号: {name}")

            try:
                page, context = login_workbuddy(browser, account, headless=headless)
                if not page:
                    log.warning(f"  [{name}] 登录失败，跳过")
                    results.append({
                        "name": name,
                        "phone": account.get("phone", ""),
                        "error": "登录失败",
                        "timestamp": datetime.now().isoformat(),
                    })
                    continue

                data = fetch_account_data(page, account)
                results.append(data)

                if name not in history["accounts"]:
                    history["accounts"][name] = {
                        "phone": account.get("phone", ""),
                        "records": []
                    }

                history["accounts"][name]["records"].append({
                    "timestamp": data["timestamp"],
                    "credits": data["credits"],
                    "energy": data["energy"],
                    "buddy_count": data["buddy_count"],
                    "error": data["error"],
                })

                if len(history["accounts"][name]["records"]) > 500:
                    history["accounts"][name]["records"] = \
                        history["accounts"][name]["records"][-500:]

                history["snapshots"].append({
                    "timestamp": data["timestamp"],
                    "account": name,
                    "credits": data["credits"],
                    "energy": data["energy"],
                })

                # 关闭浏览器上下文（用返回的 context 对象，修复原 page.context() 调用 bug）
                try:
                    context.close()
                except Exception as ctx_err:
                    log.warning(f"  -> 关闭浏览器上下文时出错（可忽略）: {ctx_err}")

            except Exception as e:
                log.error(f"  [{name}] 处理异常: {e}")
                results.append({
                    "name": name,
                    "phone": account.get("phone", ""),
                    "error": str(e),
                    "timestamp": datetime.now().isoformat(),
                })

        browser.close()

    save_history(history)

    report_path = generate_html_report(history)

    log.info("\n" + "=" * 50)
    log.info("采集完成！汇总如下：")
    log.info("-" * 50)
    for r in results:
        status = "OK" if not r.get("error") else f"FAIL {r.get('error', '未知错误')}"
        creds = r.get("credits") or "?"
        energy = r.get("energy") or "?"
        log.info(f"  {r['name']}: Credits={creds}, Energy={energy} {status}")
    log.info("-" * 50)
    log.info(f"数据已保存至: {DATA_FILE}")
    log.info(f"HTML 报告: {Path(__file__).parent / 'wb_data' / 'latest_report.html'} （双击打开即可查看）")
    log.info(f"截图保存在: {DATA_DIR}/")

    return results, history


def main():
    """主函数"""
    # 日志同时落盘到 wb_data/fetch_log.txt，便于事后追溯采集问题
    attach_file_log()
    log.info("-" * 50)
    log.info("采集器启动（本次运行开始）")

    import argparse

    parser = argparse.ArgumentParser(description="WorkBuddy 多账号积分自动采集器")
    parser.add_argument("-a", "--auto", action="store_true",
                        help="免确认模式（定时任务用）：跳过交互确认，直接开始采集")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="同 --auto（兼容写法）")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式：不显示浏览器窗口（定时任务配 --auto 使用）")
    parser.add_argument("--account", default="", metavar="NAME",
                        help="单账号模式：只采集 config.py 中 name 等于 NAME 的那个账号（账号切换用）")
    parser.add_argument("--list-accounts", action="store_true",
                        help="只列出 config.py 中配置的账号名，然后退出（供外部工具调用）")
    args = parser.parse_args()
    auto_mode = args.auto or args.yes
    headless = args.headless

    # ===== 只列出账号（供控制台等外部工具读取，不启动浏览器）=====
    if args.list_accounts:
        for acc in load_config():
            print(acc.get("name", ""))
        return

    print(f"""
╔═══════════════════════════════════════════════════════════╗
║     WorkBuddy 多账号积分自动采集器 v{FETCHER_VERSION}                    ║
║                                                           ║
║   v3.5 新增：                                            ║
║   • 采集日志自动落盘（wb_data/fetch_log.txt）              ║
║   • 数据保护：自动备份 + 损坏保全 + 原子写入                ║
║   v3.3 优化：                                            ║
║   • 登录态检测更可靠（多信号判定 + 重试）                  ║
║   • 验证登录态直接访问成长中心页面，响应更快                ║
║                                                           ║
║   数据仅存储于本地，不经过任何第三方服务器                  ║
║   采集失败时请查看: wb_data/fetch_log.txt                  ║
╚═══════════════════════════════════════════════════════════╝
    """)

    accounts = load_config()

    # ===== 单账号模式：只采集 --account 指定的那一个账号（账号切换用）=====
    if args.account:
        target = args.account.strip()
        matched = [a for a in accounts if (a.get("name") or "") == target]
        if not matched:
            names = "、".join([str(a.get("name", "?")) for a in accounts]) or "（空）"
            log.error(f"--account 指定的账号不存在: {target}")
            print(f"\n[错误] 未找到账号「{target}」")
            print(f"        config.py 中可用账号: {names}")
            sys.exit(2)
        accounts = matched
        print(f"\n[单账号模式] 本次只采集: {target}")

    print(f"\n将采集 {len(accounts)} 个账号的数据：")
    for i, acc in enumerate(accounts):
        method = acc.get("login_method", "phone")
        method_label = "手机号登录" if method == "phone" else "微信扫码"
        print(f"  {i+1}. {acc.get('name', '?')} ({acc.get('phone', '?')}) - {method_label}")

    if not auto_mode:
        confirm = input("\n确认开始？(y/n): ").strip().lower()
        if confirm != "y":
            print("已取消。")
            return
    else:
        print("\n[自动模式] 跳过确认，直接开始采集...")

    run_all(accounts, auto_mode=auto_mode, headless=headless)


if __name__ == "__main__":
    main()

