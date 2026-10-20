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
from urllib.parse import urlparse, parse_qs, quote
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from dataclasses import dataclass, field, asdict

# ===== 配置 =====
# Windows 控制台默认是 GBK（代码页 936），编不出启动横幅里的 '•' 这类字符 ——
# 不兜住的话 print_banner 会在启动第一步抛 UnicodeEncodeError，网关直接起不来。
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

BASE_DIR = Path(__file__).parent.parent  # 仓库根（本文件在 core/ 下）

#: config.py 现在在 configs/ 下，按这个路径加载（不能只靠 sys.path，免得用户环境里
#: 有个同名的 config 模块被误导入 —— 那是很多「配置不生效」类问题的根源）。
CONFIG_PY = Path(__file__).parent.parent / "configs" / "config.py"


def _load_user_config():
    """加载 configs/config.py 作为模块返回；不存在或出错时返回 None（历史行为：静默兜底）。"""
    import importlib.util
    try:
        if not CONFIG_PY.exists():
            return None
        spec = importlib.util.spec_from_file_location("wb_user_config", CONFIG_PY)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None

DATA_DIR = BASE_DIR / "data"
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = DATA_DIR / "state.json"
USAGE_FILE = DATA_DIR / "usage.json"
AUTH_DIR = BASE_DIR / "auths"
CHAT_BASE = "https://copilot.tencent.com"
#: 国际版（凭据域名是 www.workbuddy.ai）的聊天上游。把国际版 token 打到国内域名上
#: 会被 openresty 直接回 401 Authorization Required —— 这是「DSH 里必失败」的根因。
GLOBAL_CHAT_BASE = "https://www.workbuddy.ai"
GLOBAL_ORIGIN = "https://www.workbuddy.ai"
#: 一次请求最多换几个账号（401/403 视为凭据不可用，换号重试）。
MAX_ACCOUNT_TRIES = 3
#: 鉴权失败后给该账号的冷却秒数：避免它继续被选中把整池请求打死。
AUTH_FAIL_COOLDOWN = 600.0
#: 上游 code=11128「request illegal / 请求被拦截，请重新发送」是反探测拦截，属瞬时失败
#: （上游自己的文案就要求「重新发送」）。旧版把它当普通 400 原样回给客户端，DSH 这类客户端
#: 不会自动重试，用户就看到一次假失败。这里：先原地重发，再给短冷却换号。
ANTIPROBE_RETRY_DELAY = 0.8
ANTIPROBE_MAX_RETRIES = 2
ANTIPROBE_FAIL_COOLDOWN = 60.0

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
        "distribution": "priority",
        "region": "",
        "breaker_threshold": 3,
        "breaker_cooldown": "30m",
        "prefer_expiring": True
    },
    "models": {
        "enabled": True,
        "file": "./data/models.json"
    },
    "usage": {
        "enabled": True,
        "file": "./data/usage.json",
        "retention_days": 90,
        "flush_interval_seconds": 5
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

# ===== Token 统计模块（可选：缺失时网关照常工作） =====
try:
    import wb_usage
    log_usage_import_err = None
except Exception as _usage_import_exc:  # pragma: no cover - 仅模块缺失/损坏时触发
    wb_usage = None
    log_usage_import_err = _usage_import_exc
    log.warning("wb_usage 导入失败，Token 统计不可用: %s", _usage_import_exc)

# ===== 模型目录模块（可选：缺失时 /v1/models 退回内置列表） =====
try:
    import wb_models
    log_models_import_err = None
except Exception as _models_import_exc:  # pragma: no cover - 仅模块缺失/损坏时触发
    wb_models = None
    log_models_import_err = _models_import_exc
    log.warning("wb_models 导入失败，/v1/models 退回内置列表: %s", _models_import_exc)

# ===== 账号池分配策略模块（可选：缺失时退回老式轮换） =====
try:
    import wb_pool_policy
    log_policy_import_err = None
except Exception as _policy_import_exc:  # pragma: no cover - 仅模块缺失/损坏时触发
    wb_pool_policy = None
    log_policy_import_err = _policy_import_exc
    log.warning("wb_pool_policy 导入失败，账号池退回老式轮换: %s", _policy_import_exc)

# ===== 每日签到 / 收益账本模块（可选：缺失时面板签到位不可用） =====
try:
    import wb_checkin
    log_checkin_import_err = None
except Exception as _checkin_import_exc:  # pragma: no cover - 仅模块缺失/损坏时触发
    wb_checkin = None
    log_checkin_import_err = _checkin_import_exc
    log.warning("wb_checkin 导入失败，签到与收益账本不可用: %s", _checkin_import_exc)

# ===== 桌面快照发现模块（可选：缺失时快照发现位不可用） =====
try:
    import wb_snapshot
    log_snapshot_import_err = None
except Exception as _snapshot_import_exc:  # pragma: no cover - 仅模块缺失/损坏时触发
    wb_snapshot = None
    log_snapshot_import_err = _snapshot_import_exc
    log.warning("wb_snapshot 导入失败，桌面快照发现不可用: %s", _snapshot_import_exc)

# ===== 公共运维模块（采集 / 自检；缺失时面板的采集与诊断位不可用） =====
try:
    import wb_ops
    log_ops_import_err = None
except Exception as _ops_import_exc:  # pragma: no cover - 仅模块缺失/损坏时触发
    wb_ops = None
    log_ops_import_err = _ops_import_exc
    log.warning("wb_ops 导入失败，采集与诊断不可用: %s", _ops_import_exc)

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
    session_ttl: Dict[str, float] = field(default_factory=dict)  # session_id -> 上次绑定时间
    cursors: Dict[str, int] = field(default_factory=dict)        # 'policy|region' -> 轮换游标

    # ---- 分配策略参数（由 config.json 的 pool / cooldown 节注入）----
    distribution: str = "priority"      # priority | round-robin | balanced | sticky
    region: str = ""                    # "" / merged = 跨区混池
    credit_floor: float = 0.0
    prefer_expiring: bool = False
    expiring_soon: float = 0.0
    idle_weight_per_hour: float = 0.5
    idle_weight_max: float = 5.0
    breaker_threshold: int = 3
    breaker_cooldown: float = 1800.0
    rate_cooldown: float = 600.0
    last_pick_policy: str = ""
    last_pick_reason: str = ""

    # ---- 与策略模块的状态桥接 ----
    def _policy_state(self) -> Any:
        if wb_pool_policy is None:
            return None
        return wb_pool_policy.PolicyState(
            cursors=dict(self.cursors),
            sticky=dict(self.sticky_sessions),
            sticky_at=dict(self.session_ttl),
        )

    def _apply_policy_state(self, state: Any) -> None:
        if state is None:
            return
        self.cursors = dict(getattr(state, "cursors", {}) or {})
        self.sticky_sessions = dict(getattr(state, "sticky", {}) or {})
        self.session_ttl = dict(getattr(state, "sticky_at", {}) or {})

    def _pick_legacy(self, session_id: Optional[str] = None) -> Optional[Account]:
        """wb_pool_policy 缺失时的老逻辑（最久未用 + 余额最高）。"""
        if session_id and session_id in self.sticky_sessions:
            acc_id = self.sticky_sessions[session_id]
            if acc_id in self.accounts:
                acc = self.accounts[acc_id]
                if self._is_available(acc):
                    return acc

        available = [acc for acc in self.accounts.values() if self._is_available(acc) and acc.active]
        if not available:
            # 一个可用的都没有就必须说「没有」，不能硬塞一个正在熔断/冷却的号：那种请求
            # 必然失败，还会把 fail_count 越堆越高、冷却越拖越长，调用方也拿不到
            # 「池子有问题」这个真实原因（pick() 的契约是一致返回 None）。
            return None

        available.sort(key=lambda a: (a.last_used, -a.credits))
        chosen = available[0]

        if session_id:
            self.sticky_sessions[session_id] = chosen.id
            self.session_ttl[session_id] = time.time() + 1800

        return chosen

    def pick(self, session_id: Optional[str] = None) -> Optional[Account]:
        """按 distribution 策略挑账号。

        池里一个可用的都没有时返回 None（调用方回 503 并说明原因），
        而不是硬塞一个正在熔断/冷却的账号上去 —— 那种请求必然失败，
        还会把 fail_count 越堆越高、把冷却时间越拖越长。
        """
        if wb_pool_policy is None:
            self.last_pick_policy = "legacy"
            self.last_pick_reason = "fallback"
            return self._pick_legacy(session_id)

        state = self._policy_state()
        selection = wb_pool_policy.select(
            list(self.accounts.values()),
            self.distribution,
            session_id=session_id,
            state=state,
            region=self.region or None,
            credit_floor=self.credit_floor,
            prefer_expiring=self.prefer_expiring,
            expiring_soon=self.expiring_soon,
            idle_weight_per_hour=self.idle_weight_per_hour,
            idle_weight_max=self.idle_weight_max,
            current_account_id=self.current_account_id,
        )
        self._apply_policy_state(state)
        self.last_pick_policy = selection.policy
        self.last_pick_reason = selection.reason
        return selection.account

    def available_accounts(self) -> List[Account]:
        """当前真正可用的账号。"""
        return [acc for acc in self.accounts.values() if self._is_available(acc)]

    def unavailable_summary(self) -> List[Dict[str, Any]]:
        """不可用账号及原因 —— 503 文案与面板都要它。"""
        if wb_pool_policy is None:
            return []
        return [
            {"id": acc.id, "name": acc.name, "reason": wb_pool_policy.unavailable_reason(acc)}
            for acc in self.accounts.values() if not self._is_available(acc)
        ]

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
        """记录失败请求。

        阈值与冷却时长走 config.json（pool.breaker_threshold / breaker_cooldown、
        cooldown.soft_rate），不再硬编码。上游是按「行」限流的，一个号被限流
        不代表其他号不行，所以限流冷却要短而准。
        """
        if acc_id in self.accounts:
            acc = self.accounts[acc_id]
            acc.request_count += 1
            acc.fail_count += 1
            acc.last_used = time.time()

            if is_rate_limit and self.rate_cooldown > 0:
                acc.cooldown_until = time.time() + self.rate_cooldown
                log.warning("账号 %s 被限流，冷却 %.1f 分钟", acc.name, self.rate_cooldown / 60.0)

            if acc.fail_count >= max(1, int(self.breaker_threshold)):
                acc.breaker_until = time.time() + max(0.0, self.breaker_cooldown)
                log.warning("账号 %s 连续失败 %d 次，熔断 %.1f 分钟",
                            acc.name, acc.fail_count, self.breaker_cooldown / 60.0)

    def add_account(self, acc: Account):
        """添加账号"""
        self.accounts[acc.id] = acc

    def remove_account(self, acc_id: str):
        """移除账号"""
        if acc_id in self.accounts:
            del self.accounts[acc_id]
            if self.current_account_id == acc_id:
                self.current_account_id = None
            # 清掉指向它的粘性绑定，否则那些 session 会一直挑不到号
            if wb_pool_policy is not None:
                state = self._policy_state()
                dropped = state.drop_account(acc_id)
                self._apply_policy_state(state)
                if dropped:
                    log.info("已清理 %d 条指向已移除账号的粘性会话", dropped)

    def switch_account(self, acc_id: Optional[str] = None) -> Optional[str]:
        """切换当前账号。

        - 传入 acc_id：强制切到该账号（若存在）
        - acc_id=None/空：按当前 distribution 策略挑一个可用账号（等价于"换号"）
        - 切换成功一律清空粘性与轮换游标，避免旧粘性把新账号绑死

        返回：切换后的账号 id（成功），None（池空或指定账号不存在）
        """
        target_id: Optional[str] = None
        if acc_id and acc_id in self.accounts:
            target_id = acc_id
        else:
            # 排除 current 本身；若无 current 或 current 已不可用，则任意可用即可
            candidates = [
                a for a in self.accounts.values()
                if self._is_available(a) and a.active and a.id != self.current_account_id
            ]
            if not candidates:
                candidates = [
                    a for a in self.accounts.values()
                    if self._is_available(a) and a.active
                ]
            if candidates:
                if wb_pool_policy is not None:
                    state = self._policy_state()
                    selection = wb_pool_policy.select(
                        candidates, self.distribution, state=state,
                        region=self.region or None, credit_floor=self.credit_floor,
                        prefer_expiring=self.prefer_expiring, expiring_soon=self.expiring_soon,
                        idle_weight_per_hour=self.idle_weight_per_hour,
                        idle_weight_max=self.idle_weight_max,
                    )
                    self._apply_policy_state(state)
                    target_id = selection.account_id or None
                if not target_id:
                    candidates.sort(key=lambda a: (a.last_used, -a.credits))
                    target_id = candidates[0].id

        if not target_id:
            return None

        self.current_account_id = target_id
        # 关键：清掉粘性与游标，否则旧 session 会继续被粘到旧账号，切换形同虚设
        self.sticky_sessions.clear()
        self.session_ttl.clear()
        self.cursors.clear()
        return target_id

    def get_status(self) -> Dict:
        """获取状态"""
        now = time.time()
        return {
            "total": len(self.accounts),
            "active": sum(1 for a in self.accounts.values() if a.active),
            "available": sum(1 for a in self.accounts.values() if self._is_available(a)),
            "current": self.current_account_id,
            "policy": {
                "distribution": self.distribution,
                "label": (wb_pool_policy.POLICY_LABELS.get(self.distribution, self.distribution)
                          if wb_pool_policy is not None else self.distribution),
                "region": self.region or "merged",
                "credit_floor": self.credit_floor,
                "last_pick_policy": self.last_pick_policy,
                "last_pick_reason": self.last_pick_reason,
                "sticky_sessions": len(self.sticky_sessions),
                "cursors": dict(self.cursors),
            },
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
                    # 区域与不可用原因直接算好给面板：面板不该再去猜「为什么这个号不派活」。
                    "region": _usage_region_of(acc),
                    "reason": (
                        wb_pool_policy.unavailable_reason(acc, now)
                        if (wb_pool_policy is not None and not self._is_available(acc)) else ""
                    ),
                }
                for acc in self.accounts.values()
            ]
        }
    
    def save(self, path: Path):
        """保存状态"""
        data = {
            "current_account_id": self.current_account_id,
            "sticky_sessions": self.sticky_sessions,
            "session_ttl": self.session_ttl,
            "cursors": self.cursors,
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
        self.session_ttl = data.get("session_ttl", {}) or {}
        self.cursors = data.get("cursors", {}) or {}
        
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

    # Token 统计：按配置初始化用量库（模块缺失时静默跳过）
    if wb_usage is not None:
        usage_conf = config.get("usage") or {}
        if usage_conf.get("enabled", True):
            try:
                raw_path = str(usage_conf.get("file") or "").strip()
                usage_path = None
                if raw_path:
                    candidate = Path(raw_path)
                    usage_path = str(candidate if candidate.is_absolute() else (BASE_DIR / candidate))
                wb_usage.configure(
                    path=usage_path,
                    retention_days=usage_conf.get("retention_days", 90),
                    logger=log,
                    flush_interval=usage_conf.get("flush_interval_seconds", 5),
                )
                log.info("Token 统计已就绪: %s", wb_usage.get_store().path)
            except Exception as exc:
                log.warning("Token 统计初始化失败: %s", exc)

    # ===== 模型目录：按配置初始化（模块缺失时 /v1/models 退回内置列表）=====
    if wb_models is not None:
        try:
            models_conf = config.get("models") or {}
            raw_models_path = str(models_conf.get("file") or "").strip()
            models_path = None
            if raw_models_path:
                candidate = Path(raw_models_path)
                models_path = str(candidate if candidate.is_absolute() else (BASE_DIR / candidate))
            wb_models.configure(path=models_path, logger=log)
            log.info("模型目录已就绪: %s", wb_models.get_prefs().path)
        except Exception as exc:
            log.warning("模型目录初始化失败: %s", exc)

    # ===== 账号池分配策略：从 config.json 的 pool / cooldown 节注入 =====
    pool_conf = config.get("pool") or {}
    cooldown_conf = config.get("cooldown") or {}
    if wb_pool_policy is not None:
        pool.distribution = wb_pool_policy.normalize_policy(pool_conf.get("distribution"))
    else:
        pool.distribution = str(pool_conf.get("distribution") or "priority")
    pool.region = str(pool_conf.get("region") or "")
    try:
        pool.credit_floor = float(pool_conf.get("credit_floor") or 0)
    except (TypeError, ValueError):
        pool.credit_floor = 0.0
    pool.prefer_expiring = bool(pool_conf.get("prefer_expiring", False))
    pool.expiring_soon = _duration_seconds(pool_conf.get("expiring_soon"), 0.0)
    try:
        pool.idle_weight_per_hour = float(pool_conf.get("idle_weight_per_hour") or 0.5)
    except (TypeError, ValueError):
        pool.idle_weight_per_hour = 0.5
    try:
        pool.idle_weight_max = float(pool_conf.get("idle_weight_max") or 5.0)
    except (TypeError, ValueError):
        pool.idle_weight_max = 5.0
    try:
        pool.breaker_threshold = int(pool_conf.get("breaker_threshold") or 3)
    except (TypeError, ValueError):
        pool.breaker_threshold = 3
    pool.breaker_cooldown = _duration_seconds(pool_conf.get("breaker_cooldown"), 1800.0)
    pool.rate_cooldown = _duration_seconds(cooldown_conf.get("soft_rate"), 600.0)
    log.info("账号池策略: %s 区域=%s 积分下限=%.0f",
             pool.distribution, pool.region or "merged", pool.credit_floor)
    
    return config


def save_config():
    """保存配置"""
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ===== Token 统计辅助 =====

def _usage_region_of(account: Any) -> str:
    """按凭据域名判断区域：workbuddy.ai 是国际版，其余（copilot.tencent.com 等）是国内版。"""
    domain = str(getattr(account, "domain", "") or "").lower()
    return "global" if "workbuddy.ai" in domain else "cn"


def record_chat_usage(account, request_data, usage, success, duration_ms, headers, model_hint="") -> None:
    """把一次对话请求的用量落进 wb_usage。

    统计属于旁路：这里任何异常都不允许影响正常的转发/回包流程。
    """
    if wb_usage is None:
        return
    try:
        user_agent = ""
        try:
            user_agent = headers.get("User-Agent", "") if headers is not None else ""
        except Exception:
            user_agent = ""
        model = str(model_hint or "").strip()
        if not model:
            model = str((request_data or {}).get("model") or "").strip()
        wb_usage.record_request(
            account_id=getattr(account, "id", "") or "",
            region=_usage_region_of(account),
            client=wb_usage.detect_client(user_agent, headers),
            model=model or "unknown",
            usage=usage,
            success=bool(success),
            duration_ms=int(duration_ms or 0),
            user_agent=user_agent,
        )
    except Exception as exc:
        log.warning("Token 用量落账失败: %s", exc)


# ===== 模型目录接入 =====

def _duration_seconds(value: Any, default: float = 0.0) -> float:
    """把 '168h' / '30m' / 600 这类写法变成秒。"""
    if value is None or value == "":
        return float(default)
    if isinstance(value, (int, float)):
        return float(value)
    if wb_usage is not None:
        try:
            return float(wb_usage.parse_duration(value, default))
        except Exception:
            pass
    text = str(value).strip().lower()
    try:
        for suffix, factor in (("ms", 0.001), ("s", 1.0), ("m", 60.0), ("h", 3600.0), ("d", 86400.0)):
            if text.endswith(suffix):
                return float(text[: -len(suffix)]) * factor
        return float(text)
    except (TypeError, ValueError):
        return float(default)


def _upstream_message(payload: Any) -> str:
    """从上游错误体里抠出人能看懂的原因。

    上游把诊断信息分几层放：displayMsg（分语言的用户文案）、msg（英文简述）、
    extError.message（模型侧拒答细节）。只回 HTTP 状态码的话，用户只会看到
    「Upstream error: 400」，完全不知道错在哪、下一步该改什么。
    """
    if not isinstance(payload, dict):
        return ""
    for key in ("displayMsg", "display_msg"):
        block = payload.get(key)
        if isinstance(block, dict):
            for lang in ("zh", "zh-hans", "zh-hant", "en"):
                text = block.get(lang)
                if isinstance(text, str) and text.strip():
                    return text.strip()
    msg = payload.get("msg")
    if isinstance(msg, str) and msg.strip():
        return msg.strip()
    for key in ("extError", "ext_error"):
        block = payload.get(key)
        if isinstance(block, dict):
            inner = block.get("message")
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
    return ""


def resolve_model(requested: str, region: Optional[str] = None) -> Dict[str, Any]:
    """决定这次请求实际用哪个模型。

    目录模块缺失时退回老逻辑：只认 REAL_MODELS，其余换成兜底模型。
    替换原因会一路传到日志与面板，避免「明明请求了 GLM-5.3 却用了 flash」这种事
    悄悄发生却没人知道。
    """
    if wb_models is None:
        want = str(requested or "").strip()
        if want in REAL_MODELS:
            return {"model": want, "requested": want, "replaced": False, "reason": "", "spec": None}
        return {"model": DEFAULT_CHAT_MODEL, "requested": want, "replaced": bool(want),
                "reason": "unknown" if want else "empty", "spec": None}
    return wb_models.resolve_requested(requested, region or (pool.region or None))


def build_model_list(region: Optional[str] = None) -> List[Dict[str, Any]]:
    """把模型目录转成 OpenAI /v1/models 形状。

    内置目录里未知的字段（上下文窗口、输出上限）不会出现在响应里 ——
    宁可缺字段，也不给客户端一个看着可信但其实是猜的数字。
    """
    created = 1687882411
    target = region if region is not None else (pool.region or "")
    if wb_models is None:
        return [{"id": m, "object": "model", "created": created, "owned_by": "workbuddy"}
                for m in REAL_MODELS]

    out: List[Dict[str, Any]] = []
    for spec in wb_models.catalog(target or None, enabled_only=True):
        entry: Dict[str, Any] = {
            "id": spec.id,
            "object": "model",
            "created": created,
            "owned_by": "workbuddy",
        }
        if spec.context_window:
            entry["context_window"] = spec.context_window
        if spec.max_tokens:
            entry["max_tokens"] = spec.max_tokens

        meta: Dict[str, Any] = {
            "name": spec.name or spec.id,
            "regions": list(spec.regions),
            "supports_image": spec.supports_image,
            "reasoning_efforts": list(spec.reasoning_efforts),
            "rate": spec.rate,
        }
        if spec.twins:
            meta["twins"] = dict(spec.twins)
        try:
            badges = wb_models.get_prefs().promotions_for(spec.id, active_only=True)
        except Exception:
            badges = []
        if badges:
            meta["badges"] = [b.label for b in badges]
        entry["workbuddy_meta"] = meta
        out.append(entry)
    return out


def build_pool_view(limit: int = 8) -> Dict[str, Any]:
    """池健康视图：策略、排序、下一个轮到谁、每个号为什么不可用。"""
    view: Dict[str, Any] = {
        "policy": {
            "distribution": pool.distribution,
            "region": pool.region or "merged",
            "credit_floor": pool.credit_floor,
            "last_pick_policy": pool.last_pick_policy,
            "last_pick_reason": pool.last_pick_reason,
            "sticky_sessions": len(pool.sticky_sessions),
            "cursors": dict(pool.cursors),
        },
        "status": pool.get_status(),
        "unavailable": pool.unavailable_summary(),
    }
    if wb_pool_policy is None:
        view["policies"] = []
        view["preview"] = None
        view["note"] = "分配策略模块不可用，账号池退回老式轮换"
        return view
    state = pool._policy_state()
    view["policies"] = wb_pool_policy.describe(pool.distribution)
    view["preview"] = wb_pool_policy.preview(
        list(pool.accounts.values()), pool.distribution,
        state=state, region=pool.region or None, credit_floor=pool.credit_floor,
        idle_weight_per_hour=pool.idle_weight_per_hour,
        idle_weight_max=pool.idle_weight_max, limit=limit,
    )
    return view


# ===== 任务引擎辅助 =====

def get_tasks_module():
    """延迟导入 wb_tasks，避免任务模块异常影响网关启动。"""
    global wb_tasks
    if wb_tasks is None:
        try:
            import wb_tasks as _wb_tasks
        except Exception as e:
            # 记下真实原因：面板/接口报「任务引擎不可用」时要能说清为什么。
            global log_tasks_import_err
            log_tasks_import_err = str(e)
            log.warning("wb_tasks 导入失败，任务引擎不可用: %s", e)
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
        _cfg = _load_user_config()
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



def chat_base_for(account: "Account" = None) -> str:
    """按账号区域取上游 base。

    配置里显式写了 upstream.base_url / global.chat_base 时以配置为准（自建反代）；
    没写时按凭据区域自动分流：
      国际版（domain 含 workbuddy.ai）→ https://www.workbuddy.ai
      其它（copilot.tencent.com / codebuddy.cn）→ https://copilot.tencent.com
    旧版一律用国内域名，于是国际版账号必然 401。
    """
    base = (
        str(config.get("upstream", {}).get("base_url") or "").strip()
        or str(config.get("global", {}).get("chat_base") or "").strip()
    ).rstrip("/")
    if not base:
        if account is not None and _usage_region_of(account) == "global":
            return GLOBAL_CHAT_BASE
        return CHAT_BASE
    # 历史版本把默认值写成了不存在的 api.workbuddy.cn，老 config.json 里可能还留着
    if "api.workbuddy.cn" in base:
        return CHAT_BASE
    return base


def chat_upstream_url(account: "Account" = None) -> str:
    """聊天补全的真实上游地址（按账号区域取 base，再补 /v2/chat/completions）。"""
    base = chat_base_for(account)
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
    origin = GLOBAL_ORIGIN if _usage_region_of(account) == "global" else CHAT_BASE
    h = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
        "Authorization": "Bearer " + account.token,
        "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
        "Origin": origin,
        "Referer": origin + "/",
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
    
    def _send_error(self, message: str, status: int = 400, error_type: str = ""):
        """发送错误响应。

        error_type 缺省时按状态码推断：以前 5xx 也一律标成 invalid_request_error，
        等于告诉客户端「是你的请求错了」—— 客户端（含 DSH）据此不再重试，
        503「账号都不可用」这种临时故障就被当成了终局错误。
        """
        if not error_type:
            if status >= 500:
                error_type = "api_error"
            elif status in (401, 403):
                error_type = "authentication_error"
            elif status == 404:
                error_type = "not_found_error"
            else:
                error_type = "invalid_request_error"
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
        
        elif self.path.startswith("/v1/models"):
            if not self._check_auth():
                self._send_error("Unauthorized", 401, "authentication_error")
                return

            # 走模型目录：带上下文窗口 / 图片能力 / 推理档位 / 倍率，并遵守模型开关。
            # 目录模块缺失时 build_model_list 退回旧的 REAL_MODELS 裸列表。
            parsed_models = urlparse(self.path)
            query_models = parse_qs(parsed_models.query)
            region_q = (query_models.get("region") or [""])[0].strip().lower() or None
            if region_q in ("merged", "all", "any"):
                region_q = ""
            self._send_json({"data": build_model_list(region_q), "object": "list"})

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
            qs = parse_qs(urlparse(self.path).query)
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
            qs = parse_qs(urlparse(self.path).query)
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
        
        acc_id = request_data.get("account_id") or request_data.get("id") or None
        # 空 body / 只带 rotate=true → 自动挑下一个（"一键换号"）
        if request_data.get("rotate") is True and not acc_id:
            acc_id = None

        new_id = pool.switch_account(acc_id)
        if new_id:
            pool.save(STATE_FILE)
            self._send_json({"success": True, "account_id": new_id, "status": pool.get_status()})
        else:
            self._send_error("No available account", 409)

    def _handle_logs_api(self):
        """采集日志 + 采集进程状态（原老控制台「📈 实时面板」的数据源）。"""
        if wb_ops is None:
            self._send_json({
                "log": [],
                "collect": {"running": False, "unavailable": f"wb_ops 不可用: {log_ops_import_err}"},
            })
            return
        n = 200
        try:
            qs = parse_qs(urlparse(self.path).query)
            n = max(1, min(2000, int((qs.get("n") or ["200"])[0])))
        except (ValueError, TypeError):
            n = 200
        self._send_json({"log": wb_ops.get_collect_log(n), "collect": wb_ops.get_collect_status()})

    def _handle_collect_api(self, request_data: Optional[Dict[str, Any]] = None):
        """积分采集进程：GET=看状态，POST collect/start=启动，POST collect/stop=停止。"""
        if wb_ops is None:
            self._send_error(f"wb_ops 不可用: {log_ops_import_err}", 503)
            return
        request_data = request_data or {}
        path = urlparse(self.path).path.rstrip("/")
        if self.command == "GET" or path.endswith("collect/status"):
            self._send_json(wb_ops.get_collect_status())
            return
        if self.command != "POST":
            self._send_error("Method Not Allowed", 405)
            return
        if path.endswith("collect/stop"):
            self._send_json(wb_ops.stop_collect())
            return
        action = str(request_data.get("action") or "collect")
        self._send_json(wb_ops.start_collect(action))

    def _handle_diagnose_api(self):
        """系统自检四项：python / gateway / deps / files（原老控制台「🔧 诊断」页签）。"""
        if wb_ops is None:
            self._send_error(f"wb_ops 不可用: {log_ops_import_err}", 503)
            return
        self._send_json(wb_ops.run_checks())

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

    def _pick_chat_account(self, session_id: str, skip=None):
        """给聊天请求挑账号：优先「当前账号」，不可用才走池策略；skip 里的跳过。

        skip 是本次请求已经试过的账号（401 换号用），避免同一个坏 token 连试三次。
        """
        exclude = set(skip or ())
        current_id = pool.current_account_id
        if current_id and current_id in pool.accounts and current_id not in exclude:
            candidate = pool.accounts[current_id]
            if pool._is_available(candidate) and candidate.token:
                return candidate
        picked = pool.pick(session_id)
        if picked is not None and picked.id not in exclude and picked.token:
            return picked
        # 策略有可能又挑了已经试过的号（priority 会让当前号一直排第一）→ 兜底扫一遍池子
        rest = [
            acc for acc in pool.accounts.values()
            if acc.id not in exclude and pool._is_available(acc) and acc.token
        ]
        if not rest:
            return None
        if wb_pool_policy is not None:
            try:
                ordered = wb_pool_policy.order_priority(rest)
                if ordered:
                    return ordered[0]
            except Exception as exc:
                log.warning("兜底选号失败，改用最久未用优先: %s", exc)
        rest.sort(key=lambda a: (-a.last_used, -a.credits, a.id))
        return rest[0]

    def _send_account_headers(self, account, model_name: str = "") -> None:
        """把「这一次用的是哪个账号」写进响应头，方便在 DSH / curl 侧核对。

        http.server 只按 latin-1 编码响应头，中文昵称必须先 quote 成 ASCII。
        """
        self.send_header("X-WorkBuddy-Account-Id", account.id)
        name = getattr(account, "name", "") or ""
        self.send_header("X-WorkBuddy-Account", quote(name) if name else account.id[:8])
        self.send_header("X-WorkBuddy-Region", _usage_region_of(account))
        if model_name:
            self.send_header("X-WorkBuddy-Model", str(model_name))
        self.send_header(
            "Access-Control-Expose-Headers",
            "X-WorkBuddy-Account,X-WorkBuddy-Account-Id,X-WorkBuddy-Region,X-WorkBuddy-Model",
        )

    def _handle_chat_completions(self):
        """处理聊天补全请求。

        账号失败自动换号：某个账号 token 过期 / 区域不对（上游 401/403）时只让该账号
        冷却，再拿池子里的下一个号重试 —— 一个坏号不该把整个 DSH 请求打成失败。
        """
        # 读取请求体
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode('utf-8')

        try:
            request_data = json.loads(body)
        except json.JSONDecodeError:
            self._send_error("Invalid JSON", 400)
            return

        session_id = request_data.get("user", str(uuid.uuid4()))
        stream = bool(request_data.get("stream"))

        # 池子里一个能用的号都没有 → 直接 503，并把每个号为什么不能用说清楚
        if not pool.accounts or not any(
            a.token and pool._is_available(a) for a in pool.accounts.values()
        ):
            stuck = pool.unavailable_summary()
            if stuck:
                detail = "；".join(
                    "%s(%s)" % ((x["name"] or x["id"][:8]), x["reason"]) for x in stuck[:4]
                )
            else:
                detail = "账号池为空"
            self._send_error("No available accounts: %s" % detail, 503)
            return

        # 上游只接受流式请求（非流式实测直接 400 code=11101），所以对上游永远发
        # stream=true；客户端要非流式时用 aggregate_sse_completion 把 SSE 聚合成
        # 普通 completion 返回。
        up_body = dict(request_data)
        up_body["stream"] = True

        # 模型名兜底：走模型目录（未知 / 被隐藏 / 跨区 一律换成兜底模型）。
        want_model = str(request_data.get("model") or "").strip()
        resolved = resolve_model(want_model, pool.region or None)
        up_body["model"] = resolved["model"]
        if resolved.get("replaced"):
            log.info("模型 %s -> %s（%s）",
                     want_model or "(空)", resolved["model"], resolved.get("reason") or "-")

        tried: List[str] = []
        antiprobe_hits: Dict[str, int] = {}
        last_error = ""
        # 预算 = 换号次数 + 反探测原地重发次数，避免重发把换号额度吃光
        for _attempt in range(MAX_ACCOUNT_TRIES + ANTIPROBE_MAX_RETRIES):
            account = self._pick_chat_account(session_id, tried)
            if not account or not account.token:
                last_error = last_error or "没有更多可用账号"
                break
            tried.append(account.id)
            # 上游地址按账号区域分流：国际版 token 打到国内域名会被 openresty 回 401
            upstream_url = chat_upstream_url(account)

            # ===== Token 统计：起始时间与聚合占位 =====
            usage_started = time.time()
            stream_tap = None
            aggregated_usage = None
            aggregated_model = ""
            headers_sent = False

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
                        self._send_account_headers(account, up_body.get("model", ""))
                        self.end_headers()
                        headers_sent = True
                        if wb_usage is not None:
                            stream_tap = wb_usage.StreamUsageTap()
                        while True:
                            chunk = resp.read(1024)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            # 边透传边抓 usage：上游把 usage 放在最后一个 chunk
                            if stream_tap is not None:
                                stream_tap.feed(chunk)
                        if stream_tap is not None:
                            stream_tap.finish()
                    else:
                        response_data = aggregate_sse_completion(resp, request_data.get("model") or "")
                        try:
                            _parsed = json.loads(response_data)
                            if isinstance(_parsed, dict):
                                aggregated_usage = _parsed.get("usage")
                                aggregated_model = str(_parsed.get("model") or "")
                        except Exception:
                            aggregated_usage = None
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self._send_account_headers(account, up_body.get("model", ""))
                        self.end_headers()
                        headers_sent = True
                        self.wfile.write(response_data.encode('utf-8'))
                    pool.record_success(account.id)

                    # ===== Token 统计：成功请求落账 =====
                    if stream_tap is not None:
                        record_chat_usage(
                            account, request_data, stream_tap.usage, True,
                            (time.time() - usage_started) * 1000.0, self.headers,
                            model_hint=stream_tap.model or up_body.get("model", ""),
                        )
                    else:
                        record_chat_usage(
                            account, request_data, aggregated_usage, True,
                            (time.time() - usage_started) * 1000.0, self.headers,
                            model_hint=aggregated_model or up_body.get("model", ""),
                        )

                    return
            except HTTPError as e:
                error_body = e.read().decode('utf-8') if e.fp else ""
                is_auth_error = e.code in (401, 403)
                # 上游把具体原因放在 body 的 code 里（如 11128 反探测 / 11101 非流式），
                # 只回 HTTP 状态码会丢掉全部诊断信息。
                upstream_code = None
                upstream_msg = ""
                try:
                    _ub = json.loads(error_body)
                    if isinstance(_ub, dict):
                        upstream_code = _ub.get("code")
                        upstream_msg = _upstream_message(_ub)
                except Exception:
                    upstream_code = None
                is_antiprobe = e.code == 400 and (
                    upstream_code == 11128
                    or "request illegal" in error_body.lower()
                    or "请求被拦截" in error_body
                )
                # 400 且不是反探测 = 这份请求本身不合法（模型名不存在 / 参数被模型侧拒绝）。
                # 同一份请求体发给池子里哪个账号都是同样的 400，那不是账号的健康问题：
                # 既不该换号重试，更不该计入熔断 —— 否则客户端连发 3 个坏请求就能把好账号
                # 熔断 30 分钟、把整个网关打成 503（本地实测复现过）。
                is_client_fault = e.code == 400 and not is_antiprobe
                log.error("上游返回错误 %s（账号 %s / %s）: %s",
                          e.code, account.name or account.id[:8], account.id[:8],
                          error_body[:200])

                # 反探测与「客户端请求不合法」都不进熔断计数：前者有自己的短冷却，
                # 后者与账号健康无关。只有鉴权失败 / 限流 / 上游 5xx / 网络错误才算账号的账。
                if not is_antiprobe and not is_client_fault:
                    pool.record_failure(account.id, e.code == 429)

                # Token 统计：失败也计次，面板才能算成功率
                record_chat_usage(
                    account, request_data, None, False,
                    (time.time() - usage_started) * 1000.0, self.headers,
                    model_hint=up_body.get("model", ""),
                )

                if is_antiprobe and not headers_sent:
                    hits = antiprobe_hits.get(account.id, 0)
                    antiprobe_hits[account.id] = hits + 1
                    last_error = "账号 %s 被上游反探测拦截(code=%s)" % (
                        account.name or account.id[:8], upstream_code or e.code)
                    if hits < ANTIPROBE_MAX_RETRIES:
                        log.warning("账号 %s 被上游拦截(code=%s)，%.1fs 后原地重发（第 %d/%d 次）",
                                    account.id[:8], upstream_code or e.code,
                                    ANTIPROBE_RETRY_DELAY, hits + 1, ANTIPROBE_MAX_RETRIES)
                        tried.pop()  # 原地重发不算这个号已试过
                        time.sleep(ANTIPROBE_RETRY_DELAY)
                        continue
                    log.warning("账号 %s 连续被上游拦截(code=%s)，冷却 %.0fs 后换号",
                                account.id[:8], upstream_code or e.code, ANTIPROBE_FAIL_COOLDOWN)
                    account.cooldown_until = time.time() + ANTIPROBE_FAIL_COOLDOWN
                    continue

                if is_client_fault and not headers_sent:
                    # 换号也没用 —— 同样的请求体在哪个账号上都会被拒。直接把上游的
                    # 真实原因回给客户端，别再回「Upstream error: 400」这种废话。
                    log.warning("请求被上游拒绝(HTTP 400%s)，不计入账号健康: %s",
                                "" if upstream_code is None else " code=%s" % upstream_code,
                                upstream_msg or error_body[:120])
                    self._send_error(
                        "Upstream rejected the request (HTTP 400%s): %s" % (
                            "" if upstream_code is None else " code=%s" % upstream_code,
                            upstream_msg or "no detail provided"),
                        e.code, "invalid_request_error")
                    return

                if is_auth_error:
                    # 401/403 = 上游不认这把凭据（过期 / 区域不对 / 换端）。让它单独
                    # 冷却一段时间，然后换下一个账号重试。
                    account.cooldown_until = time.time() + AUTH_FAIL_COOLDOWN
                    last_error = "账号 %s 鉴权失败(%s)" % (account.name or account.id[:8], e.code)
                    if not headers_sent and len(tried) < MAX_ACCOUNT_TRIES:
                        log.warning("账号 %s 鉴权失败，冷却 %.0f 秒后换号重试",
                                    account.id[:8], AUTH_FAIL_COOLDOWN)
                        continue

                if headers_sent:
                    log.warning("响应头已发出，无法再回传上游错误")
                    return
                self._send_error(
                    "Upstream error %s: %s" % (e.code, upstream_msg or "no detail provided"),
                    e.code, "api_error")
                return
            except Exception as e:
                log.error(f"请求失败: {e}")
                pool.record_failure(account.id)
                record_chat_usage(
                    account, request_data, None, False,
                    (time.time() - usage_started) * 1000.0, self.headers,
                    model_hint=up_body.get("model", ""),
                )
                if headers_sent:
                    return
                self._send_error(f"Request failed: {str(e)}", 500)
                return

        # 池子里的号都试过了还是不行
        self._send_error(
            "No usable account after %d attempt(s): %s" % (len(tried), last_error or "全部失败"),
            503,
        )

    def _handle_usage_api(self):
        """Token 统计接口：/panel/api/usage 取聚合，/panel/api/usage/meta 取筛选项。"""
        if wb_usage is None:
            self._send_error("Token 统计模块不可用: %s" % (log_usage_import_err,), 501)
            return

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        def _one(name, default=""):
            values = query.get(name) or []
            return str(values[0]).strip() if values else default

        if parsed.path.rstrip("/").endswith("/usage/meta"):
            store = wb_usage.get_store()
            self._send_json({
                "ranges": [
                    {"key": key, "label": wb_usage.RANGE_LABELS.get(key, key)}
                    for key in wb_usage.RANGE_KEYS
                ],
                "regions": [
                    {"key": "merged", "label": "合并"},
                    {"key": "cn", "label": "国内版"},
                    {"key": "global", "label": "国际版"},
                ],
                "clients": [
                    {"key": key, "label": wb_usage.CLIENT_LABELS.get(key, key)}
                    for key in wb_usage.CLIENT_ORDER
                ],
                "store": store.stats(),
                "top_user_agents": store.top_user_agents(12),
                "note": (
                    "只统计经本网关转发的请求；客户端维度按调用来源的 "
                    "User-Agent / Header 归类，不代表上游账号的全量用量。"
                ),
            })
            return

        summary = wb_usage.summary(
            range_key=_one("range", "today") or "today",
            region=_one("region", "merged") or "merged",
            client=_one("client") or None,
            model=_one("model") or None,
        )
        # 「用的是哪个账号的积分」：by_account 原本只有 uuid，面板上没法认人。
        # 这里把账号昵称补进去（id → name），面板直接显示昵称。
        try:
            names = {acc.id: (acc.name or acc.id) for acc in pool.accounts.values()}
            for row in summary.get("by_account") or []:
                key = str(row.get("key") or "")
                if key in names:
                    row["name"] = names[key]
                    row["label"] = names[key]
        except Exception as exc:
            log.warning("账号昵称回填失败: %s", exc)
        self._send_json(summary)

    def _handle_models_api(self):
        """模型目录接口：/panel/api/models。

        GET  → 目录快照（含每个字段来自 builtin / upstream / override）
        POST → {action: toggle|set|bulk|override|clear_overrides|merge|promotions|prune_promotions}
        """
        if wb_models is None:
            self._send_error("模型目录模块不可用: %s" % (log_models_import_err,), 501)
            return

        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        region = (query.get("region") or [""])[0].strip().lower() or None
        if region in ("merged", "all", "any"):
            region = None

        content_length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
        payload: Dict[str, Any] = {}
        if body:
            try:
                payload = json.loads(body) or {}
            except json.JSONDecodeError:
                self._send_error("Invalid JSON", 400)
                return

        action = str(payload.get("action") or ("snapshot" if not body else "set")).strip().lower()
        prefs = wb_models.get_prefs()
        note = ""
        try:
            if action == "toggle":
                enabled = prefs.toggle(payload.get("model"), payload.get("region") or region)
                note = "已%s %s" % ("启用" if enabled else "隐藏", payload.get("model"))
            elif action in ("set", "bulk"):
                models = payload.get("models") or ([payload.get("model")] if payload.get("model") else [])
                changed = prefs.bulk_set([m for m in models if m],
                                         bool(payload.get("enabled", True)),
                                         payload.get("region") or region)
                note = "改了 %d 个模型的可见性" % changed
            elif action == "override":
                ok = prefs.set_override(payload.get("model"), **(payload.get("fields") or {}))
                note = "已写入人工覆盖" if ok else "没有可写入的字段"
            elif action == "clear_overrides":
                prefs.clear_overrides(payload.get("model") or None)
                note = "已清空覆盖"
            elif action == "merge":
                count = prefs.merge_discovered(payload.get("entries") or [],
                                               region=payload.get("region") or region)
                note = "已并入 %d 个模型的发现结果" % count
            elif action == "promotions":
                count = prefs.set_promotions(payload.get("promotions") or [])
                note = "已写入 %d 条促销" % count
            elif action == "prune_promotions":
                note = "已清理 %d 条过期促销" % prefs.prune_promotions()
            elif action != "snapshot":
                self._send_error("未知操作: %s" % action, 400)
                return
        except Exception as exc:
            self._send_error("模型目录操作失败: %s" % exc, 400)
            return

        snapshot = prefs.snapshot(region)
        snapshot["note"] = note
        self._send_json(snapshot)

    def _handle_pool_api(self):
        """账号池分配策略接口：/panel/api/pool。

        GET  → 策略 + 池状态 + 排序预览 + 每个不可用账号的原因
        POST → {distribution: priority|round-robin|balanced|sticky} 切换策略并落盘 config.json
        """
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        content_length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(content_length).decode("utf-8") if content_length else ""
        payload: Dict[str, Any] = {}
        if body:
            try:
                payload = json.loads(body) or {}
            except json.JSONDecodeError:
                self._send_error("Invalid JSON", 400)
                return

        if payload.get("distribution"):
            if wb_pool_policy is None:
                self._send_error("分配策略模块不可用: %s" % (log_policy_import_err,), 501)
                return
            new_policy = wb_pool_policy.normalize_policy(payload["distribution"])
            pool.distribution = new_policy
            pool_config = config.setdefault("pool", {})
            pool_config["distribution"] = new_policy
            save_config()
            log.info("账号池分配策略已切到 %s", wb_pool_policy.POLICY_LABELS.get(new_policy, new_policy))

        try:
            limit = int((query.get("limit") or ["8"])[0])
        except (TypeError, ValueError):
            limit = 8
        limit = max(1, min(50, limit))
        self._send_json(build_pool_view(limit))

    def _handle_checkin_api(self):
        """签到 / Catch-up 补签 / 收益账本。GET 读报告，POST action=run|state 执行签到。"""
        if wb_checkin is None:
            self._send_error("签到模块不可用: %s" % log_checkin_import_err, 501)
            return

        request_data: Dict[str, Any] = {}
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length > 0:
            raw_body = self.rfile.read(content_length).decode("utf-8", "replace")
            try:
                parsed_body = json.loads(raw_body) if raw_body.strip() else {}
            except json.JSONDecodeError:
                parsed_body = {}
            if isinstance(parsed_body, dict):
                request_data = parsed_body

        try:
            days = int(request_data.get("days") or 7)
        except (TypeError, ValueError):
            days = 7
        days = max(1, min(90, days))
        try:
            limit = int(request_data.get("limit") or 300)
        except (TypeError, ValueError):
            limit = 300
        limit = max(1, min(5000, limit))
        uid = request_data.get("uid") or request_data.get("account_id")
        day = request_data.get("day")

        if self.command != "POST":
            body = wb_checkin.report(days=days, limit=limit, uid=uid, day=day)
            body["account_rows"] = wb_checkin.account_state_rows(get_task_account_list())
            body["state_file"] = str(wb_checkin.CHECKIN_STATE_FILE)
            self._send_json(body)
            return

        action = str(request_data.get("action") or "run")
        if action == "state":
            self._send_json({
                "ok": True,
                "today": wb_checkin.utc8_date(),
                "account_rows": wb_checkin.account_state_rows(get_task_account_list()),
                "state": wb_checkin.load_state(),
            })
            return

        accounts = get_task_account_list()
        if uid:
            accounts = [a for a in accounts if str(a.get("uid")) == str(uid) or a.get("name") == uid]
        if not accounts:
            self._send_json({
                "ok": False,
                "error": "没有可用账号（auths/ 为空，且网关池未加载 token）",
                "today": wb_checkin.utc8_date(),
            })
            return

        # 签到前刷新即将过期的 token；失败不阻断，具体账号按原错误反馈。
        refresh_auth_tokens(force=False)
        accounts = get_task_account_list()
        if uid:
            accounts = [a for a in accounts if str(a.get("uid")) == str(uid) or a.get("name") == uid]

        result = wb_checkin.check_in_accounts(
            accounts,
            catch_up=bool(request_data.get("catch_up", True)),
            dry_run=bool(request_data.get("dry_run", False)),
            force=bool(request_data.get("force", False)),
        )
        report = wb_checkin.report(days=days, limit=limit, uid=uid, day=day)
        report["account_rows"] = wb_checkin.account_state_rows(accounts)
        report["state_file"] = str(wb_checkin.CHECKIN_STATE_FILE)
        result["report"] = report
        self._send_json(result)

    def _handle_snapshot_api(self):
        """桌面/旧版登录快照发现与导入。GET 扫描，POST action=import 导入。"""
        if wb_snapshot is None:
            self._send_error("快照模块不可用: %s" % log_snapshot_import_err, 501)
            return

        request_data: Dict[str, Any] = {}
        content_length = int(self.headers.get("Content-Length") or 0)
        if content_length > 0:
            raw_body = self.rfile.read(content_length).decode("utf-8", "replace")
            try:
                parsed_body = json.loads(raw_body) if raw_body.strip() else {}
            except json.JSONDecodeError:
                parsed_body = {}
            if isinstance(parsed_body, dict):
                request_data = parsed_body

        if self.command == "POST" and str(request_data.get("action") or "") == "import":
            result = wb_snapshot.import_snapshot(request_data, dry_run=bool(request_data.get("dry_run")))
            if result.get("ok") and not request_data.get("dry_run"):
                load_auths()
                result["pool_accounts"] = len(pool.accounts)
            self._send_json(result, 200 if result.get("ok") else 400)
            return

        extra = request_data.get("extra_roots")
        if not isinstance(extra, list) or not extra:
            extra = [str(BASE_DIR.parent)]
        self._send_json(wb_snapshot.scan(extra_roots=extra))

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
        if "/panel/api/usage" in self.path:
            self._handle_usage_api()
            return

        if "/panel/api/models" in self.path:
            self._handle_models_api()
            return

        if "/panel/api/pool" in self.path:
            self._handle_pool_api()
            return

        if "/panel/api/checkin" in self.path:
            self._handle_checkin_api()
            return

        if "/panel/api/logs" in self.path:
            self._handle_logs_api()
            return

        if "/panel/api/collect" in self.path:
            self._handle_collect_api(request_data)
            return

        if "/panel/api/diagnose" in self.path:
            self._handle_diagnose_api()
            return

        if "/panel/api/keepalive" in self.path:
            # 账号保活（原老控制台 🛡️ 页签的能力）；dry_run 时只演练不发请求
            self._send_json(handle_task_action("keepalive", request_data))
            return

        if "/panel/api/snapshots" in self.path:
            self._handle_snapshot_api()
            return

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


# ===== 面板静态资源（static/ 目录）=====

PANEL_STATIC_DIR = BASE_DIR / "static"

STATIC_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


def resolve_static(rel: str) -> Optional[Path]:
    """把 URL 里的相对路径安全地映射到 static/ 下的文件。

    只允许 static/ 内部的普通文件：拒绝空字节、绝对路径以及任何 ".." 分段，
    这样 /panel/static/../../config.json 之类的请求拿不到 config.json 与 auths/。
    """
    rel = (rel or "").split("?", 1)[0].split("#", 1)[0].replace("\\", "/").strip("/")
    if not rel:
        rel = "index.html"
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        return None
    if any("\x00" in p for p in parts):
        return None
    root = PANEL_STATIC_DIR.resolve()
    try:
        resolved = root.joinpath(*parts).resolve()
    except OSError:
        return None
    if resolved != root and root not in resolved.parents:
        return None
    if not resolved.is_file():
        return None
    return resolved


def serve_static(handler, rel: str) -> bool:
    """送出 static/ 下的一个文件；文件不存在或不合法时返回 False。"""
    target = resolve_static(rel)
    if target is None:
        return False
    try:
        data = target.read_bytes()
    except OSError as e:
        log.warning("静态资源读取失败 %s: %s", target, e)
        return False
    ctype = STATIC_MIME.get(target.suffix.lower(), "application/octet-stream")
    handler.send_response(200)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)
    return True


def serve_panel_sw(handler):
    """送出面板的 Service Worker。

    Service Worker 的 scope 由脚本自身 URL 决定，所以它必须挂在 /panel/ 根下
    （/panel/static/sw.js 的 scope 只能是 /panel/static/）。源码仍放 static/sw.js，
    这里额外声明 Service-Worker-Allowed 让 scope 覆盖整个 /panel/。
    """
    target = PANEL_STATIC_DIR / "sw.js"
    try:
        data = target.read_bytes()
    except OSError:
        handler.send_error(404, "Not Found")
        return
    handler.send_response(200)
    handler.send_header("Content-Type", "application/javascript; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Service-Worker-Allowed", "/panel/")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(data)


def panel_index_file() -> Optional[Path]:
    """新面板入口 static/index.html（存在时优先于内置旧面板）。"""
    target = PANEL_STATIC_DIR / "index.html"
    try:
        return target if target.is_file() else None
    except OSError:
        return None


def serve_panel(handler):
    """提供 Web 面板：优先 static/index.html，缺失时回退内置旧面板。"""
    target = panel_index_file()
    if target is not None:
        try:
            data = target.read_bytes()
        except OSError:
            data = None
        if data is not None:
            handler.send_response(200)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            if handler.command != "HEAD":
                handler.wfile.write(data)
            return
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(PANEL_HTML.encode('utf-8'))


# 重写 GatewayHandler 的 GET 方法以支持面板
_original_do_get = GatewayHandler.do_GET


def patched_do_get(self):
    path = (self.path or "").split("?", 1)[0].split("#", 1)[0]
    if path in ("/panel", "/panel/", "/panel/index.html"):
        serve_panel(self)
        return
    if path in ("/panel/sw.js", "/panel/service-worker.js"):
        serve_panel_sw(self)
        return
    if path.startswith("/panel/static/"):
        rel = path[len("/panel/static/"):]
        if serve_static(self, rel):
            return
        self.send_error(404, "Not Found")
        return
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
        # 退出前把还未落盘的 Token 用量刷掉
        if wb_usage is not None:
            try:
                wb_usage.flush()
                log.info("Token 用量已保存: %s", wb_usage.get_store().path)
            except Exception as exc:
                log.warning("Token 用量保存失败: %s", exc)
        server.shutdown()
        print("[完成] 服务已停止")


if __name__ == "__main__":
    main()