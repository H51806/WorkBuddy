#!/usr/bin/env python3
"""
WorkBuddy API 网关 - Python 版
================================
OpenAI 兼容 API 网关，支持：
  - 账号池管理
  - Token 转发
  - 账号切换
  - 速率限制
  - 状态持久化
  - Web 管理面板

作者：WorkBuddy 统一管理器
版本：1.0.0
"""

import base64
import json
import os
import sys
import time
import threading
import hashlib
import uuid
import logging
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field, asdict

# ===== 配置 =====
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
USAGE_FILE = DATA_DIR / "usage.json"
AUTH_DIR = BASE_DIR / "auths"
CHAT_BASE = "https://copilot.tencent.com"

# 上游真实可用的模型 id（逐个实测确认，其它名字上游一律回
# {"code":11102,"msg":"model [x] service info not found"}）。
REAL_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4.1-flash",
    "deepseek-v4-pro",
    "deepseek-v3",
    "deepseek-v3.2",
    "deepseek-r1",
    "glm-5.2",
    "glm-5.3",
]
# 客户端（如各类 Agent/Harness）常写死 gpt-4o 之类的名字，这里统一兜底到默认模型，
# 避免直接把不可用的模型名透给上游而报 400。
DEFAULT_CHAT_MODEL = "deepseek-v4-flash"

DEFAULT_CONFIG = {
    "listen": ":7863",
    "api_key": "sk-default-key-change-me",
    "auth_dir": "./auths",
    "state_file": "./data/state.json",
    "upstream": {
        "timeout_seconds": 120,
        "base_url": "https://copilot.tencent.com"
    },
    "pool": {
        "max_in_flight": 3,
        "breaker_threshold": 3,
        "breaker_cooldown": "30m",
        "prefer_expiring": True
    },
    "schedule": {
        "checkin_hours": [9, 21],
        "checkin_enabled": True,
        "balance_refresh_minutes": 5
    }
}

# ===== 日志配置 =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("wb-gateway")

# ===== 数据模型 =====

@dataclass
class Account:
    """账号信息"""
    id: str
    name: str
    token: str
    phone: str = ""
    domain: str = ""
    credits: int = 0
    energy: int = 0
    active: bool = True
    last_used: float = 0
    request_count: int = 0
    success_count: int = 0
    fail_count: int = 0
    breaker_until: float = 0  # 熔断器截止时间
    cooldown_until: float = 0  # 冷却截止时间
    balance_checked_at: float = 0


@dataclass
class AccountPool:
    """账号池"""
    accounts: Dict[str, Account] = field(default_factory=dict)
    current_account_id: Optional[str] = None
    sticky_sessions: Dict[str, str] = field(default_factory=dict)  # session_id -> account_id
    session_ttl: Dict[str, float] = field(default_factory=dict)  # session_id -> expires_at
    
    def pick(self, session_id: Optional[str] = None) -> Optional[Account]:
        """选择账号"""
        # 1. 粘性会话优先
        if session_id and session_id in self.sticky_sessions:
            acc_id = self.sticky_sessions[session_id]
            if acc_id in self.accounts:
                acc = self.accounts[acc_id]
                if self._is_available(acc):
                    return acc
        
        # 2. 轮换选择可用账号
        available = [
            acc for acc in self.accounts.values()
            if self._is_available(acc) and acc.active
        ]
        
        if not available:
            # 所有账号都不可用，返回任意一个（让调用方处理错误）
            return next(iter(self.accounts.values()), None)
        
        # 选择最闲的账号
        available.sort(key=lambda a: (a.last_used, -a.credits))
        chosen = available[0]
        
        # 设置粘性会话
        if session_id:
            self.sticky_sessions[session_id] = chosen.id
            self.session_ttl[session_id] = time.time() + 1800  # 30 分钟 TTL
        
        return chosen
    
    def _is_available(self, acc: Account) -> bool:
        """检查账号是否可用"""
        now = time.time()
        if not acc.active:
            return False
        if now < acc.breaker_until:
            return False
        if now < acc.cooldown_until:
            return False
        return True
    
    def record_success(self, acc_id: str):
        """记录成功请求"""
        if acc_id in self.accounts:
            acc = self.accounts[acc_id]
            acc.request_count += 1
            acc.success_count += 1
            acc.last_used = time.time()
            acc.fail_count = 0
    
    def record_failure(self, acc_id: str, is_rate_limit: bool = False):
        """记录失败请求"""
        if acc_id in self.accounts:
            acc = self.accounts[acc_id]
            acc.request_count += 1
            acc.fail_count += 1
            acc.last_used = time.time()
            
            # 熔断器
            if acc.fail_count >= 3:
                acc.breaker_until = time.time() + 1800  # 30 分钟熔断
                log.warning(f"账号 {acc.name} 熔断中，30 分钟后恢复")
            
            # 限流冷却
            if is_rate_limit:
                acc.cooldown_until = time.time() + 600  # 10 分钟冷却
    
    def add_account(self, acc: Account):
        """添加账号"""
        self.accounts[acc.id] = acc
    
    def remove_account(self, acc_id: str):
        """移除账号"""
        if acc_id in self.accounts:
            del self.accounts[acc_id]
            if self.current_account_id == acc_id:
                self.current_account_id = None
    
    def switch_account(self, acc_id: str) -> bool:
        """切换当前账号"""
        if acc_id in self.accounts:
            self.current_account_id = acc_id
            return True
        return False
    
    def get_status(self) -> Dict:
        """获取状态"""
        now = time.time()
        return {
            "total": len(self.accounts),
            "active": sum(1 for a in self.accounts.values() if a.active),
            "available": sum(1 for a in self.accounts.values() if self._is_available(a)),
            "current": self.current_account_id,
            "accounts": [
                {
                    "id": acc.id,
                    "name": acc.name,
                    "phone": acc.phone,
                    "credits": acc.credits,
                    "energy": acc.energy,
                    "active": acc.active,
                    "available": self._is_available(acc),
                    "requests": acc.request_count,
                    "success": acc.success_count,
                    "failures": acc.fail_count,
                    "last_used": acc.last_used,
                }
                for acc in self.accounts.values()
            ]
        }
    
    def save(self, path: Path):
        """保存状态"""
        data = {
            "current_account_id": self.current_account_id,
            "sticky_sessions": self.sticky_sessions,
            "accounts": [asdict(acc) for acc in self.accounts.values()]
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    def load(self, path: Path):
        """加载状态"""
        if not path.exists():
            return
        
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        self.current_account_id = data.get("current_account_id")
        self.sticky_sessions = data.get("sticky_sessions", {})
        
        for acc_data in data.get("accounts", []):
            acc = Account(**acc_data)
            self.accounts[acc.id] = acc
        
        log.info(f"已加载 {len(self.accounts)} 个账号")


# ===== 全局状态 =====
pool = AccountPool()
config = DEFAULT_CONFIG.copy()
api_key = ""
server_start_time = time.time()
wb_tasks = None
log_tasks_import_err = None
wb_auth = None
log_auth_import_err = None


# ===== 配置加载 =====

def load_config() -> Dict:
    """加载配置"""
    global config, api_key
    
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
        api_key = config.get("api_key", "sk-default-key-change-me")
        log.info(f"配置已加载: {CONFIG_FILE}")
    else:
        # 创建默认配置
        config = DEFAULT_CONFIG.copy()
        api_key = config["api_key"]
        save_config()
        log.info(f"配置不存在，已创建默认配置: {CONFIG_FILE}")
    
    # 确保目录存在
    DATA_DIR.mkdir(exist_ok=True)
    AUTH_DIR.mkdir(exist_ok=True)
    
    return config


def save_config():
    """保存配置"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ===== 任务引擎辅助 =====

def get_tasks_module():
    """延迟导入 wb_tasks，避免任务模块异常影响网关启动。"""
    global wb_tasks
    if wb_tasks is None:
        try:
            import wb_tasks as _wb_tasks
        except Exception as e:
            return None
        wb_tasks = _wb_tasks
        log.info(f"任务引擎已加载: {wb_tasks.__file__}")
    return wb_tasks


def _gateway_acc_to_task_acc(acc: Account) -> Dict[str, Any]:
    """把网关内部 Account 适配为 wb_tasks 期望的账号字典。"""
    return {
        "token": acc.token or "",
        "uid": acc.id,
        "name": acc.name or acc.id,
        "domain": "",
        "file": "",
        "device_token": "",
    }


def get_task_account_list() -> List[Dict[str, Any]]:
    """任务引擎使用的账号来源：优先 auths/*.json，其次 wb_data/login_state_*.json，最后回落到网关内存池。"""
    m = get_tasks_module()
    if m is None:
        return []
    accounts = m.load_accounts()
    if accounts:
        return accounts
    return [_gateway_acc_to_task_acc(acc) for acc in pool.accounts.values() if acc.token]


def handle_task_action(action: str, request_data: Dict[str, Any]):
    """统一的成长任务/保活动作调度。"""
    m = get_tasks_module()
    if m is None:
        return {"ok": False, "error": f"任务引擎不可用: {log_tasks_import_err}"}

    dry_run = bool(request_data.get("dry_run", False))
    uid = request_data.get("account_id") or request_data.get("uid")
    codes = request_data.get("codes") or None

    if action == "list":
        # 任务目录不需要账号，避免未扫码登录时界面无法展示
        accounts_count = len(get_task_account_list())
        return {"ok": True, "tasks": m.TASKS, "accounts": accounts_count, "auto_codes": m.AUTO_TASK_CODES}

    accounts = get_task_account_list()
    if not accounts:
        return {"ok": False, "error": "没有可用账号（auths/ 为空，且网关池未加载 token）"}

    if uid:
        accounts = [a for a in accounts if str(a.get("uid")) == str(uid) or a.get("name") == uid]
        if not accounts:
            return {"ok": False, "error": "账号不存在", "status": 404}

    # 执行前自动刷新即将过期 token；失败不阻断，具体账号会按原错误反馈。
    if action in ("scan", "run", "keepalive"):
        refresh_auth_tokens(force=False)
        accounts = get_task_account_list()
        if not accounts:
            return {"ok": False, "error": "刷新后仍没有可用账号"}

    if action == "scan":
        return m.scan_accounts(accounts, codes)
    if action == "run":
        return m.run_all_accounts(accounts, codes, dry_run=dry_run, claim=bool(request_data.get("claim", True)))
    if action == "keepalive":
        return m.run_keepalive_all(accounts, dry_run=dry_run)
    return {"ok": False, "error": f"未知动作: {action}", "status": 400}


def log_tasks_import_err() -> str:
    """保留导入错误的访问入口。"""
    return log_tasks_import_err or ""


def get_auth_module():
    """延迟导入 wb_auth，提供 OAuth 设备登录接口。"""
    global wb_auth
    if wb_auth is None:
        try:
            import wb_auth as _wb_auth
        except Exception as e:
            return None
        wb_auth = _wb_auth
        log.info(f"OAuth 登录模块已加载: {wb_auth.__file__}")
    return wb_auth


# ===== 认证加载 =====

def _normalize_auth_file(auth_file: Path) -> Optional[Account]:
    """把 auths/*.json 统一成网关 Account。

    支持：
      1) OAuth/上游嵌套结构：{"auth":{"accessToken":...,"refreshToken":...}, "account":{"uid":...,"nickname":...}}
      2) 扁平结构：{"token":"...","uid":"...","name":"..."}
    """
    with open(auth_file, 'r', encoding='utf-8') as f:
        raw = json.load(f)

    auth = raw.get("auth") or {}
    account = raw.get("account") or {}
    uid = str(account.get("uid") or raw.get("uid") or account.get("id") or "")
    token = auth.get("accessToken") or auth.get("token") or raw.get("token") or auth.get("access_token") or ""
    if not uid or not token:
        raise ValueError("缺少 uid/token（期望 auth.accessToken + account.uid，或扁平 token+uid）")

    # 上游 OAuth 设备登录不写昵称/手机号，从 JWT payload 兜底取（preferred_username 即手机号）
    claims = decode_jwt_payload(token)
    nickname = str(
        account.get("nickname") or account.get("name") or raw.get("name")
        or claims.get("nickname") or claims.get("name") or uid
    )
    phone = mask_phone(str(
        account.get("phone") or raw.get("phone") or claims.get("preferred_username") or ""
    ))
    return Account(
        id=uid,
        name=nickname,
        token=token,
        phone=phone,
        domain=str(auth.get("domain") or claims.get("iss") or ""),
        credits=int(raw.get("credits") or account.get("credits") or 0),
        energy=int(raw.get("energy") or account.get("energy") or 0),
        active=bool(raw.get("active", True)),
    )


def load_auths():
    """加载认证文件"""
    if not AUTH_DIR.exists():
        AUTH_DIR.mkdir(parents=True, exist_ok=True)
        log.warning(f"认证目录已创建: {AUTH_DIR}")

    pool.accounts.clear()
    for auth_file in sorted(AUTH_DIR.glob("*.json")):
        try:
            acc = _normalize_auth_file(auth_file)
            if acc:
                pool.add_account(acc)
                log.info(f"已加载账号: {acc.name} uid={acc.id} file={auth_file.name}")
        except Exception as e:
            log.warning(f"跳过无效认证文件 {auth_file.name}: {e}")

    # 从状态文件加载额外状态；只覆盖内存计数，不移除 auths/ 里的真实账号。
    pool.load(STATE_FILE)


def reload_auths_result() -> Dict[str, Any]:
    """重新从磁盘加载 auths/，供 UI 手动刷新账号池。"""
    load_auths()
    return {
        "ok": True,
        "pool_accounts": len(pool.accounts),
        "auth_files": len(account_auth_summary()),
        "accounts": [
            {"id": a.id, "name": a.name, "phone": a.phone}
            for a in pool.accounts.values()
        ],
    }


def ensure_pool_fresh() -> bool:
    """磁盘账号数与内存池不一致时就地重载。

    覆盖「先启动网关、后扫码登录」和「运行中新增/删除账号」两种场景：
    旧版只在 pool.accounts 为空时才 reload，导致已有 1 个账号的情况下
    新增第 2 个永远进不来。现在按磁盘文件数 vs 内存池大小判断差异。
    """
    if not AUTH_DIR.exists():
        return False
    disk_files = sorted(AUTH_DIR.glob("*.json"))
    disk_count = len(disk_files)
    if disk_count == len(pool.accounts):
        # 数量一致时再做一次「按 uid 对账」：磁盘里有但池里没有的也算新增
        disk_uids = set()
        for f in disk_files:
            try:
                raw = json.loads(f.read_text(encoding="utf-8"))
                uid = str((raw.get("account") or raw).get("uid") or raw.get("uid") or "")
                if uid:
                    disk_uids.add(uid)
            except Exception:
                continue
        if disk_uids and disk_uids.issubset(set(pool.accounts.keys())):
            return False  # 池里都有，真一致
    try:
        load_auths()
        return True
    except Exception as e:
        log.warning("自动重载 auths 失败: %s", e)
    return False


def _latest_credits_per_account() -> Dict[str, Dict[str, Any]]:
    """从 wb_data/credits_history.json 取每个 config_name 的最新 credits/energy。

    账号池内存里的 credits/energy 恒为 0（auths/*.json 没这俩字段），
    这个函数就是「总积分」从 0 变成真实值的唯一数据源。
    注意：积分文件在 wb_data/ 而不是 data/（data/ 是网关自身的 state/usage）。
    """
    out: Dict[str, Dict[str, Any]] = {}
    hist_path = BASE_DIR / "wb_data" / "credits_history.json"
    if not hist_path.exists():
        return out
    try:
        with open(hist_path, "r", encoding="utf-8") as f:
            hist = json.load(f)
    except Exception:
        return out
    accounts_hist = hist.get("accounts") or {}
    for name, info in accounts_hist.items():
        recs = (info or {}).get("records") or []
        valid = [r for r in recs
                 if (r.get("credits") is not None or r.get("energy") is not None)]
        if not valid:
            continue
        last = max(valid, key=lambda r: str(r.get("timestamp") or ""))
        out[name] = {
            "credits": last.get("credits"),
            "energy": last.get("energy"),
            "timestamp": last.get("timestamp"),
        }
    return out


def _enrich_pool_credits() -> None:
    """把 credits_history.json 的最新值回填到内存账号池。

    匹配策略（按可信度递减）：
    1) 手机号匹配：identity_*.json 里的脱敏手机号 ↔ JWT preferred_username 脱敏后
    2) config.ACCOUNTS.name 匹配：auths/*.json 的 nickname 与 config name 相同
    3) 唯一账号兜底：磁盘 1 个账号 + history 1 条记录 → 直接对应
    """
    latest = _latest_credits_per_account()
    if not latest or not pool.accounts:
        return

    # 手机号 → config_name 反向索引（identity 文件在 wb_data/，不是 data/）
    phone_to_name: Dict[str, str] = {}
    try:
        identity_dir = BASE_DIR / "wb_data"
        if identity_dir.exists():
            for f in identity_dir.glob("identity_*.json"):
                try:
                    d = json.loads(f.read_text(encoding="utf-8"))
                    name = f.name[len("identity_"):-len(".json")]
                    if d.get("phone"):
                        phone_to_name[d["phone"]] = name
                except Exception:
                    continue
    except Exception:
        pass

    # config.ACCOUNTS 里的 name 集合（兜底匹配）
    config_names = set()
    try:
        import config as _cfg
        for a in getattr(_cfg, "ACCOUNTS", []) or []:
            config_names.add(str(a.get("name") or ""))
    except Exception:
        pass

    for acc in pool.accounts.values():
        # 1) 手机号匹配
        if acc.phone and acc.phone in phone_to_name:
            match_name = phone_to_name[acc.phone]
            if match_name in latest:
                acc.credits = latest[match_name].get("credits") or 0
                acc.energy = latest[match_name].get("energy") or 0
                continue
        # 2) nickname 匹配 config name
        if acc.name and acc.name in config_names and acc.name in latest:
            acc.credits = latest[acc.name].get("credits") or 0
            acc.energy = latest[acc.name].get("energy") or 0
            continue
        # 3) 唯一兜底
        if len(pool.accounts) == 1 and len(latest) == 1:
            only = next(iter(latest.values()))
            acc.credits = only.get("credits") or 0
            acc.energy = only.get("energy") or 0


# ===== 实时积分余额（billing meter API，30s 缓存） =====
# 上游 /v2/billing/meter/get-user-resource 直接返回当前活跃 + 将过期积分包，
# 累加 remain 就是真实余额——不需要跑采集器。缓存 30s 避免频繁打上游。
import threading as _th
_LIVE_CREDITS_CACHE_TTL = 30
_live_credits_cache = {"data": None, "ts": 0.0, "lock": _th.Lock()}

try:
    import wb_live_credits as _live_mod
except Exception:
    _live_mod = None


def live_credits_cached() -> Dict[str, Any]:
    """实时积分余额（30s 缓存）。返回 {uid: {remain, total, expiring, packages, ...}}。"""
    if _live_mod is None:
        return {}
    now = time.time()
    with _live_credits_cache["lock"]:
        cached = _live_credits_cache["data"]
        ts = _live_credits_cache["ts"]
        if cached is not None and now - ts < _LIVE_CREDITS_CACHE_TTL:
            return cached
        # miss → 查询上游
        data = _live_mod.query_all()
        _live_credits_cache["data"] = data
        _live_credits_cache["ts"] = now
        return data


def live_credits_force_refresh() -> Dict[str, Any]:
    """强制刷新（绕过缓存）。前端点"刷新实时余额"按钮时用。"""
    if _live_mod is None:
        return {}
    data = _live_mod.query_all()
    with _live_credits_cache["lock"]:
        _live_credits_cache["data"] = data
        _live_credits_cache["ts"] = time.time()
    return data



def chat_upstream_url() -> str:
    """聊天补全的真实上游地址。

    优先级：upstream.base_url > global.chat_base > CHAT_BASE 常量。
    旧版默认值是 https://api.workbuddy.cn/v1，这个域名并不存在，请求会直接
    getaddrinfo failed（网关 /v1/chat/completions 恒 500 的根因）。
    上游 cn 域真实路径是 /v2/chat/completions。
    """
    base = (
        str(config.get("upstream", {}).get("base_url") or "").strip()
        or str(config.get("global", {}).get("chat_base") or "").strip()
        or CHAT_BASE
    ).rstrip("/")
    # 历史版本把默认值写成了不存在的 api.workbuddy.cn，老 config.json 里可能还留着
    if "api.workbuddy.cn" in base:
        base = CHAT_BASE
    if base.endswith("/chat/completions") or base.endswith("/v2/chat/completions"):
        return base
    if base.endswith("/v1"):
        base = base[:-3]
    return base + "/v2/chat/completions"


def aggregate_sse_completion(resp, model: str) -> str:
    """把上游 SSE 流聚合成一个 OpenAI 非流式 chat.completion（JSON 字符串）。

    上游实测只接受 stream=true（非流式直接 400 code=11101
    "Non-stream chat request is currently not supported"），所以对上游永远发流式，
    客户端要非流式响应时在这里聚合。
    """
    content_parts: List[str] = []
    tool_calls: Dict[int, Dict[str, Any]] = {}
    finish_reason = ""
    usage = None
    cid = ""
    cmodel = model
    created = int(time.time())

    while True:
        raw = resp.readline()
        if not raw:
            break
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        cid = obj.get("id") or cid
        cmodel = obj.get("model") or cmodel
        created = obj.get("created") or created
        if obj.get("usage"):
            usage = obj["usage"]
        for ch in obj.get("choices") or []:
            delta = ch.get("delta") or {}
            if delta.get("content"):
                content_parts.append(delta["content"])
            for tc in delta.get("tool_calls") or []:
                idx = int(tc.get("index") or 0)
                slot = tool_calls.setdefault(
                    idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["function"]["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if ch.get("finish_reason"):
                finish_reason = ch["finish_reason"]

    message: Dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = [tool_calls[k] for k in sorted(tool_calls)]
    return json.dumps({
        "id": cid or "chatcmpl-local",
        "object": "chat.completion",
        "created": created,
        "model": cmodel,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason or "stop"}],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }, ensure_ascii=False)


def chat_upstream_headers(account: "Account", stream: bool = False) -> Dict[str, str]:
    """构造与上游一致的聊天请求头（缺 X-User-Id/X-Domain 会被上游拒绝）。"""
    h = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "Authorization": "Bearer " + account.token,
        "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
        "Origin": CHAT_BASE,
        "Referer": CHAT_BASE + "/",
        "X-User-Id": account.id,
    }
    if account.domain:
        h["X-Domain"] = account.domain
    return h


def refresh_auth_tokens(force: bool = False) -> Dict[str, Any]:
    """刷新 auths/*.json 中即将过期/已过期的 accessToken。

    force=True 时刷新所有带 refreshToken 的账号；默认只刷新剩余有效期小于 30 天的账号。
    失败不会改坏原文件，只把错误写回结果。
    """
    if not AUTH_DIR.exists():
        return {"ok": True, "accounts": 0, "results": []}

    def headers_for(raw):
        auth = raw.get("auth") or raw
        account = raw.get("account") or {}
        domain = auth.get("domain") or ""
        uid = str(account.get("uid") or raw.get("uid") or "")
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
            "Origin": "https://www.codebuddy.cn",
            "Referer": "https://www.codebuddy.cn/",
        }
        if uid:
            h["X-User-Id"] = uid
        if domain:
            h["X-Domain"] = domain
        return h

    results = []
    updated = 0
    for fp in sorted(AUTH_DIR.glob("*.json")):
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            auth = raw.get("auth") or raw
            rt = auth.get("refreshToken") or auth.get("refresh_token") or ""
            expires_at = int(auth.get("expiresAt") or auth.get("expires_at") or 0)
            expires_in = int(auth.get("expiresIn") or 0)
            if expires_in and not expires_at:
                expires_at = int(time.time()) + expires_in
            need = force or expires_at <= int(time.time()) + 30 * 24 * 3600
            if not rt:
                results.append({"file": fp.name, "ok": False, "error": "没有 refreshToken，无法刷新"})
                continue
            if not need:
                results.append({"file": fp.name, "ok": True, "status": "skip", "expires_at": expires_at})
                continue

            h = headers_for(raw)
            h["Authorization"] = "Bearer " + (auth.get("accessToken") or auth.get("access_token") or "")
            req = Request(CHAT_BASE + "/v2/plugin/auth/token/refresh", data=None, headers=h, method="POST")
            with urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
            # 兼容上游 {data:{accessToken}} 或直接对象
            data = body.get("data", body) if isinstance(body, dict) else {}
            new_at = data.get("accessToken") or data.get("access_token") or ""
            new_rt = data.get("refreshToken") or data.get("refresh_token") or ""
            new_exp = int(data.get("expiresIn") or 0)
            new_domain = data.get("domain") or ""
            if not new_at:
                results.append({"file": fp.name, "ok": False, "error": "refresh 响应没有 accessToken"})
                continue
            if "auth" in raw:
                auth = raw["auth"]
            auth["accessToken"] = new_at
            if new_rt:
                auth["refreshToken"] = new_rt
            if new_domain:
                auth["domain"] = new_domain
            auth["expiresAt"] = int(time.time()) + new_exp if new_exp > 0 else max(expires_at, int(time.time()) + 3600)
            if new_exp > 0:
                auth["expiresIn"] = new_exp
            raw.setdefault("metadata", {})["refreshed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            fp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            updated += 1
            results.append({"file": fp.name, "ok": True, "status": "refreshed", "expires_at": auth.get("expiresAt")})
            log.info("已刷新账号 token: %s", fp.name)
        except Exception as e:
            results.append({"file": fp.name, "ok": False, "error": str(e)})
            log.warning("刷新账号 token 失败 %s: %s", fp.name, e)

    # 刷新后重新加载网关池，避免内存池拿旧 token
    if updated:
        load_auths()
    return {"ok": True, "accounts": len(results), "updated": updated, "results": results}


def decode_jwt_payload(token: str) -> Dict[str, Any]:
    """只解码 JWT 的 payload 段（不校验签名），用于读取昵称/手机号/过期时间。

    返回空 dict 表示不是 JWT 或解码失败——调用方必须容错，不能因此报错。
    """
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        seg = parts[1]
        seg += "=" * (-len(seg) % 4)
        data = base64.urlsafe_b64decode(seg.encode("ascii"))
        payload = json.loads(data.decode("utf-8", "replace"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def mask_phone(phone: str) -> str:
    """13812345678 -> 138****5678，用于展示。"""
    p = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if len(p) != 11:
        return str(phone or "")
    return f"{p[:3]}****{p[-4:]}"


def fmt_ts(ts: Any) -> str:
    """epoch 秒 -> 本地可读时间。"""
    try:
        t = int(ts)
    except Exception:
        return ""
    if t <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t))


def account_auth_summary() -> List[Dict[str, Any]]:
    """供 WebUI/网关状态展示的真实账号摘要，不泄露完整 token。

    phone/nickname 优先取 auths/*.json 里的显式字段；上游 OAuth 设备登录不写
    手机号，因此再回退到 accessToken 的 JWT payload（preferred_username 就是
    手机号，nickname 是昵称）。status 字段是给 UI 用的归一状态，避免界面出现
    undefined。
    """
    if not AUTH_DIR.exists():
        return []
    items = []
    now = int(time.time())
    for fp in sorted(AUTH_DIR.glob("*.json")):
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            auth = raw.get("auth") or raw
            account = raw.get("account") or {}
            uid = str(account.get("uid") or raw.get("uid") or "")
            token = auth.get("accessToken") or auth.get("token") or raw.get("token") or ""
            expires_at = int(auth.get("expiresAt") or auth.get("expires_at") or 0)

            claims = decode_jwt_payload(token)
            if not expires_at and claims.get("exp"):
                try:
                    expires_at = int(claims["exp"])
                except Exception:
                    expires_at = 0

            name = str(
                account.get("nickname") or account.get("name") or raw.get("name")
                or claims.get("nickname") or claims.get("name") or uid or fp.stem
            )
            raw_phone = str(
                account.get("phone") or raw.get("phone")
                or claims.get("preferred_username") or ""
            )
            expired = bool(expires_at and expires_at < now)
            if not token:
                status = "invalid"
            elif expired:
                status = "expired"
            else:
                status = "active"

            item = {
                "file": fp.name,
                "uid": uid,
                "name": name,
                "phone": mask_phone(raw_phone),
                "has_token": bool(token),
                # 兼容旧调用方，但 UI 不再直接显示原始 JWT 片段（看起来像乱码）
                "token_prefix": token[:12] if token else "",
                "token_kind": "JWT" if token.count(".") == 2 else ("OPAQUE" if token else ""),
                "has_refresh_token": bool(auth.get("refreshToken") or auth.get("refresh_token")),
                "realm": raw.get("realm") or auth.get("realm") or "cn",
                "domain": auth.get("domain") or claims.get("iss") or "",
                "login_method": "oauth",
                "status": status,
                "expires_at": expires_at,
                "expires_at_human": fmt_ts(expires_at),
                "expires_in_seconds": max(0, expires_at - now) if expires_at else None,
                "expired": expired,
            }
            items.append(item)
        except Exception as e:
            items.append({"file": fp.name, "error": str(e), "status": "invalid"})
    return items


# ===== API 处理器 =====

class GatewayHandler(BaseHTTPRequestHandler):
    """API 网关处理器"""
    
    def log_message(self, format, *args):
        """自定义日志格式"""
        log.info(f"{self.address_string()} - {args[0]}")
    
    def _check_auth(self) -> bool:
        """检查 API Key"""
        if not api_key:
            return True  # 无 key 则不检查
        
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:] == api_key
        
        return False
    
    def _send_json(self, data: Dict, status: int = 200):
        """发送 JSON 响应"""
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))
    
    def _send_error(self, message: str, status: int = 400, error_type: str = "invalid_request_error"):
        """发送错误响应"""
        self._send_json({
            "error": {
                "message": message,
                "type": error_type,
                "code": status
            }
        }, status)
    
    def do_OPTIONS(self):
        """处理 CORS 预检"""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()
    
    def do_GET(self):
        """处理 GET 请求"""
        import traceback as _tb
        try:
            return self._do_GET_inner()
        except Exception as _e:
            _tb.print_exc()
            log.error(f"do_GET unhandled exception: {_e}\n{_tb.format_exc()}")
            try:
                self._send_error(f"internal server error: {_e}", 500, "internal_server_error")
            except Exception:
                pass
            return

    def _do_GET_inner(self):
        if self.path == "/healthz":
            self._send_json({"status": "ok", "version": "1.0.0"})
        
        elif self.path == "/status":
            if not self._check_auth():
                self._send_error("Unauthorized", 401, "authentication_error")
                return
            
            reloaded = ensure_pool_fresh()
            # 回填 credits/energy：auths/*.json 没这俩字段，账号池内存恒为 0，
            # 这里从 wb_data/credits_history.json 按手机号/config_name 匹配最新值。
            try:
                _enrich_pool_credits()
            except Exception:
                pass
            # 用实时余额覆盖快照——上游 billing meter API 直接返回当前 remain
            try:
                live = live_credits_cached()
            except Exception:
                live = {}
            # 同步到账号池内存，让 accounts[].credits 也变成实时值
            try:
                for acc in pool.accounts.values():
                    uid = getattr(acc, "id", "") or ""
                    if uid and uid in live:
                        lv = live[uid]
                        remain = int(lv.get("remain") or 0)
                        total = int(lv.get("total") or 0)
                        if remain > 0 or total > 0:
                            acc.credits = remain
                            acc.energy = total
            except Exception:
                pass
            status = pool.get_status()
            status["uptime"] = int(time.time() - server_start_time)
            status["version"] = "1.0.0"
            try:
                status["auth_files"] = account_auth_summary()
            except Exception:
                pass
            if reloaded:
                status["auto_reloaded"] = True
            # 实时积分余额（30s 缓存，避免频繁打上游）
            try:
                remain_sum = int(sum(int(v.get("remain") or 0) for v in live.values()))
                total_sum = int(sum(int(v.get("total") or 0) for v in live.values()))
                expiring_sum = int(sum(int(v.get("expiring") or 0) for v in live.values()))
                queried = None
                if live:
                    queried = next(iter(live.values())).get("queried_at")
                status["live_credits"] = {
                    "total_remain": remain_sum,
                    "total_size": total_sum,
                    "expiring_7d": expiring_sum,
                    "accounts": live,
                    "queried_at": queried,
                }
            except Exception as e:
                status["live_credits"] = {"_error": str(e)}
            # 防御性 JSON 编码——packages 数组可能含异常字符
            try:
                payload = json.dumps(status, ensure_ascii=False)
            except Exception as e:
                self._send_error(f"status JSON encode failed: {e}", 500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload.encode("utf-8"))
        
        elif self.path == "/v1/models":
            if not self._check_auth():
                self._send_error("Unauthorized", 401, "authentication_error")
                return

            # 只列上游真实可用的模型（旧版列的是 gpt-4o 等假名字，上游会直接 400）
            created = 1687882411
            models = [{"id": m, "object": "model", "created": created, "owned_by": "workbuddy"}
                      for m in REAL_MODELS]
            self._send_json({"data": models, "object": "list"})
        
        elif self.path in ("/api/pool", "/v1/pool"):
            if not self._check_auth():
                self._send_error("Unauthorized", 401, "authentication_error")
                return
            self._send_json(pool.get_status())

        elif self.path == "/api/auth-files":
            if not self._check_auth():
                self._send_error("Unauthorized", 401, "authentication_error")
                return
            self._send_json({"ok": True, "accounts": account_auth_summary()})

        elif self.path.startswith("/panel/api/login/start"):
            if not self._check_auth():
                self._send_error("Unauthorized", 401, "authentication_error")
                return
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            realm = qs.get("realm", ["cn"])[0]
            m = get_auth_module()
            if m is None:
                self._send_json({"ok": False, "error": "OAuth 登录模块不可用"}, 500)
                return
            result = m.login_start(realm)
            self._send_json(result, 200 if result.get("ok") else 502)

        elif self.path.startswith("/panel/api/login/poll"):
            if not self._check_auth():
                self._send_error("Unauthorized", 401, "authentication_error")
                return
            import urllib.parse
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            state = qs.get("state", [""])[0]
            m = get_auth_module()
            if m is None:
                self._send_json({"ok": False, "error": "OAuth 登录模块不可用"}, 500)
                return
            result = m.login_poll(state)
            # 登录成功后 auths/ 会多出一个文件；必须把磁盘账号重新灌进内存池，
            # 否则「账号池」会一直是 0 个账号（旧版只在进程启动时 load_auths 一次）。
            if result.get("done"):
                try:
                    load_auths()
                    result["pool_accounts"] = len(pool.accounts)
                    result["applied"] = True
                except Exception as e:
                    result["pool_accounts"] = len(pool.accounts)
                    result["applied"] = False
                    result["reload_error"] = str(e)
            status = int(result.get("status") or (200 if result.get("ok") else 500))
            self._send_json(result, status)

        elif self.path == "/api/refresh-tokens":
            if not self._check_auth():
                self._send_error("Unauthorized", 401, "authentication_error")
                return
            self._send_json(refresh_auth_tokens(force=True), 200)

        elif self.path in ("/api/reload-auths", "/panel/api/reload-auths"):
            if not self._check_auth():
                self._send_error("Unauthorized", 401, "authentication_error")
                return
            self._send_json(reload_auths_result(), 200)

        elif self.path.startswith("/tasks"):
            self._handle_tasks_api()

        elif self.path.startswith("/panel/api/"):
            self._handle_panel_api()

        elif self.path.split("?")[0] == "/api/credits/live":
            # 实时积分余额：GET /api/credits/live?refresh=1 强制刷新缓存
            if not self._check_auth():
                self._send_error("Unauthorized", 401, "authentication_error")
                return
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            force = "1" in qs.get("refresh", [])
            try:
                data = live_credits_force_refresh() if force else live_credits_cached()
            except Exception as e:
                self._send_error(f"credits query failed: {e}", 500)
                return
            payload = {
                "total_remain": int(sum(v.get("remain", 0) for v in data.values())),
                "total_size": int(sum(v.get("total", 0) for v in data.values())),
                "expiring_7d": int(sum(v.get("expiring", 0) for v in data.values())),
                "accounts": data,
                "queried_at": next(iter(data.values()), {}).get("queried_at"),
                "forced": force,
            }
            self._send_json(payload)

        else:
            self._send_error("Not Found", 404)
    
    def do_POST(self):
        """处理 POST 请求"""
        if self.path == "/v1/chat/completions":
            if not self._check_auth():
                self._send_error("Unauthorized", 401, "authentication_error")
                return
            
            self._handle_chat_completions()
        
        elif self.path in ("/api/switch", "/v1/switch", "/panel/api/accounts/switch"):
            self._handle_switch_account()

        elif self.path.startswith("/tasks"):
            self._handle_tasks_api()

        elif self.path.startswith("/panel/api/"):
            self._handle_panel_api()

        else:
            self._send_error("Not Found", 404)
    
    def _handle_switch_account(self):
        """WebUI / 面板统一切换账号入口"""
        if not self._check_auth():
            self._send_error("Unauthorized", 401)
            return
        
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            request_data = json.loads(body)
        except json.JSONDecodeError:
            request_data = {}
        
        acc_id = request_data.get("account_id") or request_data.get("id")
        if not acc_id:
            self._send_error("account_id is required", 400)
            return
        
        if pool.switch_account(acc_id):
            pool.save(STATE_FILE)
            self._send_json({"success": True, "account_id": acc_id, "status": pool.get_status()})
        else:
            self._send_error("Account not found", 404)

    def _handle_tasks_api(self):
        """成长任务中心 & 保活 API。"""
        if not self._check_auth():
            self._send_error("Unauthorized", 401)
            return

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            request_data = json.loads(body)
        except json.JSONDecodeError:
            request_data = {}

        if self.path == "/tasks/list" and self.command == "GET":
            request_data.pop("action", None)
            result = handle_task_action("list", request_data)
        else:
            parts = [p for p in self.path.split("/") if p]
            action = parts[1] if len(parts) > 1 else "list"
            result = handle_task_action(action, request_data)

        status = result.get("status") if isinstance(result, dict) else 200
        status = status if isinstance(status, int) else 200
        if status != 200 and "error" in result:
            self._send_error(result["error"], status)
        else:
            self._send_json(result, status)

    def _handle_chat_completions(self):
        """处理聊天补全请求"""
        # 读取请求体
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode('utf-8')
        
        try:
            request_data = json.loads(body)
        except json.JSONDecodeError:
            self._send_error("Invalid JSON", 400)
            return
        
        # 选择账号：优先使用 WebUI 切换后的当前账号
        session_id = request_data.get("user", str(uuid.uuid4()))
        if pool.current_account_id and pool.current_account_id in pool.accounts:
            account = pool.accounts[pool.current_account_id]
        else:
            account = pool.pick(session_id)
        
        if not account or not account.token:
            self._send_error("No available accounts", 503)
            return
        
        # 转发到上游（cn 域真实路径是 /v2/chat/completions，旧版拼到了不存在的域名）
        upstream_url = chat_upstream_url()
        stream = bool(request_data.get("stream"))
        headers_sent = False

        # 上游只接受流式请求（非流式实测直接 400 code=11101），所以对上游永远发
        # stream=true；客户端要非流式时用 aggregate_sse_completion 把 SSE 聚合成
        # 普通 completion 返回。
        up_body = dict(request_data)
        up_body["stream"] = True

        # 模型名兜底：客户端写死的 gpt-4o / deepseek-chat 等上游不认，替换成真实模型。
        want_model = str(request_data.get("model") or "").strip()
        if want_model in REAL_MODELS:
            up_body["model"] = want_model
        else:
            up_body["model"] = DEFAULT_CHAT_MODEL
            if want_model:
                log.info("模型 %s 上游不支持，改用 %s", want_model, DEFAULT_CHAT_MODEL)

        try:
            req = Request(
                upstream_url,
                data=json.dumps(up_body, ensure_ascii=False).encode('utf-8'),
                headers=chat_upstream_headers(account, stream=True),
                method="POST"
            )

            timeout = config.get("upstream", {}).get("timeout_seconds", 120)
            with urlopen(req, timeout=timeout) as resp:
                if stream:
                    # SSE 透传：边收边发，否则客户端等不到首包会超时
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    headers_sent = True
                    while True:
                        chunk = resp.read(1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                else:
                    response_data = aggregate_sse_completion(resp, request_data.get("model") or "")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    headers_sent = True
                    self.wfile.write(response_data.encode('utf-8'))
                pool.record_success(account.id)

        except HTTPError as e:
            error_body = e.read().decode('utf-8') if e.fp else ""
            log.error(f"上游返回错误 {e.code}: {error_body[:200]}")

            # 记录失败
            is_rate_limit = e.code == 429
            pool.record_failure(account.id, is_rate_limit)

            if headers_sent:
                log.warning("响应头已发出，无法再回传上游错误")
                return
            self._send_json({
                "error": {
                    "message": f"Upstream error: {e.code}",
                    "type": "api_error",
                    "code": e.code
                }
            }, e.code)

        except Exception as e:
            log.error(f"请求失败: {e}")
            pool.record_failure(account.id)
            if headers_sent:
                return
            self._send_error(f"Request failed: {str(e)}", 500)
    
    def _handle_panel_api(self):
        """处理面板 API 请求"""
        if not self._check_auth():
            self._send_error("Unauthorized", 401)
            return
        
        # 读取请求体（如果有）
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode('utf-8') if content_length > 0 else '{}'
        
        try:
            request_data = json.loads(body)
        except json.JSONDecodeError:
            request_data = {}
        
        # 路由分发
        if "/panel/api/accounts" in self.path:
            if self.command == "GET":
                self._send_json(pool.get_status())
            elif self.command == "POST" and "switch" in self.path:
                acc_id = request_data.get("account_id")
                if pool.switch_account(acc_id):
                    pool.save(STATE_FILE)
                    self._send_json({"success": True, "account_id": acc_id})
                else:
                    self._send_error("Account not found", 404)
            elif self.command == "POST" and "add" in self.path:
                # 添加账号
                acc = Account(
                    id=str(uuid.uuid4()),
                    name=request_data.get("name", "unknown"),
                    token=request_data.get("token", ""),
                    phone=request_data.get("phone", ""),
                    active=True
                )
                pool.add_account(acc)
                pool.save(STATE_FILE)
                self._send_json({"success": True, "account": asdict(acc)})
            elif self.command == "POST" and "remove" in self.path:
                acc_id = request_data.get("account_id")
                pool.remove_account(acc_id)
                pool.save(STATE_FILE)
                self._send_json({"success": True})
            else:
                self._send_error("Not Found", 404)
        
        elif "/panel/api/config" in self.path:
            if self.command == "GET":
                # 返回配置（隐藏敏感信息）
                safe_config = {k: v for k, v in config.items() if k != "api_key"}
                safe_config["api_key"] = "***" + api_key[-4:] if api_key else ""
                self._send_json(safe_config)
            elif self.command == "POST":
                # 更新配置
                config.update(request_data)
                save_config()
                self._send_json({"success": True})
        
        elif "/panel/api/stats" in self.path:
            # 返回统计信息
            self._send_json({
                "uptime": int(time.time() - server_start_time),
                "accounts": len(pool.accounts),
                "requests": sum(acc.request_count for acc in pool.accounts.values()),
                "success": sum(acc.success_count for acc in pool.accounts.values()),
                "failures": sum(acc.fail_count for acc in pool.accounts.values())
            })
        
        else:
            self._send_error("Not Found", 404)


# ===== Web 面板 =====

PANEL_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WorkBuddy 网关面板</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1419; color: #e8eaed; padding: 20px; }
.container { max-width: 1200px; margin: 0 auto; }
.header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; padding: 20px; background: #1a1f2e; border-radius: 12px; }
.title { font-size: 24px; font-weight: bold; }
.subtitle { color: #8b9dc3; font-size: 14px; margin-top: 4px; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 16px; margin-bottom: 20px; }
.card { background: #252b3d; border-radius: 10px; padding: 20px; }
.card-title { font-size: 13px; color: #8b9dc3; margin-bottom: 8px; text-transform: uppercase; }
.card-value { font-size: 28px; font-weight: bold; }
.btn { padding: 10px 20px; border: none; border-radius: 8px; cursor: pointer; font-size: 14px; transition: all 0.2s; }
.btn-primary { background: #4f46e5; color: white; }
.btn-primary:hover { background: #4338ca; }
.btn-sm { padding: 6px 12px; font-size: 12px; }
table { width: 100%; border-collapse: collapse; }
th, td { padding: 12px 16px; text-align: left; border-bottom: 1px solid #2d3447; }
th { color: #8b9dc3; font-size: 12px; text-transform: uppercase; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }
.badge-success { background: rgba(52, 211, 153, 0.15); color: #34d399; }
.badge-warning { background: rgba(251, 191, 36, 0.15); color: #fbbf24; }
.badge-danger { background: rgba(239, 68, 68, 0.15); color: #ef4444; }
.status-bar { display: flex; gap: 15px; }
.status-item { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; }
.dot-green { background: #34d399; box-shadow: 0 0 8px #34d399; }
.dot-red { background: #ef4444; }
.tabs { display: flex; gap: 4px; margin-bottom: 20px; background: #1a1f2e; padding: 6px; border-radius: 10px; }
.tab { padding: 12px 20px; border-radius: 8px; cursor: pointer; font-size: 14px; }
.tab.active { background: #4f46e5; color: white; }
.panel { display: none; background: #1a1f2e; border-radius: 12px; padding: 24px; }
.panel.active { display: block; }
</style>
</head>
<body>
<div class="container">
    <div class="header">
        <div>
            <div class="title">WorkBuddy 网关面板</div>
            <div class="subtitle">v1.0.0 - Python 版</div>
        </div>
        <div class="status-bar">
            <div class="status-item"><div class="status-dot dot-green"></div><span>运行中</span></div>
        </div>
    </div>
    
    <div class="tabs">
        <div class="tab active" onclick="switchTab('dashboard', event)">📊 仪表盘</div>
        <div class="tab" onclick="switchTab('accounts', event)">👤 账号池</div>
        <div class="tab" onclick="switchTab('config', event)">⚙️ 配置</div>
        <div class="tab" onclick="switchTab('stats', event)">📈 统计</div>
    </div>
    
    <div class="panel active" id="panel-dashboard">
        <div class="grid">
            <div class="card">
                <div class="card-title">账号总数</div>
                <div class="card-value" id="stat-total">-</div>
            </div>
            <div class="card">
                <div class="card-title">可用账号</div>
                <div class="card-value" id="stat-available">-</div>
            </div>
            <div class="card">
                <div class="card-title">总请求数</div>
                <div class="card-value" id="stat-requests">-</div>
            </div>
            <div class="card">
                <div class="card-title">成功率</div>
                <div class="card-value" id="stat-success">-</div>
            </div>
        </div>
    </div>
    
    <div class="panel" id="panel-accounts">
        <h3 style="margin-bottom: 16px;">账号列表</h3>
        <table>
            <thead>
                <tr><th>名称</th><th>手机号</th><th>积分</th><th>能量</th><th>状态</th><th>请求数</th><th>操作</th></tr>
            </thead>
            <tbody id="accounts-table"></tbody>
        </table>
    </div>
    
    <div class="panel" id="panel-config">
        <h3 style="margin-bottom: 16px;">配置</h3>
        <pre id="config-display" style="background: #0f1419; padding: 16px; border-radius: 8px; overflow-x: auto;"></pre>
    </div>
    
    <div class="panel" id="panel-stats">
        <h3 style="margin-bottom: 16px;">运行统计</h3>
        <div class="grid">
            <div class="card">
                <div class="card-title">运行时间</div>
                <div class="card-value" id="stat-uptime">-</div>
            </div>
            <div class="card">
                <div class="card-title">成功请求</div>
                <div class="card-value" id="stat-success-count">-</div>
            </div>
            <div class="card">
                <div class="card-title">失败请求</div>
                <div class="card-value" id="stat-fail-count">-</div>
            </div>
        </div>
    </div>
</div>

<script>
// API Key 优先从 URL hash 读取（#key=xxx）：hash 不发送给服务器、也不进访问日志，
// 这样从统一管理器 iframe 嵌入时无需手动输入。直接打开本页时才回退到手动输入。
function readApiKeyFromHash() {
    try {
        const m = new URLSearchParams(String(location.hash || '').replace(/^#/, '')).get('key');
        if (m) return m.trim();
    } catch (e) { /* 忽略解析异常 */ }
    return '';
}
let apiKey = readApiKeyFromHash();
if (!apiKey) {
    try { apiKey = (prompt('请输入 API Key:') || '').trim(); } catch (e) { apiKey = ''; }
}
const headers = {'Authorization': 'Bearer ' + apiKey, 'Content-Type': 'application/json'};

// 无 Key / Key 错误时给出明确指引，避免只看到一片 401
function apiHint() {
    return apiKey ? '' : '（未提供 API Key：请从统一管理器「📈 实时面板」进入，或在网址末尾加 #key=你的密钥）';
}

// 统一的取数封装：HTTP 非 2xx 直接抛出带状态码的错误，调用方据此提示
async function apiGet(url) {
    const resp = await fetch(url, { headers: headers });
    if (!resp.ok) {
        let detail = '';
        try { detail = (await resp.text()).slice(0, 200); } catch (e) { /* ignore */ }
        throw new Error('HTTP ' + resp.status + (detail ? ' ' + detail : '') + apiHint());
    }
    return resp.json();
}

function switchTab(name, event) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    if (event && event.currentTarget) event.currentTarget.classList.add('active');
    document.getElementById('panel-' + name).classList.add('active');
    
    if (name === 'accounts') loadAccounts();
    if (name === 'config') loadConfig();
    if (name === 'stats') loadStats();
}

async function refreshDashboard() {
    try {
        const data = await apiGet('/status');
        const accounts = Array.isArray(data.accounts) ? data.accounts : [];
        document.getElementById('stat-total').textContent =
            (data.total !== undefined) ? data.total : accounts.length;
        document.getElementById('stat-available').textContent =
            (data.available !== undefined) ? data.available : accounts.filter(a => a.available).length;
        const total = accounts.reduce((sum, a) => sum + (a.requests || 0), 0);
        const success = accounts.reduce((sum, a) => sum + (a.success || 0), 0);
        document.getElementById('stat-requests').textContent = total;
        document.getElementById('stat-success').textContent = total > 0 ? Math.round(success/total*100) + '%' : '0%';
    } catch (e) {
        console.error('刷新失败:', e.message || e);
    }
}

async function loadAccounts() {
    try {
        const data = await apiGet('/status');
        const accounts = Array.isArray(data.accounts) ? data.accounts : [];
        const tbody = document.getElementById('accounts-table');
        if (!accounts.length) {
            tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:#94a3b8;">'
                + '网关池暂无账号。请到统一管理器「添加/登录账号」完成一次登录，或等待采集写入 auths/。</td></tr>';
            return;
        }
        tbody.innerHTML = accounts.map(a => `
            <tr>
                <td>${a.name || a.id || '-'}</td>
                <td>${a.phone || '-'}</td>
                <td>${a.credits !== undefined ? a.credits : '-'}</td>
                <td>${a.energy !== undefined ? a.energy : '-'}</td>
                <td><span class="badge ${a.available ? 'badge-success' : 'badge-warning'}">${a.available ? '可用' : '不可用'}</span></td>
                <td>${a.requests || 0}</td>
                <td><button class="btn btn-sm btn-primary" onclick="switchAccount('${a.id}')">切换</button></td>
            </tr>
        `).join('');
    } catch (e) {
        console.error('加载账号失败:', e.message || e);
    }
}

async function switchAccount(id) {
    try {
        const resp = await fetch('/panel/api/accounts/switch', {
            method: 'POST',
            headers: headers,
            body: JSON.stringify({ account_id: id })
        });
        if (!resp.ok) {
            let detail = '';
            try { detail = (await resp.text()).slice(0, 200); } catch (e) { /* ignore */ }
            throw new Error('HTTP ' + resp.status + (detail ? ' ' + detail : '') + apiHint());
        }
        const result = await resp.json();
        alert('切换成功: ' + id);
    } catch (e) {
        alert('切换失败: ' + (e.message || e));
    }
}

async function loadConfig() {
    try {
        const data = await apiGet('/panel/api/config');
        document.getElementById('config-display').textContent = JSON.stringify(data, null, 2);
    } catch (e) {
        console.error('加载配置失败:', e.message || e);
        const box = document.getElementById('config-display');
        if (box) box.textContent = '加载配置失败：' + (e.message || e);
    }
}

async function loadStats() {
    try {
        const data = await apiGet('/panel/api/stats');
        document.getElementById('stat-uptime').textContent = formatUptime(data.uptime || 0);
        document.getElementById('stat-success-count').textContent = data.success || 0;
        document.getElementById('stat-fail-count').textContent = data.failures || 0;
    } catch (e) {
        console.error('加载统计失败:', e.message || e);
    }
}

function formatUptime(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    return `${h}h ${m}m ${s}s`;
}

// 启动
refreshDashboard();
setInterval(refreshDashboard, 10000);
</script>
</body>
</html>"""


def serve_panel(handler):
    """提供 Web 面板"""
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.end_headers()
    handler.wfile.write(PANEL_HTML.encode('utf-8'))


# 重写 GatewayHandler 的 GET 方法以支持面板
_original_do_get = GatewayHandler.do_GET


def patched_do_get(self):
    if self.path == "/panel/" or self.path == "/panel":
        serve_panel(self)
    else:
        _original_do_get(self)


GatewayHandler.do_GET = patched_do_get


# ===== 主函数 =====

def main():
    """主函数"""
    global config, api_key
    
    print("""
╔═══════════════════════════════════════════════════════════════╗
║          WorkBuddy API 网关 v1.0.0 (Python 版)                ║
║                                                               ║
║  • API 端点: http://127.0.0.1:7863/v1/chat/completions        ║
║  • 模型列表: http://127.0.0.1:7863/v1/models                   ║
║  • 状态检查: http://127.0.0.1:7863/status                      ║
║  • 管理面板: http://127.0.0.1:7863/panel/                       ║
║                                                               ║
║  按 Ctrl+C 停止服务                                            ║
╚═══════════════════════════════════════════════════════════════╝
    """)
    
    # 加载配置
    load_config()
    
    # 加载认证
    load_auths()
    
    # 解析监听地址
    listen = config.get("listen", ":7863")
    if listen.startswith(":"):
        listen = "127.0.0.1" + listen
    
    host, port = listen.rsplit(":", 1)
    port = int(port)
    
    # 启动服务器
    server = ThreadingHTTPServer((host, port), GatewayHandler)
    server.daemon_threads = True
    
    log.info(f"网关启动: http://{host}:{port}")
    log.info(f"API Key: {api_key[:20]}...")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[停止] 正在关闭服务...")
        pool.save(STATE_FILE)
        server.shutdown()
        print("[完成] 服务已停止")


if __name__ == "__main__":
    main()