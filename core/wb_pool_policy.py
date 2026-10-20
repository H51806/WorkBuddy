"""账号池分配策略（wb_pool_policy）。

四种分配方式（与上游 WorkBuddy 插件 XD Pool 的 distribution 对齐）：
  priority      先用完一个：固定在一个号上，直到它被熔断/冷却/停用才换下一个；
  round-robin   顺序轮换：按固定顺序一个个来，每个号轮流拿到请求；
  balanced      均衡分配：按「闲置越久权重越大」随机挑，长期不用到的号更容易被选中；
  sticky        对话粘性：同一个对话始终用同一个账号。

为什么粘性重要：上游的 prompt 缓存是按账号隔离的。一个对话中途换号，
整段缓存作废，输入 token 全额重算 —— 这是真金白银的差距，不是偏好问题。

为什么需要「先用完一个」：积分的价值随到期时间衰减，分散使用会让多份积分同时过期。
优先把一份用干净，过期损失最小。

本模块只做纯逻辑：不碰网络、不碰文件、不导入网关。
调用方（AccountPool）负责提供可用账号列表与持久化游标。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

# ===== 常量 =====

POLICIES: Tuple[str, ...] = ("priority", "round-robin", "balanced", "sticky")
DEFAULT_POLICY = "priority"

POLICY_LABELS: Dict[str, str] = {
    "priority": "先用完一个",
    "round-robin": "顺序轮换",
    "balanced": "均衡分配",
    "sticky": "对话粘性",
}

POLICY_NOTES: Dict[str, str] = {
    "priority": "固定一个号，用尽或被熔断才换 —— 减少积分同时过期的损失",
    "round-robin": "按顺序轮流，负载平均摊开",
    "balanced": "闲置越久越容易被选中，随机但偏向冷号",
    "sticky": "一个对话固定一个账号，保住上游 prompt 缓存",
}

REASON_LABELS: Dict[str, str] = {
    "sticky-hit": "命中对话粘性",
    "sticky-new": "新对话首轮",
    "priority": "先用完一个",
    "round-robin": "顺序轮换",
    "balanced": "均衡分配",
    "fallback": "兜底（唯一可用）",
    "empty": "无可用账号",
    "current": "沿用当前账号",
}


def normalize_policy(policy: Optional[str]) -> str:
    text = str(policy or "").strip().lower().replace("_", "-")
    aliases = {
        "lru": "balanced",
        "random": "balanced",
        "rr": "round-robin",
        "roundrobin": "round-robin",
        "round-robin": "round-robin",
        "sticky-session": "sticky",
        "session": "sticky",
        "drain": "priority",
    }
    if text in POLICIES:
        return text
    return aliases.get(text, DEFAULT_POLICY)


def policy_note(policy: Optional[str]) -> str:
    key = normalize_policy(policy)
    return POLICY_NOTES.get(key, "")


# ===== 账号视图 =====

def region_of(account: Any) -> str:
    """按凭据域名判断区域。与 wb_gateway._usage_region_of 保持一致。"""
    domain = str(getattr(account, "domain", "") or "").lower()
    return "global" if "workbuddy.ai" in domain else "cn"


def account_id(account: Any) -> str:
    return str(getattr(account, "id", "") or "")


def _num(account: Any, name: str, default: float = 0.0) -> float:
    try:
        value = getattr(account, name, default)
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def is_available(account: Any, now: Optional[float] = None) -> bool:
    """是否可用：启用 + 未熔断 + 未冷却。与 AccountPool._is_available 对齐。"""
    ts = time.time() if now is None else float(now)
    if not bool(getattr(account, "active", True)):
        return False
    if _num(account, "breaker_until") > ts:
        return False
    if _num(account, "cooldown_until") > ts:
        return False
    return True


def unavailable_reason(account: Any, now: Optional[float] = None) -> str:
    ts = time.time() if now is None else float(now)
    if not bool(getattr(account, "active", True)):
        return "停用"
    left = _num(account, "breaker_until") - ts
    if left > 0:
        return "熔断中 %.1f 分钟" % (left / 60.0)
    left = _num(account, "cooldown_until") - ts
    if left > 0:
        return "冷却中 %.1f 分钟" % (left / 60.0)
    return ""


def scope(
    accounts: Sequence[Any],
    *,
    region: Optional[str] = None,
    credit_floor: float = 0.0,
    now: Optional[float] = None,
) -> List[Any]:
    """挑出这次可以考虑的账号。region=None / 'merged' 表示不限区域。"""
    target = str(region or "").strip().lower()
    if target in ("", "merged", "all", "any"):
        target = ""
    out: List[Any] = []
    for account in accounts:
        if target and region_of(account) != target:
            continue
        if not is_available(account, now):
            continue
        if credit_floor and _num(account, "credits") < float(credit_floor):
            continue
        out.append(account)
    return out


# ===== 持久化游标 =====

@dataclass
class PolicyState:
    """跨请求要保留的游标。由 AccountPool 负责写进 state.json。"""

    cursors: Dict[str, int] = field(default_factory=dict)   # 'region|policy' -> 下标
    sticky: Dict[str, str] = field(default_factory=dict)     # session_id -> account_id
    sticky_at: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"cursors": dict(self.cursors), "sticky": dict(self.sticky),
                "sticky_at": {k: float(v) for k, v in self.sticky_at.items()}}

    @classmethod
    def from_dict(cls, raw: Any) -> "PolicyState":
        state = cls()
        if not isinstance(raw, dict):
            return state
        cursors = raw.get("cursors")
        if isinstance(cursors, dict):
            for key, value in cursors.items():
                try:
                    state.cursors[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue
        sticky = raw.get("sticky")
        if isinstance(sticky, dict):
            state.sticky = {str(k): str(v) for k, v in sticky.items()}
        sticky_at = raw.get("sticky_at")
        if isinstance(sticky_at, dict):
            for key, value in sticky_at.items():
                try:
                    state.sticky_at[str(key)] = float(value)
                except (TypeError, ValueError):
                    continue
        return state

    # ---- 粘性表维护 ----
    def get_sticky(self, session_id: Optional[str], now: Optional[float] = None,
                   ttl: float = 0.0) -> Optional[str]:
        if not session_id:
            return None
        acc_id = self.sticky.get(str(session_id))
        if not acc_id:
            return None
        if ttl and now is not None:
            seen = self.sticky_at.get(str(session_id), 0.0)
            if seen and (now - seen) > ttl:
                self.sticky.pop(str(session_id), None)
                self.sticky_at.pop(str(session_id), None)
                return None
        return acc_id

    def set_sticky(self, session_id: Optional[str], acc_id: Optional[str],
                   now: Optional[float] = None) -> None:
        if not session_id:
            return
        key = str(session_id)
        if acc_id:
            self.sticky[key] = str(acc_id)
            if now is not None:
                self.sticky_at[key] = float(now)
        else:
            self.sticky.pop(key, None)
            self.sticky_at.pop(key, None)

    def drop_account(self, acc_id: str) -> int:
        """账号被移除时清掉所有指向它的粘性记录。返回清掉的条数。"""
        target = str(acc_id)
        keys = [k for k, v in self.sticky.items() if v == target]
        for key in keys:
            self.sticky.pop(key, None)
            self.sticky_at.pop(key, None)
        return len(keys)

    def gc(self, now: Optional[float] = None, ttl: float = 0.0) -> int:
        """清理过期粘性记录。ttl<=0 表示不清理。"""
        if ttl <= 0 or now is None:
            return 0
        dead = [k for k, at in self.sticky_at.items() if at and (now - at) > ttl]
        for key in dead:
            self.sticky.pop(key, None)
            self.sticky_at.pop(key, None)
        return len(dead)

    def clear_sticky(self) -> int:
        count = len(self.sticky)
        self.sticky.clear()
        self.sticky_at.clear()
        return count


# ===== 选择结果 =====

@dataclass
class Selection:
    account: Any = None
    policy: str = DEFAULT_POLICY
    reason: str = ""
    candidates: int = 0

    @property
    def account_id(self) -> str:
        return account_id(self.account) if self.account is not None else ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy": self.policy,
            "reason": self.reason,
            "reason_label": REASON_LABELS.get(self.reason, self.reason),
            "account_id": self.account_id,
            "candidates": self.candidates,
        }


# ===== 各策略的排序 / 选择 =====

def _idle_seconds(account: Any, now: float) -> float:
    last = _num(account, "last_used")
    if last <= 0:
        return float("inf")
    return max(0.0, now - last)


def order_priority(candidates: Sequence[Any], now: float) -> List[Any]:
    """最近的号排前面：把流量压在一个号上直到它不可用。

    全部是全新号（last_used=0）时退化为「积分多的优先」，
    这样挑中的那个号能撑更久。
    """
    return sorted(candidates, key=lambda a: (-_num(a, "last_used"), -_num(a, "credits"), account_id(a)))


def order_round_robin(candidates: Sequence[Any], offset: int = 0) -> List[Any]:
    """固定顺序（按 id 排序）后从 offset 处开始，形成轮换。"""
    ordered = sorted(candidates, key=account_id)
    if not ordered:
        return ordered
    start = int(offset) % len(ordered)
    return ordered[start:] + ordered[:start]


def order_balanced(candidates: Sequence[Any], now: float,
                   idle_weight_per_hour: float = 0.5,
                   idle_weight_max: float = 5.0) -> List[Any]:
    """闲置越久越靠前（确定性排序）。真正的随机加权在 select 里做。"""
    def weight(account: Any) -> float:
        idle = _idle_seconds(account, now)
        if idle == float("inf"):
            return float(idle_weight_max)
        return max(1.0, min(float(idle_weight_max), float(idle_weight_per_hour) * (idle / 3600.0)))

    return sorted(candidates, key=lambda a: (-weight(a), account_id(a)))


def order_prefer_expiring(candidates: Sequence[Any], now: float,
                          expiring_soon: float = 0.0) -> List[Any]:
    """积分快过期的号排前面（扣掉积分快用尽的号）。"""
    def key(account: Any) -> Tuple[int, float, str]:
        expires = _num(account, "credits_expire_at")
        soon = 0
        if expires > 0 and (expiring_soon <= 0 or (expires - now) <= expiring_soon):
            soon = 1
        return (-soon, expires if expires > 0 else float("inf"), account_id(account))

    return sorted(candidates, key=key)


def weighted_choice(candidates: Sequence[Any], now: float,
                    idle_weight_per_hour: float = 0.5,
                    idle_weight_max: float = 5.0,
                    rng: Optional[random.Random] = None) -> Any:
    """按「闲置越久权重越大」随机挑一个。rng 可注入以便复现。"""
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    weights: List[float] = []
    for account in candidates:
        idle = _idle_seconds(account, now)
        if idle == float("inf"):
            weights.append(float(idle_weight_max))
        else:
            weights.append(max(1.0, min(float(idle_weight_max), float(idle_weight_per_hour) * (idle / 3600.0))))
    picker = rng or random
    total = sum(weights)
    if total <= 0:
        return candidates[0]
    point = picker.random() * total
    upto = 0.0
    for account, weight in zip(candidates, weights):
        upto += weight
        if point <= upto:
            return account
    return candidates[-1]


# ===== 主入口 =====

def cursor_key(policy: str, region: Optional[str]) -> str:
    return "%s|%s" % (normalize_policy(policy), str(region or "merged").lower())


def select(
    pool: Sequence[Any],
    policy: Optional[str] = None,
    *,
    session_id: Optional[str] = None,
    state: Optional[PolicyState] = None,
    now: Optional[float] = None,
    region: Optional[str] = None,
    credit_floor: float = 0.0,
    sticky_ttl: float = 0.0,
    prefer_expiring: bool = False,
    expiring_soon: float = 0.0,
    idle_weight_per_hour: float = 0.5,
    idle_weight_max: float = 5.0,
    rng: Optional[random.Random] = None,
    current_account_id: Optional[str] = None,
) -> Selection:
    """从 pool 里选一个账号。

    pool 是「全部账号」，本函数自己按 region / credit_floor / 可用性筛选。
    选不到时返回 Selection(account=None, reason='empty')。
    """
    key = normalize_policy(policy)
    ts = time.time() if now is None else float(now)

    candidates = scope(pool, region=region, credit_floor=credit_floor, now=ts)
    if not candidates:
        return Selection(account=None, policy=key, reason="empty", candidates=0)

    by_id: Dict[str, Any] = {account_id(a): a for a in candidates}

    # ① 粘性：命中的号如果还可用，就继续用它
    sticky_state = state if state is not None else PolicyState()
    if session_id:
        held = sticky_state.get_sticky(session_id, now=ts, ttl=sticky_ttl)
        if held and held in by_id:
            sticky_state.set_sticky(session_id, held, now=ts)
            return Selection(account=by_id[held], policy=key, reason="sticky-hit",
                             candidates=len(candidates))
        if held:
            # 原来绑的号不可用了，解绑后重新挑
            sticky_state.set_sticky(session_id, None, now=ts)

    # ② 显式指定了当前账号（面板「切换到这个号」）且它可用
    if current_account_id and str(current_account_id) in by_id and not session_id:
        chosen = by_id[str(current_account_id)]
        return Selection(account=chosen, policy=key, reason="current",
                         candidates=len(candidates))

    if prefer_expiring:
        candidates = order_prefer_expiring(candidates, ts, expiring_soon)

    # ③ 按策略挑
    reason = key
    chosen: Any = None

    if key == "priority":
        chosen = order_priority(candidates, ts)[0]
        reason = "priority"
    elif key == "round-robin":
        cursor = cursor_key(key, region)
        offset = int(sticky_state.cursors.get(cursor, 0))
        ordered = order_round_robin(candidates, offset)
        chosen = ordered[0]
        index = [account_id(a) for a in sorted(candidates, key=account_id)].index(account_id(chosen))
        sticky_state.cursors[cursor] = (index + 1) % len(candidates)
        reason = "round-robin"
    elif key == "balanced":
        chosen = weighted_choice(candidates, ts,
                                 idle_weight_per_hour=idle_weight_per_hour,
                                 idle_weight_max=idle_weight_max, rng=rng)
        reason = "balanced"
    else:  # sticky
        if session_id:
            chosen = weighted_choice(candidates, ts,
                                     idle_weight_per_hour=idle_weight_per_hour,
                                     idle_weight_max=idle_weight_max, rng=rng)
            reason = "sticky-new"
        else:
            # 没有 session（例如一次性请求）时退回均衡，避免所有请求压在一个号上
            chosen = weighted_choice(candidates, ts,
                                     idle_weight_per_hour=idle_weight_per_hour,
                                     idle_weight_max=idle_weight_max, rng=rng)
            reason = "balanced"

    if chosen is None:
        chosen = candidates[0]
        reason = "fallback"

    # ④ 写下粘性绑定：除了 round-robin/balanced 这类「故意换号」的策略，其余都绑住
    if session_id and key in ("sticky", "priority"):
        sticky_state.set_sticky(session_id, account_id(chosen), now=ts)

    if len(candidates) == 1:
        reason = "fallback"

    return Selection(account=chosen, policy=key, reason=reason, candidates=len(candidates))


# ===== 面板展示用 =====

def preview(pool: Sequence[Any], policy: Optional[str] = None, *,
            state: Optional[PolicyState] = None, now: Optional[float] = None,
            region: Optional[str] = None, credit_floor: float = 0.0,
            idle_weight_per_hour: float = 0.5, idle_weight_max: float = 5.0,
            limit: int = 5) -> Dict[str, Any]:
    """池健康视图：下一个会轮到谁、每个号为什么排在那个位置。"""
    key = normalize_policy(policy)
    ts = time.time() if now is None else float(now)
    candidates = scope(pool, region=region, credit_floor=credit_floor, now=ts)

    if key == "round-robin":
        st = state if state is not None else PolicyState()
        ordered = order_round_robin(candidates, int(st.cursors.get(cursor_key(key, region), 0)))
    elif key == "balanced":
        st = state if state is not None else PolicyState()
        ordered = order_balanced(candidates, ts, idle_weight_per_hour, idle_weight_max)
    else:
        ordered = order_priority(candidates, ts)

    def row(account: Any, rank: int) -> Dict[str, Any]:
        idle = _idle_seconds(account, ts)
        return {
            "rank": rank,
            "id": account_id(account),
            "name": str(getattr(account, "name", "") or ""),
            "region": region_of(account),
            "credits": _num(account, "credits"),
            "energy": _num(account, "energy"),
            "idle_seconds": None if idle == float("inf") else round(idle, 1),
            "idle_text": "从未使用" if idle == float("inf") else "%.1f 小时前" % (idle / 3600.0),
            "last_used": _num(account, "last_used"),
            "requests": int(_num(account, "request_count")),
        }

    return {
        "policy": key,
        "policy_label": POLICY_LABELS.get(key, key),
        "policy_note": POLICY_NOTES.get(key, ""),
        "region": str(region or "merged"),
        "candidates": len(candidates),
        "next": (row(ordered[0], 1) if ordered else None),
        "order": [row(a, i + 1) for i, a in enumerate(ordered[:max(1, int(limit))])],
        "policies": [{"key": p, "label": POLICY_LABELS[p], "note": POLICY_NOTES[p]} for p in POLICIES],
    }


def describe(policy: Optional[str] = None) -> Dict[str, Any]:
    key = normalize_policy(policy)
    return {
        "policies": [{"key": p, "label": POLICY_LABELS[p], "note": POLICY_NOTES[p]} for p in POLICIES],
        "current": key,
        "current_label": POLICY_LABELS.get(key, key),
        "default": DEFAULT_POLICY,
    }


if __name__ == "__main__":   # pragma: no cover - 手工排查用
    class _A:
        def __init__(self, i, credits, last, domain="www.codebuddy.cn"):
            self.id = i
            self.name = i
            self.credits = credits
            self.energy = 0
            self.last_used = last
            self.active = True
            self.breaker_until = 0
            self.cooldown_until = 0
            self.request_count = 0
            self.domain = domain

    _pool = [_A("a", 100, 0), _A("b", 900, 0), _A("c", 50, time.time() - 7200)]
    for _p in POLICIES:
        _sel = select(_pool, _p, session_id="s1", state=PolicyState())
        print("%-12s -> %s (%s)" % (_p, _sel.account_id, REASON_LABELS.get(_sel.reason, _sel.reason)))
