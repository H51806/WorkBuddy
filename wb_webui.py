#!/usr/bin/env python3
"""
WorkBuddy 统一管理器 - WebUI 控制台
====================================
界面已合并到网关面板 http://127.0.0.1:7863/panel/（见 static/）：
本进程保留 /api/* 数据接口（老面板依赖），根路径 / 直接 302 跳到统一面板，
/legacy 仍可打开旧界面兜底。原老控制台独占的能力（采集实时日志、系统诊断、
账号保活）已抽到 wb_ops.py 并由网关面板的「实时日志」「诊断」页复用。
"""

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime

# ===== 配置 =====
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "wb_data"
CONSOLE_PORT = 8789
GATEWAY_PORT = 7863
GATEWAY_URL = f"http://127.0.0.1:{GATEWAY_PORT}"

# 从 config.json 读真实的 api_key；读不到时保留占位符（网关侧无 key 时也不校验）
def _load_api_key() -> str:
    cfg = BASE_DIR / "config.json"
    try:
        with open(cfg, "r", encoding="utf-8") as f:
            return json.load(f).get("api_key", "") or ""
    except Exception:
        return ""

API_KEY = _load_api_key() or "sk-CHANGEME-please-set-your-own-key"

# 状态：采集进程与自检逻辑已抽到 wb_ops.py，老控制台与统一面板共用一份实现
from wb_ops import (  # noqa: E402
    start_collect,
    stop_collect,
    get_collect_status,
    get_collect_log,
    diagnose_python,
    diagnose_gateway,
    diagnose_deps,
)


def get_gateway_status():
    """获取网关状态"""
    try:
        req = urllib.request.Request(f"{GATEWAY_URL}/healthz")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return {"status": "running", "port": GATEWAY_PORT}
    except:
        return {"status": "stopped", "port": GATEWAY_PORT}


def get_accounts():
    """获取账号列表：以真实 auths/*.json 为唯一事实来源。

    只要网关可达，就返回它报告的真实列表（可能为空）；空列表不再回落到
    config.py 的静态账号，否则界面会显示一个没有 token 的“幻影账号”，
    看起来 active 实际不可用。仅当网关完全不可达时，才用 config.py 的
    账号元数据兜底，且状态标记为 unbound（未登录），避免误导。
    """
    try:
        req = urllib.request.Request(
            f"{GATEWAY_URL}/api/auth-files",
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        accounts = []
        for a in data.get("accounts", []):
            accounts.append({
                "uid": a.get("uid") or a.get("file") or "",
                "name": a.get("name") or a.get("file") or "未知",
                # 手机号/昵称由网关从 auths/*.json + JWT payload 里解析，这里必须透传，
                # 否则界面恒显示 "-"（旧版把 phone 硬编码成空串就是这个 bug）。
                "phone": a.get("phone") or "",
                "login_method": a.get("login_method") or "oauth",
                "status": a.get("status") or ("active" if a.get("has_token") else "invalid"),
                "credits": None,
                "energy": None,
                # 不再展示 accessToken 的前 12 个字符（JWT 头部，看起来就是乱码）。
                # 只给一个可读的凭据类型，真正的有效期单独用 expires_at_human 展示。
                "token": a.get("token_kind") or "",
                "expires_at": a.get("expires_at"),
                "expires_at_human": a.get("expires_at_human") or "",
                "has_refresh_token": a.get("has_refresh_token"),
                "file": a.get("file") or "",
            })
        return accounts
    except Exception:
        pass

    # 网关不可达：仅作只读兜底，明确标为未登录
    accounts = []
    try:
        import config
        for acc in config.ACCOUNTS:
            accounts.append({
                "uid": acc.get("uid", ""),
                "name": acc.get("name", "未知"),
                "phone": acc.get("phone", ""),
                "login_method": acc.get("login_method", "wechat"),
                "status": "unbound",
                "credits": None,
                "energy": None,
                "token": "",
                "expires_at": None,
                "has_refresh_token": False,
            })
    except Exception as e:
        accounts.append({"error": str(e)})
    return accounts


def get_credits_data():
    """获取积分数据（来自 wb_data/credits_history.json）"""
    data_file = DATA_DIR / "credits_history.json"
    if data_file.exists():
        try:
            with open(data_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {"accounts": {}, "snapshots": []}


def _latest_record_per_account(credits_history):
    """把采集历史的每个账号（按 config.py 的 name 索引）压成 {name: {credits, energy, ts}}。"""
    out = {}
    accounts = credits_history.get("accounts", {}) if isinstance(credits_history, dict) else {}
    for name, info in accounts.items():
        recs = (info or {}).get("records") or []
        # 只保留有积分或能量的记录，取最新一条
        valid = [r for r in recs if (r.get("credits") is not None or r.get("energy") is not None)]
        if not valid:
            continue
        last = max(valid, key=lambda r: r.get("timestamp") or "")
        out[name] = {
            "credits": last.get("credits"),
            "energy": last.get("energy"),
            "timestamp": last.get("timestamp"),
        }
    return out


def _identity_phone_by_name():
    """从 wb_data/identity_*.json 拿 {config_name: masked_phone}。
    这是把 config.py 里的账号名映射到真实手机号的唯一桥。
    """
    out = {}
    if not DATA_DIR.exists():
        return out
    for f in DATA_DIR.glob("identity_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            name = f.name[len("identity_"):-len(".json")]
            if name and data.get("phone"):
                out[name] = data["phone"]
        except Exception:
            continue
    return out


def _auths_phone_by_uid():
    """从 auths/*.json 拿 {uid: masked_phone}。
    优先用文件里的 account.phone，没有就用 JWT claims 里的 preferred_username 脱敏。
    """
    out = {}
    if not (BASE_DIR / "auths").exists():
        return out
    import re
    def _mask(p):
        p = str(p or "")
        return p[:3] + "****" + p[-4:] if len(p) >= 7 else p
    def _jwt_phone(token):
        try:
            import base64
            parts = (token or "").split(".")
            if len(parts) < 2: return ""
            payload = parts[1]
            payload += "=" * (-len(payload) % 4)
            return str(json.loads(base64.urlsafe_b64decode(payload).decode("utf-8", "replace")).get("preferred_username") or "")
        except Exception:
            return ""
    for f in (BASE_DIR / "auths").glob("*.json"):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        auth = raw.get("auth") or {}
        acc = raw.get("account") or {}
        uid = str(acc.get("uid") or raw.get("uid") or "")
        if not uid:
            continue
        phone = acc.get("phone") or raw.get("phone") or _jwt_phone(auth.get("accessToken"))
        if phone:
            out[uid] = _mask(phone)
    return out


def enrich_accounts_with_credits(accounts):
    """给 /api/status 里的 accounts 注入 credits/energy。

    数据链：
      auths/*.json   → {uid: 真实手机号}            （从 JWT claims 解 preferred_username）
      identity_*.json → {config_name: 脱敏手机号}    （采集器抓的）
      credits_history.json → {config_name: {credits, energy}}  （采集器写的）

    通过「脱敏手机号」把 uid ↔ config_name 对上，再取最新积分回填。
    网关内存里的 credits/energy 永远是 0（auths/*.json 没有这个字段），
    所以这里就是仪表盘「总积分」的唯一真实来源。
    """
    latest = _latest_record_per_account(get_credits_data())
    identity_phone = _identity_phone_by_name()
    auths_phone = _auths_phone_by_uid()

    # config.py 里的 ACCOUNTS（name, phone 显示脱敏值）—— 作为最后兜底匹配
    config_names = set()
    try:
        import config as _cfg
        for a in getattr(_cfg, "ACCOUNTS", []) or []:
            config_names.add(str(a.get("name") or ""))
    except Exception:
        pass

    # 反向索引：脱敏手机号 → config_name（identity 文件里存的）
    phone_to_name = {phone: name for name, phone in identity_phone.items()}

    for a in accounts:
        uid = str(a.get("uid") or "")
        a_name = a.get("name") or ""
        match_name = None
        if uid in auths_phone and auths_phone[uid] in phone_to_name:
            match_name = phone_to_name[auths_phone[uid]]
        elif a_name in config_names and a_name in latest:
            match_name = a_name
        # 兜底：磁盘上只有一个账号、credits_history 也只有一个记录时，
        # 直接按"唯一的真实账号 = 唯一的 credits 记录"对应。覆盖
        # auths/*.json nickname 与 config.ACCOUNTS.name 不一致的情况。
        if match_name is None and len(accounts) == 1 and len(latest) == 1:
            match_name = next(iter(latest.keys()))
        if match_name and match_name in latest:
            a["credits"] = latest[match_name].get("credits")
            a["energy"] = latest[match_name].get("energy")
            a["credits_updated_at"] = latest[match_name].get("timestamp")
        else:
            a.setdefault("credits", a.get("credits"))
            a.setdefault("energy", a.get("energy"))
    return accounts


def build_report_html():
    """生成采集/任务报告页面"""
    credits = get_credits_data()
    snapshots = credits.get("snapshots", []) if isinstance(credits, dict) else []
    accounts = credits.get("accounts", {}) if isinstance(credits, dict) else {}

    rows = "".join(
        f"<tr><td>{uid}</td><td>{info.get('name','-') if isinstance(info, dict) else '-'}</td>"
        f"<td>{info.get('credits','-') if isinstance(info, dict) else '-'}</td>"
        f"<td>{info.get('energy','-') if isinstance(info, dict) else '-'}</td></tr>"
        for uid, info in accounts.items()
    ) or '<tr><td colspan="4">暂无采集数据，请先运行「开始采集」</td></tr>'

    task_lines = ""
    log_file = DATA_DIR / "tasks_log.jsonl"
    if log_file.exists():
        try:
            with open(log_file, "r", encoding="utf-8") as f:
                tail = f.readlines()[-50:]
            task_lines = "".join(f"<div class='line'>{line.strip()}</div>" for line in tail)
        except Exception as e:
            task_lines = f"<div class='line'>读取失败: {e}</div>"
    if not task_lines:
        task_lines = "<div class='line'>暂无任务日志</div>"

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>WorkBuddy 报告</title>
<style>
body{{background:#0f172a;color:#e2e8f0;font-family:system-ui,'Microsoft YaHei',sans-serif;padding:24px;}}
h1{{font-size:22px}} h2{{font-size:16px;margin-top:28px;color:#93c5fd}}
table{{width:100%;border-collapse:collapse;margin-top:10px}}
th,td{{border:1px solid #334155;padding:8px 10px;text-align:left;font-size:13px}}
th{{background:#1e293b}} .line{{font-family:Consolas,monospace;font-size:12px;color:#94a3b8;padding:2px 0}}
.meta{{color:#8b9dc3;font-size:12px}}
</style></head><body>
<h1>WorkBuddy 统一管理器 · 报告</h1>
<div class="meta">生成时间：{datetime.now().isoformat()} ｜ 快照数：{len(snapshots)}</div>
<h2>账号积分 / 能量</h2>
<table><tr><th>UID</th><th>名称</th><th>积分</th><th>能量</th></tr>{rows}</table>
<h2>最近任务日志（末 50 条）</h2>{task_lines}
</body></html>"""


# ===== WebUI Handler =====
class WebUIHandler(BaseHTTPRequestHandler):
    """WebUI HTTP 处理器"""
    
    def log_message(self, format, *args):
        pass  # 静默日志
    
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def _send_html(self, html, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(html.encode('utf-8'))

    def _send_text(self, text, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write(text.encode('utf-8'))
    
    def panel_url(self) -> str:
        """统一面板地址；config.json 里有真实 api_key 时带上 #key= 片段，省得手输密钥。"""
        base = f"http://127.0.0.1:{GATEWAY_PORT}/panel/"
        key = _load_api_key()
        if not key:
            return base
        try:
            from urllib.parse import quote
            return base + "#key=" + quote(key, safe="")
        except Exception:
            return base

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            # 统一入口：老控制台不再自带界面，直接跳到唯一面板（静态文件 + 全部 API 都在网关上）
            self.send_response(302)
            self.send_header('Location', self.panel_url())
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
        elif self.path == '/legacy':
            # 兜底：面板打不开时还能用老界面看数据
            self._send_html(self.get_index_html())
        elif self.path == '/favicon.ico':
            # 桌面浏览器会自动请求 favicon；不处理会在控制台留下 404 噪音
            self.send_response(204)
            self.send_header('Cache-Control', 'public, max-age=86400')
            self.end_headers()
            return
        elif self.path == '/api/status':
            accs = get_accounts()
            # 从 wb_data/credits_history.json 回填 credits/energy——auths/*.json 里
            # 没有积分字段，网关内存的 credits 恒为 0，这里是仪表盘的真正数据源。
            accs = enrich_accounts_with_credits(accs)
            payload = {
                "gateway": get_gateway_status(),
                "console": {"port": CONSOLE_PORT, "status": "running"},
                "accounts": accs,
                "collect": get_collect_status(),
                "time": datetime.now().isoformat(),
            }
            # 用实时余额覆盖 accounts 里的 credits/energy，让 /api/status 单次就能拿到最新值
            try:
                req = urllib.request.Request(
                    f"{GATEWAY_URL}/api/credits/live",
                    headers={"Authorization": f"Bearer {API_KEY}"},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    live = json.loads(resp.read().decode("utf-8", "replace"))
                payload["live_credits"] = {
                    "total_remain": live.get("total_remain"),
                    "total_size": live.get("total_size"),
                    "expiring_7d": live.get("expiring_7d"),
                    "queried_at": live.get("queried_at"),
                }
                live_accounts = live.get("accounts") or {}
                for a in payload["accounts"]:
                    lv = live_accounts.get(a.get("uid"))
                    if lv and (lv.get("remain") or 0) > 0:
                        a["credits"] = lv.get("remain")
                        a["energy"] = lv.get("total")
            except Exception:
                payload["live_credits"] = None
            self._send_json(payload)
        elif self.path.startswith('/api/credits/live'):
            # 实时余额：走网关（30s 缓存）
            self._proxy_gateway()
        elif self.path == '/api/credits':
            self._send_json(get_credits_data())
        elif self.path == '/api/logs':
            self._send_json({"log": get_collect_log(100)})
        elif self.path == '/api/collect/status':
            self._send_json(get_collect_status())
        elif self.path == '/api/report':
            self._send_html(build_report_html())
        elif self.path == '/api/diagnose/python':
            self._send_text(diagnose_python())
        elif self.path == '/api/diagnose/gateway':
            self._send_text(diagnose_gateway())
        elif self.path == '/api/diagnose/deps':
            self._send_text(diagnose_deps())
        elif self.path.startswith('/api/gateway'):
            # 代理网关请求
            self._proxy_gateway()
        else:
            self._send_html("<h1>404</h1>", 404)
    
    def do_POST(self):
        if self.path.startswith('/api/gateway'):
            self._proxy_gateway()
            return
        
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else ''
        
        if self.path == '/api/collect/start':
            result = start_collect()
            self._send_json(result)
        elif self.path == '/api/collect/stop':
            result = stop_collect()
            self._send_json(result)
        elif self.path == '/api/collect/status':
            result = get_collect_status()
            self._send_json(result)
        else:
            self._send_json({"error": "Not found"}, 404)
    
    def _proxy_gateway(self, body=None):
        """代理网关请求（支持 GET/POST，供 WebUI 内账号切换使用）"""
        path = self.path.replace('/api/gateway', '', 1)
        url = f"{GATEWAY_URL}{path}"
        if body is None:
            content_length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_length) if content_length > 0 else None
        
        try:
            req = urllib.request.Request(url, data=body, method=self.command)
            req.add_header('Authorization', f'Bearer {API_KEY}')
            if body is not None:
                req.add_header('Content-Type', self.headers.get('Content-Type', 'application/json'))
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = resp.read()
                self.send_response(resp.status)
                self.send_header('Content-Type', resp.headers.get('Content-Type', 'application/json'))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)
    
    def get_index_html(self):
        """返回主页面 HTML

        注意：这里必须是原始字符串（r\"\"\"...）。页面里的 JS 含 '\\n' 等
        反斜杠转义，如果是普通字符串，Python 会先把它变成真正的换行，
        导致 JS 字符串跨行未闭合（SyntaxError: Invalid or unexpected token），
        整个 <script> 失效、所有按钮都点不动。
        """
        return r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WorkBuddy 统一管理器</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1419; color: #e8eaed; }
.container { max-width: 1200px; margin: 0 auto; padding: 20px; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; padding: 20px; background: #1a1f2e; border-radius: 12px; }
.title { font-size: 24px; font-weight: bold; }
.subtitle { color: #8b9dc3; font-size: 14px; margin-top: 4px; }
.status-bar { display: flex; gap: 15px; }
.status-item { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; }
.dot-green { background: #34d399; box-shadow: 0 0 8px #34d399; }
.dot-red { background: #ef4444; }
.dot-yellow { background: #fbbf24; }
.tabs { display: flex; gap: 4px; margin-bottom: 20px; background: #1a1f2e; padding: 6px; border-radius: 10px; }
.tab { padding: 12px 20px; border-radius: 8px; cursor: pointer; font-size: 14px; transition: all 0.2s; }
.tab:hover { background: rgba(255,255,255,0.05); }
.tab.active { background: #4f46e5; color: white; }
.panel { display: none; background: #1a1f2e; border-radius: 12px; padding: 24px; }
.panel.active { display: block; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 20px; }
.card { background: #252b3d; border-radius: 10px; padding: 20px; }
.card-title { font-size: 13px; color: #8b9dc3; margin-bottom: 8px; text-transform: uppercase; }
.card-value { font-size: 28px; font-weight: bold; }
.card-sub { font-size: 12px; color: #8b9dc3; margin-top: 4px; }
.btn { padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; transition: all 0.2s; }
.btn-primary { background: #4f46e5; color: white; }
.btn-primary:hover { background: #4338ca; }
.btn-success { background: #059669; color: white; }
.btn-danger { background: #dc2626; color: white; }
.btn-sm { padding: 6px 12px; font-size: 12px; }
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #2d3447; }
th { color: #8b9dc3; font-size: 12px; text-transform: uppercase; }
.log-container { background: #0f1419; border-radius: 8px; padding: 16px; max-height: 400px; overflow-y: auto; font-family: 'Consolas', monospace; font-size: 12px; line-height: 1.6; }
.log-line { color: #8b9dc3; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.badge-success { background: rgba(52, 211, 153, 0.15); color: #34d399; }
.badge-warning { background: rgba(251, 191, 36, 0.15); color: #fbbf24; }
.badge-danger { background: rgba(239, 68, 68, 0.15); color: #ef4444; }
.switch { position: relative; display: inline-block; width: 44px; height: 24px; }
.switch input { opacity: 0; width: 0; height: 0; }
.slider { position: absolute; cursor: pointer; top: 0; left: 0; right: 0; bottom: 0; background: #4b5563; border-radius: 24px; transition: .3s; }
.slider:before { position: absolute; content: ""; height: 18px; width: 18px; left: 3px; bottom: 3px; background: white; border-radius: 50%; transition: .3s; }
input:checked + .slider { background: #4f46e5; }
input:checked + .slider:before { transform: translateX(20px); }
.link { color: #818cf8; text-decoration: none; }
.link:hover { text-decoration: underline; }
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <div class="title">WorkBuddy 统一管理器</div>
            <div class="subtitle">v1.0 - 集成 API 网关 + 账号管理 + 积分采集</div>
        </div>
        <div class="status-bar">
            <div class="status-item"><div class="status-dot" id="gateway-dot"></div><span id="gateway-status">网关</span></div>
            <div class="status-item"><div class="status-dot" id="collect-dot"></div><span id="collect-status">采集</span></div>
        </div>
    </div>
    
    <div class="tabs">
        <div class="tab active" onclick="switchTab('dashboard', event)">📊 仪表盘</div>
        <div class="tab" onclick="switchTab('console', event)">🖥️ 控制台</div>
        <div class="tab" onclick="switchTab('gateway', event)">🔌 API 网关</div>
        <div class="tab" onclick="switchTab('tasks', event)">🎯 任务中心</div>
        <div class="tab" onclick="switchTab('keepalive', event)">🛡️ 账号保活</div>
        <div class="tab" onclick="switchTab('realtime', event)">📈 实时面板</div>
        <div class="tab" onclick="switchTab('diagnose', event)">🔧 诊断</div>
    </div>
    
    <!-- 仪表盘 -->
    <div class="panel active" id="panel-dashboard">
        <div class="grid">
            <div class="card">
                <div class="card-title">账号数量</div>
                <div class="card-value" id="stat-accounts">-</div>
                <div class="card-sub">已配置账号</div>
            </div>
            <div class="card">
                <div class="card-title">总积分</div>
                <div class="card-value" id="stat-credits">-</div>
                <div class="card-sub">所有账号合计</div>
            </div>
            <div class="card">
                <div class="card-title">总能量</div>
                <div class="card-value" id="stat-energy">-</div>
                <div class="card-sub">所有账号合计</div>
            </div>
            <div class="card" id="stat-live-card" style="border-color:#f59e0b;background:rgba(245,158,11,0.06);">
                <div class="card-title">🔴 实时余额 <span id="live-ts" style="font-size:10px;color:#9ca3af;font-weight:normal;">(点击 🔄 强刷)</span></div>
                <div class="card-value" id="stat-live-credits" style="color:#f59e0b;">-</div>
                <div class="card-sub">上游 billing meter API · 30s 缓存<br><button class="btn btn-sm" onclick="refreshLiveCredits(true)" style="margin-top:4px;">🔄 强制刷新</button></div>
            </div>
            <div class="card">
                <div class="card-title">网关状态</div>
                <div class="card-value" id="stat-gateway">-</div>
                <div class="card-sub" id="stat-gateway-sub">端口 7863</div>
            </div>
        </div>
        
        <h3 style="margin: 20px 0 12px;">账号列表</h3>
        <div style="margin-bottom:10px;">
            <button class="btn btn-primary" onclick="openLoginModal()">➕ 添加/登录账号</button>
            <button class="btn btn-primary" onclick="refreshTokens()" style="margin-left:8px;">🔄 刷新 Token</button>
            <button class="btn btn-primary" onclick="reloadAuths()" style="margin-left:8px;">📥 重载账号池</button>
            <button class="btn" onclick="refreshStatus()" style="margin-left:8px;">🔄 刷新列表</button>
            <button class="btn btn-primary" onclick="rotateAccount()" style="margin-left:8px;" title="自动切到最闲+余额最高的可用账号，并清空会话粘性">🎲 一键换号</button>
        </div>
        <table>
            <thead>
                <tr><th>账号</th><th>手机号</th><th>登录方式</th><th>积分/能量</th><th>状态</th><th>操作</th></tr>
            </thead>
            <tbody id="accounts-table"></tbody>
        </table>
        <div id="login-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:100;">
            <div style="background:#1a1f2e;margin:80px auto;padding:24px;border-radius:12px;max-width:720px;">
                <h3>WorkBuddy OAuth 登录</h3>
                <p id="login-msg" style="color:#8b9dc3;margin:10px 0;">点击按钮生成登录链接，然后用浏览器打开完成登录。</p>
                <a id="login-link" href="#" target="_blank" style="word-break:break-all;color:#5ce7ff;">等待生成链接...</a>
                <pre id="login-state" style="background:#0f1419;padding:10px;border-radius:8px;margin:12px 0;word-break:break-all;"></pre>
                <input type="hidden" id="login-state-value" value="">
                <div style="display:flex;gap:8px;margin-top:12px;">
                    <button class="btn btn-primary" onclick="startLogin()">生成登录链接</button>
                    <button class="btn" onclick="pollLogin()">确认已登录</button>
                    <button class="btn" onclick="closeLoginModal()">关闭</button>
                </div>
            </div>
        </div>
    </div>
    
    <!-- 控制台 -->
    <div class="panel" id="panel-console">
        <h3>积分采集</h3>
        <p style="color: #8b9dc3; margin: 10px 0;">一键采集所有账号的积分、能量数据</p>
        <div style="display: flex; gap: 10px; margin: 15px 0;">
            <button class="btn btn-primary" onclick="startCollect()" id="btn-collect">🚀 开始采集</button>
            <button class="btn btn-danger" onclick="stopCollect()" id="btn-stop" disabled>⏹️ 停止</button>
            <button class="btn btn-success btn-sm" onclick="viewReport()">📄 查看报告</button>
        </div>
        
        <h4 style="margin: 20px 0 10px;">实时日志</h4>
        <div class="log-container" id="collect-log">等待采集开始...</div>
    </div>
    
    <!-- 任务中心 -->
    <div class="panel" id="panel-tasks">
        <div class="card" style="margin-bottom:16px;">
            <div class="card-title">成长计划任务</div>
            <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:8px;">
                <button class="btn btn-primary btn-sm" onclick="taskScan()">🔍 扫描全账号任务</button>
                <button class="btn btn-primary btn-sm" onclick="taskRun()">🚀 一键完成全部（已做自动跳过）</button>
                <button class="btn btn-success btn-sm" onclick="taskRun(true)">🧪 预演 dry-run</button>
                <label style="font-size:12px; color:#8b9dc3; align-self:center;"><input type="checkbox" id="task-claim" checked> 达标后自动领奖</label>
            </div>
        </div>
        <div id="task-progress" class="card" style="margin-bottom:12px;">
            <div class="card-title">当前队列</div>
            <div class="card-sub" id="task-current">未开始</div>
        </div>
        <div id="tasks-log" class="log-container" style="max-height: 360px;">任务日志将显示在这里...</div>
    </div>
    
    <!-- 账号保活 -->
    <div class="panel" id="panel-keepalive">
        <div class="card" style="margin-bottom:16px;">
            <div class="card-title">账号保活</div>
            <div style="display:flex; gap:10px; flex-wrap:wrap; margin-top:8px;">
                <button class="btn btn-primary btn-sm" onclick="keepaliveRun()">🛡️ 全账号保活</button>
                <button class="btn btn-success btn-sm" onclick="keepaliveRun(true)">🧪 dry-run 预览</button>
            </div>
            <p style="color:#8b9dc3; font-size:12px; margin-top:8px;">对每个账号尝试刷新 token，失败则退化到 streak 探针，结果写入 wb_data/tasks_log.jsonl。</p>
        </div>
        <div id="keepalive-log" class="log-container" style="max-height: 360px;">保活日志将显示在这里...</div>
    </div>
    
    <!-- API 网关 -->
    <div class="panel" id="panel-gateway">
        <div class="grid">
            <div class="card">
                <div class="card-title">网关地址</div>
                <div style="font-size: 16px;">http://127.0.0.1:7863</div>
            </div>
            <div class="card">
                <div class="card-title">管理面板</div>
                <a href="http://127.0.0.1:7863/panel/" target="_blank" class="link">打开面板 →</a>
            </div>
            <div class="card">
                <div class="card-title">API Key</div>
                <code style="font-size: 12px; word-break: break-all;">sk-CHANGEME-please-set-your-own-key</code>
            </div>
            <div class="card">
                <div class="card-title">状态</div>
                <div id="gateway-detail" class="card-value" style="font-size: 20px;">-</div>
            </div>
        </div>
        
        <h3 style="margin: 20px 0 12px;">账号池</h3>
        <div id="pool-info">加载中...</div>
    </div>
    
    <!-- 实时面板 -->
    <div class="panel" id="panel-realtime">
        <p style="color:#8b9dc3;margin-bottom:10px;">
            下面是网关自带的实时面板。为避免重复输入密钥，本页会自动把 API Key 通过网址片段（#key=）传给面板。
        </p>
        <iframe id="realtime-frame" data-src="http://127.0.0.1:7863/panel/" style="width: 100%; height: 600px; border: none; border-radius: 8px;"></iframe>
    </div>
    
    <!-- 诊断 -->
    <div class="panel" id="panel-diagnose">
        <h3>系统诊断</h3>
        <div id="diagnose-output" class="log-container" style="max-height: 500px;">点击诊断按钮开始...</div>
        <button class="btn btn-primary" onclick="runDiagnose()" style="margin-top: 15px;">🔍 运行诊断</button>
    </div>
</div>

<script>
// 服务端在返回本页时把 __API_KEY__ 替换为真实密钥（见 get_index_html 末尾的 .replace）。
// 之前 JS 里到处用 API_KEY 却从未声明，导致登录/任务/刷新等按钮一调用就抛
// "API_KEY is not defined"，这也是“模块点不动”的原因之一。
const API_KEY = "__API_KEY__";
let refreshTimer;

function switchTab(name, event) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    if (event && event.currentTarget) event.currentTarget.classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
    
    if (name === 'gateway') loadGatewayInfo();
    if (name === 'tasks') taskLoadList();
    if (name === 'keepalive') keepaliveLoad();
    if (name === 'diagnose') runDiagnose();
}

async function refreshStatus() {
    try {
        const resp = await fetch('/api/status');
        const data = await resp.json();
        
        // 更新状态点
        const gw = data.gateway;
        document.getElementById('gateway-dot').className = 'status-dot ' + (gw.status === 'running' ? 'dot-green' : 'dot-red');
        document.getElementById('gateway-status').textContent = '网关 ' + gw.status;
        
        const col = data.collect;
        document.getElementById('collect-dot').className = 'status-dot ' + (col.running ? 'dot-green' : 'dot-red');
        document.getElementById('collect-status').textContent = '采集 ' + (col.running ? '运行中' : '停止');
        
        // 更新统计
        document.getElementById('stat-accounts').textContent = data.accounts.length;
        // 总积分 / 总能量：累加所有账号的 credits/energy（富集自 wb_data/credits_history.json）
        const totalCredits = (data.accounts || []).reduce((s, a) => s + (Number(a.credits) || 0), 0);
        const totalEnergy = (data.accounts || []).reduce((s, a) => s + (Number(a.energy) || 0), 0);
        document.getElementById('stat-credits').textContent = totalCredits || 0;
        document.getElementById('stat-energy').textContent = totalEnergy || 0;
        
        // 实时余额（30s 缓存，走独立端点，不阻塞主状态刷新）
        refreshLiveCredits(false);

        // 用实时余额覆盖 accounts 里的 credits/energy（比历史快照更准）
        // 通过 /api/credits/live 单独拉一次；若已有缓存则不会打上游
        const liveResp = await fetch('/api/credits/live');
        const liveData = await liveResp.json();
        if (liveData && liveData.accounts) {
            for (const a of data.accounts) {
                if (a.uid && liveData.accounts[a.uid]) {
                    const lv = liveData.accounts[a.uid];
                    if (lv.remain > 0 || lv.total > 0) {
                        a.credits = lv.remain;
                        a.energy = lv.total;
                    }
                }
            }
        }
        
        const tbody = document.getElementById('accounts-table');
        const methodMap = { oauth:'OAuth 扫码', wechat:'📱 微信', phone:'📞 手机' };
        const statusMap = { active:'已登录', expired:'Token 已过期', invalid:'Token 失效', unbound:'未登录' };
        tbody.innerHTML = data.accounts.map(a => {
            const canSwitch = !!(a.uid && a.status === 'active');
            const btn = canSwitch
                ? `<button class="btn btn-sm btn-primary" onclick="switchAccount('${a.uid}')">切换</button>`
                : `<button class="btn btn-sm" disabled style="opacity:.45;cursor:not-allowed;">切换</button>`;
            const badge = a.status === 'active' ? 'badge-success' : 'badge-warning';
            const cred = a.token
                ? `<small>${a.token}${a.expires_at_human ? '<br>有效至 ' + a.expires_at_human : ''}</small>`
                : '-';
            const creditsTxt = a.credits != null
                ? `<b style="color:${a.credits < 30 ? '#ef4444' : (a.credits < 100 ? '#f59e0b' : '#fbbf24')}">${a.credits}</b> · ⚡${a.energy || 0}`
                : '-';
            return `
            <tr>
                <td>${a.name || '-'}<br><small>${a.uid||''}</small></td>
                <td>${a.phone || '-'}</td>
                <td>${methodMap[a.login_method] || a.login_method || '-'}</td>
                <td>${creditsTxt}</td>
                <td><span class="badge ${badge}">${statusMap[a.status] || a.status || '未知'}</span></td>
                <td>${btn}</td>
            </tr>`;
        }).join('') || '<tr><td colspan="6">暂无可用 auths/*.json，请先点击「添加/登录账号」完成 OAuth 登录</td></tr>';
        
    } catch (e) {
        console.error('刷新失败:', e);
    }
}

async function refreshLiveCredits(force) {
    try {
        const url = '/api/credits/live' + (force ? '?refresh=1' : '');
        const resp = await fetch(url);
        const d = await resp.json();
        const el = document.getElementById('stat-live-credits');
        const ts = document.getElementById('live-ts');
        if (d && d.total_remain != null) {
            const remain = d.total_remain || 0;
            const size = d.total_size || 0;
            const expiring = d.expiring_7d || 0;
            el.textContent = remain + (size ? ' / ' + size : '');
            // 三档颜色：>100 常规橙、30-100 警示黄、<30 危险红；0 也归入红
            el.style.color = remain > 100 ? '#f59e0b' : (remain > 30 ? '#fb923c' : '#ef4444');
            // 同步把总积分卡片也染色，让顶部一眼可见
            const statCredits = document.getElementById('stat-credits');
            if (statCredits) {
                statCredits.style.color = remain > 100 ? '' : (remain > 30 ? '#f59e0b' : '#ef4444');
            }
            const tsText = d.queried_at
                ? '查询于 ' + new Date(d.queried_at).toLocaleTimeString() + (expiring > 0 ? ' · ⚠️' + expiring + ' 7天内到期' : '') + (remain < 30 ? ' · 🚨 余额告急' : (remain < 100 ? ' · ⚠️ 余额偏低' : ''))
                : (force ? '强制刷新失败' : '无数据');
            ts.textContent = tsText;
        } else {
            el.textContent = '-';
            ts.textContent = '无实时余额数据';
        }
    } catch (e) {
        console.error('实时余额刷新失败:', e);
        document.getElementById('stat-live-credits').textContent = '获取失败';
    }
}

async function loadGatewayInfo() {
    try {
        const resp = await fetch('/api/status');
        const data = await resp.json();
        const gw = data.gateway;
        document.getElementById('gateway-detail').textContent = gw.status;
        document.getElementById('gateway-detail').style.color = gw.status === 'running' ? '#34d399' : '#ef4444';
        
        // 加载网关账号池信息
        try {
            const poolResp = await fetch('/api/gateway/status');
            const poolData = await poolResp.json();
            const currentName = (poolData.accounts || []).find(a => a.id === poolData.current)?.name || poolData.current || '未选择';
            const poolRows = (poolData.accounts || []).map(a => `
                <tr>
                    <td>${a.name || '-'}</td>
                    <td>${a.phone || '-'}</td>
                    <td>${a.credits || 0}</td>
                    <td>${a.available ? '可用' : '不可用'}</td>
                    <td><button class="btn btn-sm btn-primary" onclick="switchAccount('${a.id}')">切换</button></td>
                </tr>
            `).join('') || '<tr><td colspan="5">暂无已登录账号。请点上方「➕ 添加/登录账号」完成一次扫码登录（会写入 auths/）。</td></tr>';
            document.getElementById('pool-info').innerHTML = `
                <div class="card" style="margin-bottom:12px;">
                    <div class="card-title">当前使用账号</div>
                    <div class="card-value" style="font-size:20px;">${currentName}</div>
                </div>
                <table>
                    <thead><tr><th>账号</th><th>手机号</th><th>积分</th><th>状态</th><th>操作</th></tr></thead>
                    <tbody>${poolRows}</tbody>
                </table>
            `;
        } catch (e) {
            document.getElementById('pool-info').innerHTML = '<p style="color: #8b9dc3;">无法加载账号池信息</p>';
        }
    } catch (e) {
        console.error('加载网关信息失败:', e);
    }
}

async function taskLoadList() {
    const box = document.getElementById('tasks-log');
    box.innerHTML = '<div class="log-line">加载任务列表...</div>';
    try {
        const resp = await fetch('/api/gateway/tasks/list', { headers: { 'Authorization': 'Bearer ' + API_KEY } });
        const data = await resp.json();
        if (!data.ok) {
            box.innerHTML = `<div class="log-line" style="color:#ef4444">加载失败: ${data.error || JSON.stringify(data)}</div>`;
            return;
        }
        // 把内部执行策略代号（kind）翻成中文，不要把 canvas/chat5 这类代号直接丢给用户
        const kindMap = {
            first_buddy:'首次对话', canvas:'设计画布', chat5:'对话 ×5',
            model_chat_glm:'模型对话', richmeow:'桌面端对话', buddy_app:'Buddy 应用',
            buddy_app_qq:'企鹅教师助手', automation:'自动化任务', library_read:'资料库阅读',
            template5:'模板 ×5', playbook_prompt:'灵感做同款', expert5:'专家 ×5',
            expert_team3:'专家团 ×3', appearance:'主题外观', expert_lighthouse:'轻量云专家',
            skill1:'技能体验', manual:'需人工，不可自动'
        };
        const autoSet = new Set(data.auto_codes || []);
        const rows = (data.tasks || []).map(t => {
            const auto = autoSet.has(t.code);
            return `<div class="log-line"><b>${t.code}</b> · ${t.reward} · ${t.desc} `
                 + `<span style="color:#818cf8">${kindMap[t.kind] || t.kind || '未分类'}</span> `
                 + `<span style="color:${auto ? '#34d399' : '#f59e0b'}">[${auto ? '可自动' : '需人工'}]</span></div>`;
        }).join('');
        box.innerHTML = rows || '<div class="log-line">暂无任务</div>';
    } catch (e) {
        box.innerHTML = `<div class="log-line" style="color:#ef4444">请求失败: ${e.message}</div>`;
    }
}

function appendTaskLog(msg, color) {
    const box = document.getElementById('tasks-log');
    const div = document.createElement('div');
    div.className = 'log-line';
    if (color) div.style.color = color;
    div.textContent = msg;
    box.appendChild(div);
    box.scrollTop = box.scrollHeight;
}

async function taskScan() {
    document.getElementById('task-current').textContent = '扫描中...';
    appendTaskLog('🔍 开始扫描全账号任务...');
    try {
        const resp = await fetch('/api/gateway/tasks/scan', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + API_KEY, 'Content-Type': 'application/json' },
            body: JSON.stringify({ dry_run: true })
        });
        const data = await resp.json();
        if (!data.ok) {
            appendTaskLog('扫描失败: ' + (data.error || JSON.stringify(data)), '#ef4444');
            document.getElementById('task-current').textContent = '扫描失败';
            return;
        }
        document.getElementById('task-current').textContent = `账号 ${data.accounts.length} 个，匹配任务 ${data.tasks.length} 条`;
        appendTaskLog(`扫描完成：账号 ${data.accounts.length} 个，匹配任务 ${data.tasks.length} 条`, '#34d399');
        (data.tasks || []).forEach(t => {
            // accept_status 是上游字段，必须给出兜底文案，否则界面会出现 undefined
            const stMap = { accepted:'已接取', in_progress:'进行中', completed:'已完成待领奖', claimed:'已领奖' };
            const st = stMap[t.accept_status] || t.accept_status || '未获取';
            appendTaskLog(` • [${t.name || t.uid || '未知账号'}] ${t.task_code || '未知任务'} ${st}`);
        });
    } catch (e) {
        appendTaskLog('扫描请求异常: ' + e.message, '#ef4444');
    }
}

async function taskRun(dryRun = false) {
    document.getElementById('task-current').textContent = dryRun ? '预演中...' : '执行中...';
    const logBox = document.getElementById('tasks-log');
    logBox.innerHTML = `<div class="log-line">${dryRun ? '🧪 dry-run' : '🚀 执行'}成长任务一键完成...</div>`;
    try {
        const resp = await fetch('/api/gateway/tasks/run', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + API_KEY, 'Content-Type': 'application/json' },
            body: JSON.stringify({ dry_run: dryRun, claim: document.getElementById('task-claim')?.checked })
        });
        const data = await resp.json();
        if (!data.ok) {
            logBox.innerHTML += `<div class="log-line" style="color:#ef4444">失败: ${data.error || JSON.stringify(data)}</div>`;
            document.getElementById('task-current').textContent = '失败';
            return;
        }
        const accounts = data.accounts || [];
        let done = 0, partial = 0, noeffect = 0, manual = 0;
        accounts.forEach(acc => {
            logBox.innerHTML += `<div class="log-line"><b>${acc.name || acc.uid}</b> 跳过${acc.skipped||0} 错误${acc.errors||0}</div>`;
            (acc.steps || []).forEach(step => {
                const b = step.progress_before || {};
                const a = step.progress_after || {};
                const prog = a.current != null
                    ? `（${b.current||0}/${b.target||'-'} → ${a.current}/${a.target||'-'}）`
                    : '';
                const tag = step.skipped ? '⏭️' : (step.ok ? '✅' : '❌');
                const cls = step.ok ? '#34d399' : (step.skipped ? '#8b9dc3' : '#ef4444');
                if (step.ok && !step.skipped) done++;
                else if (!step.skipped && a.current != null && a.current > (b.current||0)) partial++;
                else if (!step.skipped && step.effect === '未计入进度') noeffect++;
                else if (!step.skipped && step.effect === '未知') manual++;
                const claimTxt = step.claim_ok ? ' · 🎁已领奖' : (step.claim ? ' · 🎁领奖失败' : '');
                logBox.innerHTML +=
                    `<div class="log-line" style="color:${cls}">${tag} ${step.code}${prog}: ${step.msg || ''}${claimTxt}</div>`;
            });
        });
        // 汇总：如实区分「真完成 / 部分进度 / 上报无效 / 需人工」
        if (done === 0 && noeffect > 0) {
            logBox.innerHTML += `<div class="log-line" style="color:#f59e0b">⚠ ${noeffect} 个任务上报后被上游判定无效（成长计划进度未变），请手动完成对应操作</div>`;
        }
        if (partial > 0) {
            logBox.innerHTML += `<div class="log-line" style="color:#fbbf24">📊 ${partial} 个任务有进度但未达标，需再做几次</div>`;
        }
        logBox.innerHTML += `<div class="log-line" style="color:#8b9dc3">汇总：完成${done} · 进行中${partial} · 未计入${noeffect} · 需人工${manual}</div>`;
        document.getElementById('task-current').textContent =
            `完成 ${done} 个任务` + (partial ? `，${partial} 个进行中` : '');
    } catch (e) {
        logBox.innerHTML += `<div class="log-line" style="color:#ef4444">请求异常: ${e.message}</div>`;
    }
}

async function keepaliveLoad() {
    const box = document.getElementById('keepalive-log');
    box.innerHTML = '<div class="log-line">准备保活...</div>';
}

async function keepaliveRun(dryRun = false) {
    const box = document.getElementById('keepalive-log');
    box.innerHTML = `<div class="log-line">${dryRun ? '🧪 dry-run' : '🛡️'} 保活中...</div>`;
    try {
        const resp = await fetch('/api/gateway/tasks/keepalive', {
            method: 'POST',
            headers: { 'Authorization': 'Bearer ' + API_KEY, 'Content-Type': 'application/json' },
            body: JSON.stringify({ dry_run: dryRun })
        });
        const data = await resp.json();
        if (!data.ok) {
            box.innerHTML += `<div class="log-line" style="color:#ef4444">失败: ${data.error || JSON.stringify(data)}</div>`;
            return;
        }
        (data.accounts || []).forEach(acc => {
            box.innerHTML += `<div class="log-line">${acc.ok ? '✅' : '❌'} ${acc.name || acc.uid}: ${acc.msg || acc.status || ''}</div>`;
        });
        box.innerHTML += `<div class="log-line" style="color:#34d399">保活扫描完成</div>`;
    } catch (e) {
        box.innerHTML += `<div class="log-line" style="color:#ef4444">请求异常: ${e.message}</div>`;
    }
}

async function startCollect() {
    document.getElementById('btn-collect').disabled = true;
    document.getElementById('btn-stop').disabled = false;
    document.getElementById('collect-log').innerHTML = '<div class="log-line">启动采集中...</div>';
    
    try {
        const resp = await fetch('/api/collect/start', { method: 'POST' });
        const result = await resp.json();
        
        if (result.success) {
            document.getElementById('collect-log').innerHTML = '<div class="log-line">✓ ' + result.message + '</div>';
            // 开始轮询日志
            pollCollectLog();
        } else {
            document.getElementById('collect-log').innerHTML = '<div class="log-line" style="color: #ef4444;">✗ ' + result.message + '</div>';
            document.getElementById('btn-collect').disabled = false;
            document.getElementById('btn-stop').disabled = true;
        }
    } catch (e) {
        document.getElementById('collect-log').innerHTML = '<div class="log-line" style="color: #ef4444;">✗ 错误: ' + e.message + '</div>';
        document.getElementById('btn-collect').disabled = false;
        document.getElementById('btn-stop').disabled = true;
    }
}

async function stopCollect() {
    try {
        const resp = await fetch('/api/collect/stop', { method: 'POST' });
        const result = await resp.json();
        document.getElementById('collect-log').innerHTML += '<div class="log-line">✓ ' + result.message + '</div>';
        document.getElementById('btn-collect').disabled = false;
        document.getElementById('btn-stop').disabled = true;
    } catch (e) {
        console.error('停止失败:', e);
    }
}

let logPollTimer;
function pollCollectLog() {
    clearInterval(logPollTimer);
    logPollTimer = setInterval(async () => {
        try {
            const resp = await fetch('/api/logs');
            const data = await resp.json();
            document.getElementById('collect-log').innerHTML = data.log.map(l => `<div class="log-line">${l}</div>`).join('');
            
            // 检查是否还在运行
            const statusResp = await fetch('/api/collect/status');
            const status = await statusResp.json();
            if (!status.running) {
                clearInterval(logPollTimer);
                document.getElementById('btn-collect').disabled = false;
                document.getElementById('btn-stop').disabled = true;
                if (status.hint) {
                    document.getElementById('collect-log').innerHTML +=
                        `<div class="log-line" style="color:#f59e0b">⚠ ${status.hint}</div>`;
                } else if (status.exit_code) {
                    document.getElementById('collect-log').innerHTML +=
                        `<div class="log-line" style="color:#ef4444">✗ 采集进程已退出（exit code ${status.exit_code}），详情见 wb_data/fetch_log.txt</div>`;
                }
            }
        } catch (e) {
            console.error('轮询日志失败:', e);
        }
    }, 2000);
}

async function switchAccount(accountId) {
    try {
        const resp = await fetch('/api/gateway/api/switch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + API_KEY},
            body: JSON.stringify({ account_id: accountId })
        });
        const result = await resp.json();
        if (result.success) {
            alert('已切换到账号: ' + accountId);
            loadGatewayInfo();
        } else {
            alert('切换失败: ' + (result.error || result));
        }
    } catch (e) {
        alert('切换失败: ' + e.message);
    }
}

async function rotateAccount() {
    // 一键换号：让网关自动挑下一个最闲+余额最高的可用账号，并清空所有会话粘性
    if (!confirm('要自动切换到下一个可用账号吗？\n（会清空当前所有会话粘性）')) return;
    try {
        const resp = await fetch('/api/gateway/api/switch', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + API_KEY},
            body: JSON.stringify({ rotate: true })
        });
        const result = await resp.json();
        if (result.success) {
            alert('已换号到: ' + result.account_id);
            refreshStatus();
            loadGatewayInfo();
        } else {
            alert('换号失败: ' + (result.error || JSON.stringify(result)));
        }
    } catch (e) {
        alert('换号失败: ' + e.message);
    }
}

function viewReport() {
    window.open('/api/report', '_blank');
}

async function runDiagnose() {
    const output = document.getElementById('diagnose-output');
    output.innerHTML = '<div class="log-line">诊断中...</div>';

    const checks = [
        ['Python 运行时', '/api/diagnose/python'],
        ['网关连通性', '/api/diagnose/gateway'],
        ['依赖与文件', '/api/diagnose/deps'],
    ];
    let failed = 0;
    for (const [label, url] of checks) {
        try {
            const resp = await fetch(url);
            const text = await resp.text();
            if (resp.ok) {
                output.innerHTML += '<div class="log-line" style="color:#34d399;">✓ ' + label + ': ' + text + '</div>';
            } else {
                failed++;
                output.innerHTML += '<div class="log-line" style="color:#ef4444;">✗ ' + label + ' (HTTP ' + resp.status + ')</div>';
            }
        } catch (e) {
            failed++;
            output.innerHTML += '<div class="log-line" style="color:#ef4444;">✗ ' + label + ': ' + e.message + '</div>';
        }
    }
    output.innerHTML += failed === 0
        ? '<div class="log-line" style="color:#34d399;">✓ 诊断完成，全部通过</div>'
        : '<div class="log-line" style="color:#f59e0b;">⚠ 诊断完成，' + failed + ' 项异常</div>';
}

function openLoginModal() {
    document.getElementById('login-modal').style.display = 'block';
    document.getElementById('login-msg').textContent = '点击按钮生成登录链接，然后用浏览器打开完成登录。';
    document.getElementById('login-state').textContent = '';
    document.getElementById('login-state-value').value = '';
    document.getElementById('login-link').href = '#';
    document.getElementById('login-link').textContent = '等待生成链接...';
}

function closeLoginModal() {
    document.getElementById('login-modal').style.display = 'none';
}

async function startLogin() {
    const msg = document.getElementById('login-msg');
    const link = document.getElementById('login-link');
    const stateBox = document.getElementById('login-state');
    msg.textContent = '正在生成登录链接...';
    try {
        const resp = await fetch('/api/gateway/panel/api/login/start?realm=cn', { headers: { 'Authorization': 'Bearer ' + API_KEY } });
        const data = await resp.json();
        if (!data.ok) {
            msg.textContent = '生成失败: ' + (data.error || JSON.stringify(data));
            return;
        }
        msg.textContent = '请在浏览器打开下面链接，完成扫码/登录。';
        link.href = data.url;
        link.textContent = '打开 WorkBuddy 登录页 →';
        document.getElementById('login-state-value').value = data.state || '';
        stateBox.textContent = 'state: ' + (data.state || '') + ' | realm: ' + (data.realm || 'cn');
    } catch (e) {
        msg.textContent = '请求异常: ' + e.message;
    }
}

async function pollLogin() {
    const state = document.getElementById('login-state-value').value;
    const msg = document.getElementById('login-msg');
    if (!state) {
        msg.textContent = '没有 state，请先点击「生成登录链接」。';
        return;
    }
    msg.textContent = '正在确认登录结果...';
    try {
        const resp = await fetch('/api/gateway/panel/api/login/poll?state=' + encodeURIComponent(state), { headers: { 'Authorization': 'Bearer ' + API_KEY } });
        const data = await resp.json();
        if (!data.ok) {
            msg.textContent = '登录未完成或失败: ' + (data.error || JSON.stringify(data));
            return;
        }
        if (!data.done) {
            msg.textContent = data.message || '等待登录...';
            return;
        }
        msg.textContent = '✓ 登录成功，已写入 ' + (data.file || 'auths') + '，uid=' + data.uid + '，昵称=' + (data.nickname || '-');
        document.getElementById('login-state').textContent = '✓ 完成\n' + JSON.stringify(data, null, 2);
        await refreshStatus();
    } catch (e) {
        msg.textContent = '请求异常: ' + e.message;
    }
}

// 重载账号池：把 auths/*.json 重新灌进网关内存池。
// 解决「已经扫码登录了，但账号池还是 0 个」——旧版只在网关启动时加载一次。
async function reloadAuths() {
    try {
        const resp = await fetch('/api/gateway/api/reload-auths', { headers: { 'Authorization': 'Bearer ' + API_KEY } });
        const data = await resp.json();
        if (!data.ok) {
            alert('重载失败: ' + (data.error || JSON.stringify(data)));
        } else {
            alert(`账号池已重载：${data.pool_accounts} 个账号，auths/ 共 ${data.auth_files} 个文件`);
        }
        await refreshStatus();
    } catch (e) {
        alert('重载请求异常: ' + e.message);
    }
}

async function refreshTokens() {
    const btn = event && event.currentTarget;
    if (btn) btn.disabled = true;
    try {
        const resp = await fetch('/api/gateway/api/refresh-tokens', { headers: { 'Authorization': 'Bearer ' + API_KEY } });
        const data = await resp.json();
        if (!data.ok) {
            alert('刷新失败: ' + (data.error || JSON.stringify(data)));
        } else {
            alert('Token 刷新完成');
        }
        await refreshStatus();
    } catch (e) {
        alert('刷新请求异常: ' + e.message);
    } finally {
        if (btn) btn.disabled = false;
    }
}

// 实时面板 iframe：懒加载，并把 API Key 通过 hash 传给网关面板页，
// 免得用户在内嵌页面里再手输一次密钥（hash 不会发往服务器）。
function initRealtimeFrame() {
    const frame = document.getElementById('realtime-frame');
    if (!frame) return;
    const base = frame.getAttribute('data-src') || 'http://127.0.0.1:7863/panel/';
    frame.src = base + '#key=' + encodeURIComponent(API_KEY || '');
}

// 启动
initRealtimeFrame();
refreshStatus();
refreshTimer = setInterval(refreshStatus, 10000);
// 实时余额独立轮询：15s 一次（后端 30s 缓存，前端 15s 保证用户看到的是新鲜的）
setInterval(() => refreshLiveCredits(false), 15000);
</script>
</body>
</html>""".replace('__API_KEY__', API_KEY)