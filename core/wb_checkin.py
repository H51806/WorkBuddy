#!/usr/bin/env python3
"""
WorkBuddy 每日签到 / 补签（Catch-up）+ 收益账本
================================================
协议来源：dsh-workbuddy-connect 插件 src/checkin.ts 与 src/checkin-scheduler.ts
（已在本仓库 extracted_connect/ 下核对），字段与判定规则逐条对齐：

- 端点：国内 https://www.workbuddy.cn/v2/billing/meter/daily-checkin
        国际 https://www.workbuddy.ai/v2/billing/meter/daily-checkin
- 头：Authorization: Bearer <accessToken>、Content-Type、Accept、Origin、
      Referer: {origin}/profile/growth-center、User-Agent: CLI/2.63.2 CodeBuddy/2.63.2、
      X-CodeBuddy-Request: 1；国际额外 X-Domain: www.workbuddy.ai、X-No-Enterprise-Id: 1、
      Accept-Language: en-US；国内 Accept-Language: zh-CN
- 设备指纹：X-Machine-ID / X-Session-ID = sha256("wb2a:" + purpose + ":" + uid)[:36]
- 判定：已签到 = HTTP 400 且 code == 10001 或 message 含「已签到」/「今天已签到」
        成功 = HTTP 200 且 code 为 0/200，并从响应里抽取积分数量
- 日期口径：UTC+8 的 YYYY-MM-DD（getUtc8DateString）
- Catch-up：每账号持久化 {lastDate, lastAt, status, amount, message}；
        今天已 claimed 且非 force 时跳过；正常档只在「过了该账号今天的
        签到时点」后才执行，Catch-up 档忽略时点立即补。

本模块只做两件事：签到，以及把签到/任务写入收益账本日志
（wb_data/tasks_log.jsonl，格式与 wb_tasks._log_task 完全一致）。
"""

import hashlib
import json
import logging
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ===== 路径与常量 =====
BASE_DIR = Path(__file__).parent.parent  # 仓库根（本文件在 core/ 下）
DATA_DIR = BASE_DIR / "data"
WB_DATA_DIR = BASE_DIR / "wb_data"
CHECKIN_STATE_FILE = DATA_DIR / "checkin_state.json"
TASKS_LOG = WB_DATA_DIR / "tasks_log.jsonl"

CN_TZ = timezone(timedelta(hours=8))
CN_URL = "https://www.workbuddy.cn/v2/billing/meter/daily-checkin"
GLOBAL_URL = "https://www.workbuddy.ai/v2/billing/meter/daily-checkin"
ORIGIN_CN = "https://www.workbuddy.cn"
ORIGIN_GLOBAL = "https://www.workbuddy.ai"
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
STATE_VERSION = 1
LOG_KIND = "checkin"

# 正常档的签到时点窗口（UTC+8 当天分钟数），Catch-up 档忽略
OWN_TIME_FROM_MIN = 8 * 60
OWN_TIME_TO_MIN = 20 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wb-checkin")


# ===== 时间与工具 =====
def now_ts() -> int:
    return int(time.time())


def utc8_date(ts: Optional[int] = None) -> str:
    """UTC+8 的 YYYY-MM-DD（与 checkin.ts 的 getUtc8DateString 一致）。"""
    value = now_ts() if ts is None else int(ts)
    return datetime.fromtimestamp(value, CN_TZ).strftime("%Y-%m-%d")


def utc8_minute() -> int:
    d = datetime.fromtimestamp(now_ts(), CN_TZ)
    return d.hour * 60 + d.minute


def stable_id(uid: str, purpose: str) -> str:
    """与 checkin.ts 的 deriveAccountStableId 一致：sha256("wb2a:purpose:uid")[:36]。"""
    raw = ("wb2a:" + str(purpose) + ":" + str(uid)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:36]


def realm_of(acc: Dict[str, Any], override: Optional[str] = None) -> str:
    want = str(override or "").strip().lower()
    if want in ("cn", "global"):
        return want
    realm = str((acc or {}).get("realm") or "").strip().lower()
    if realm in ("cn", "global"):
        return realm
    domain = str((acc or {}).get("domain") or "").lower()
    if "workbuddy.ai" in domain or domain.endswith(".ai"):
        return "global"
    return "cn"


def _load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("读取 %s 失败: %s", path, e)
        return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ===== 签到状态 =====
def load_state() -> Dict[str, Any]:
    raw = _load_json(CHECKIN_STATE_FILE, {})
    if not isinstance(raw, dict):
        raw = {}
    state: Dict[str, Any] = {
        "version": STATE_VERSION,
        "updated": int(raw.get("updated") or 0),
        "last_run": raw.get("last_run") or {},
        "accounts": raw.get("accounts") if isinstance(raw.get("accounts"), dict) else {},
    }
    return state


def save_state(state: Dict[str, Any]) -> None:
    state["version"] = STATE_VERSION
    state["updated"] = now_ts()
    _save_json(CHECKIN_STATE_FILE, state)


def account_state_rows(accounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给面板用：每个账号今天的签到状态。"""
    state = load_state()
    today = utc8_date()
    rows: List[Dict[str, Any]] = []
    for acc in accounts:
        uid = str(acc.get("uid") or "")
        cur = state.get("accounts", {}).get(uid) or {}
        rows.append({
            "uid": uid,
            "name": str(acc.get("name") or uid),
            "realm": realm_of(acc),
            "last_date": cur.get("lastDate") or "",
            "last_at": cur.get("lastAt") or 0,
            "status": cur.get("status") or "unknown",
            "amount": cur.get("amount"),
            "message": cur.get("message") or "",
            "today_done": bool(cur.get("lastDate") == today and cur.get("status") == "claimed"),
            "own_time_passed": past_own_time(uid, today),
            "own_time": own_time_text(uid, today),
            "file": cur.get("file") or "",
        })
    return rows


def past_own_time(uid: str, day: Optional[str] = None) -> bool:
    """该账号今天的签到时点是否已过。

    checkin-scheduler.ts 里每个账号有自己当日配置的分钟点；这里用
    sha256(uid + "|" + 当天) 取一个 08:00-20:00 之间的确定值作为等价实现，
    保证同一账号同一天时点稳定、不同账号错峰。
    """
    day = day or utc8_date()
    digest = hashlib.sha256((str(uid) + "|" + str(day)).encode("utf-8")).hexdigest()
    span = max(OWN_TIME_TO_MIN - OWN_TIME_FROM_MIN, 1)
    minute = OWN_TIME_FROM_MIN + int(digest[:8], 16) % span
    return utc8_minute() >= minute


def own_time_text(uid: str, day: Optional[str] = None) -> str:
    day = day or utc8_date()
    digest = hashlib.sha256((str(uid) + "|" + str(day)).encode("utf-8")).hexdigest()
    span = max(OWN_TIME_TO_MIN - OWN_TIME_FROM_MIN, 1)
    minute = OWN_TIME_FROM_MIN + int(digest[:8], 16) % span
    return "%02d:%02d" % (minute // 60, minute % 60)


# ===== HTTP =====
def _parse_body(txt: str) -> Dict[str, Any]:
    try:
        data = json.loads(txt)
        if isinstance(data, dict):
            return data
        return {"data": data}
    except Exception:
        return {"raw": (txt or "")[:500]}


def _request(url: str, headers: Dict[str, str], body: Optional[Dict[str, Any]] = None,
             timeout: int = 20) -> Tuple[int, Dict[str, Any]]:
    payload = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _parse_body(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        return e.code, _parse_body(e.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return -1, {"err": repr(e)}


def build_headers(acc: Dict[str, Any], realm: Optional[str] = None) -> Dict[str, str]:
    realm = realm_of(acc, realm)
    origin = ORIGIN_GLOBAL if realm == "global" else ORIGIN_CN
    headers: Dict[str, str] = {
        "Authorization": "Bearer " + str(acc.get("token") or ""),
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": origin + "/profile/growth-center",
        "User-Agent": CLIENT_UA,
        "X-CodeBuddy-Request": "1",
        "Accept-Language": "en-US" if realm == "global" else "zh-CN",
    }
    uid = str(acc.get("uid") or "")
    if uid:
        headers["X-Machine-ID"] = stable_id(uid, "machine")
        headers["X-Session-ID"] = stable_id(uid, "session")
    if realm == "global":
        headers["X-Domain"] = "www.workbuddy.ai"
        headers["X-No-Enterprise-Id"] = "1"
    else:
        headers["X-Domain"] = "www.workbuddy.cn"
    enterprise = str(acc.get("enterpriseId") or acc.get("enterprise_id") or "")
    if enterprise:
        headers["X-Enterprise-Id"] = enterprise
    return headers


AMOUNT_KEYS = ("amount", "credits", "credit", "points", "score", "reward", "integral", "value", "coin")


def _extract_amount(body: Dict[str, Any]) -> Optional[int]:
    """从响应里尽力抽取积分数量；抽不到返回 None（不猜）。"""
    if not isinstance(body, dict):
        return None
    scopes: List[Dict[str, Any]] = [body]
    for key in ("data", "result", "extra"):
        sub = body.get(key)
        if isinstance(sub, dict):
            scopes.append(sub)
    for scope in scopes:
        for key in AMOUNT_KEYS:
            if key in scope:
                try:
                    num = int(float(scope.get(key)))
                except Exception:
                    continue
                if num > 0:
                    return num
    return None


def classify(status_code: int, body: Dict[str, Any]) -> Tuple[str, Optional[int], str]:
    """把上游响应归到 claimed / already-claimed / no-campaign / error。"""
    code = None
    message = ""
    if isinstance(body, dict):
        code = body.get("code")
        message = str(body.get("message") or body.get("msg") or "")
    text = message + " " + str((body or {}).get("raw") or "")
    amount = _extract_amount(body)
    already = status_code == 400 and (str(code) == "10001" or "已签到" in text or "今天已签到" in text)
    if already:
        return "already-claimed", amount, message or "今天已签到"
    if status_code == 200 and (code is None or str(code) in ("0", "200")):
        return "claimed", amount, message or ("签到成功" + ("，+" + str(amount) + " 积分" if amount else ""))
    if status_code == 200:
        return "no-campaign", amount, message or ("上游返回 code=" + str(code))
    if status_code == -1:
        return "error", None, message or str((body or {}).get("err") or "网络错误")
    return "error", amount, message or ("HTTP " + str(status_code))


def check_in(acc: Dict[str, Any], realm: Optional[str] = None, dry_run: bool = False,
             timeout: int = 20) -> Dict[str, Any]:
    """单账号签到。"""
    uid = str(acc.get("uid") or "")
    realm = realm_of(acc, realm)
    today = utc8_date()
    base = {
        "uid": uid,
        "name": str(acc.get("name") or uid),
        "realm": realm,
        "date": today,
        "timestamp": now_ts(),
        "status": "error",
        "amount": None,
        "message": "",
        "url": GLOBAL_URL if realm == "global" else CN_URL,
    }
    if dry_run:
        base.update({"status": "dry-run", "message": "演练模式：没有真正请求上游"})
        return base
    if not uid or not acc.get("token"):
        base["message"] = "缺少 uid 或 accessToken"
        return base
    status_code, body = _request(base["url"], build_headers(acc, realm), {}, timeout)
    status, amount, message = classify(status_code, body)
    base.update({"status": status, "amount": amount, "message": message, "http": status_code})
    return base


# ===== 批量签到 / Catch-up =====
def check_in_accounts(accounts: List[Dict[str, Any]], catch_up: bool = True,
                      dry_run: bool = False, force: bool = False,
                      timeout: int = 20) -> Dict[str, Any]:
    """批量签到。

    catch_up=True  → 补签档：忽略时点，只要今天还没签到就立刻补
    catch_up=False → 正常档：先看该账号今天的签到时点是否已过
    force=True     → 连「今天已 claimed」也再打一次（用于验证协议/排查）
    """
    state = load_state()
    today = utc8_date()
    rows: List[Dict[str, Any]] = []
    claimed = 0
    already = 0
    skipped = 0
    failed = 0
    amount_total = 0

    for acc in accounts:
        uid = str(acc.get("uid") or "")
        row: Dict[str, Any] = {
            "uid": uid, "name": str(acc.get("name") or uid), "realm": realm_of(acc),
            "date": today, "status": "skipped", "amount": None, "message": "", "http": None,
        }
        cur = state.get("accounts", {}).get(uid) or {}
        if not force and cur.get("lastDate") == today and cur.get("status") == "claimed":
            row.update({"status": "skipped", "amount": cur.get("amount"),
                        "message": "今天已签到" + ("，+" + str(cur.get("amount")) + " 积分" if cur.get("amount") else "")})
            skipped += 1
            rows.append(row)
            continue
        if not force and not catch_up and not past_own_time(uid, today):
            row.update({"status": "skipped",
                        "message": "未到该账号今天的签到时点（" + own_time_text(uid, today) + "，补签档可忽略）"})
            skipped += 1
            rows.append(row)
            continue

        res = check_in(acc, dry_run=dry_run, timeout=timeout)
        row.update({
            "status": res.get("status"),
            "amount": res.get("amount"),
            "message": res.get("message"),
            "http": res.get("http"),
            "date": res.get("date") or today,
        })
        status = str(res.get("status") or "error")
        if status == "claimed":
            claimed += 1
            if res.get("amount"):
                amount_total += int(res.get("amount") or 0)
        elif status == "already-claimed":
            already += 1
        elif status == "dry-run":
            pass
        elif status == "no-campaign":
            pass
        else:
            failed += 1

        if not dry_run:
            entry = {
                "lastDate": res.get("date") or today,
                "lastAt": res.get("timestamp") or now_ts(),
                "status": status,
                "amount": res.get("amount"),
                "message": res.get("message"),
                "realm": row["realm"],
                "name": row["name"],
            }
            state.setdefault("accounts", {})[uid] = entry
            if status == "claimed":
                append_log({
                    "kind": LOG_KIND,
                    "uid": uid,
                    "name": row["name"],
                    "realm": row["realm"],
                    "status": status,
                    "ok": True,
                    "effect": "已签到",
                    "amount": res.get("amount"),
                    "reward": ("+" + str(res.get("amount")) + "c") if res.get("amount") else "",
                    "msg": res.get("message"),
                })
        rows.append(row)

    if not dry_run:
        state["last_run"] = {
            "at": now_ts(),
            "day": today,
            "catch_up": bool(catch_up),
            "force": bool(force),
            "claimed": claimed,
            "already": already,
            "skipped": skipped,
            "failed": failed,
            "amount": amount_total,
        }
        save_state(state)

    return {
        "ok": True,
        "today": today,
        "catch_up": bool(catch_up),
        "force": bool(force),
        "dry_run": bool(dry_run),
        "accounts": rows,
        "claimed": claimed,
        "already": already,
        "skipped": skipped,
        "failed": failed,
        "amount": amount_total,
        "note": "签到只影响每日连续签到积分，不会改变账号池的可用性判定。",
    }


# ===== 收益账本日志 =====
def append_log(entry: Dict[str, Any]) -> None:
    """与 wb_tasks._log_task 完全一致的写入格式（ts 为本地时间字符串）。"""
    TASKS_LOG.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(entry or {})
    payload["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(TASKS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("写收益账本失败: %s", e)


def read_log(limit: int = 500, uid: Optional[str] = None,
             day: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取收益账本尾部（默认最近 500 条，最新在前）。"""
    if not TASKS_LOG.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with open(TASKS_LOG, "r", encoding="utf-8", errors="replace") as f:
            for line in deque(f, maxlen=max(int(limit), 1) * 4):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if not isinstance(item, dict):
                    continue
                if uid and str(item.get("uid") or "") != str(uid):
                    continue
                if day and str(item.get("ts") or "")[:10] != str(day):
                    continue
                rows.append(item)
    except Exception as e:
        log.warning("读收益账本失败: %s", e)
        return []
    rows.reverse()
    return rows[:max(int(limit), 1)]


def log_stats(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    exists = TASKS_LOG.exists()
    size = TASKS_LOG.stat().st_size if exists else 0
    return {
        "path": str(TASKS_LOG),
        "exists": exists,
        "size_bytes": int(size),
        "rows": len(rows) if rows is not None else None,
    }


def ledger(days: int = 7, rows: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """按天汇总的收益账本（含签到积分与任务完成数）。"""
    if rows is None:
        rows = read_log(limit=3000)
    days = max(int(days), 1)
    buckets: Dict[str, Dict[str, Any]] = {}
    for i in range(days):
        d = utc8_date(now_ts() - i * 86400)
        dt = datetime.strptime(d, "%Y-%m-%d")
        buckets[d] = {
            "date": d,
            "weekday": "周" + "一二三四五六日"[dt.weekday()],
            "entries": 0,
            "checkin_count": 0,
            "checkin_amount": 0,
            "task_count": 0,
            "task_ok": 0,
            "task_skipped": 0,
            "claimed": 0,
            "rewards": [],
        }
    for item in rows:
        day = str(item.get("ts") or "")[:10]
        b = buckets.get(day)
        if not b:
            continue
        b["entries"] += 1
        kind = str(item.get("kind") or "task")
        ok = bool(item.get("ok"))
        if kind == LOG_KIND:
            b["checkin_count"] += 1
            try:
                b["checkin_amount"] += int(item.get("amount") or 0)
            except Exception:
                pass
            if ok:
                b["claimed"] += 1
        else:
            if item.get("skipped"):
                b["task_skipped"] += 1
            else:
                b["task_count"] += 1
                if ok:
                    b["task_ok"] += 1
            if str(item.get("effect") or "") == "已领奖":
                b["claimed"] += 1
        reward = str(item.get("reward") or "")
        code = str(item.get("task_code") or item.get("code") or "")
        if reward and code:
            pair = {"code": code, "reward": reward}
            if pair not in b["rewards"]:
                b["rewards"].append(pair)
    return [buckets[utc8_date(now_ts() - i * 86400)] for i in range(days)]


def summary(rows: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    if rows is None:
        rows = read_log(limit=3000)
    total_amount = 0
    checkin_count = 0
    task_count = 0
    task_ok = 0
    task_skipped = 0
    for item in rows:
        kind = str(item.get("kind") or "task")
        if kind == LOG_KIND:
            checkin_count += 1
            try:
                total_amount += int(item.get("amount") or 0)
            except Exception:
                pass
        else:
            if item.get("skipped"):
                task_skipped += 1
            else:
                task_count += 1
                if bool(item.get("ok")):
                    task_ok += 1
    return {
        "entries": len(rows),
        "checkin_count": checkin_count,
        "checkin_amount": total_amount,
        "task_count": task_count,
        "task_ok": task_ok,
        "task_skipped": task_skipped,
    }


def report(days: int = 7, limit: int = 300, uid: Optional[str] = None,
           day: Optional[str] = None) -> Dict[str, Any]:
    """面板 GET /panel/api/checkin 的一次性数据包。"""
    state = load_state()
    rows = read_log(limit=limit, uid=uid, day=day)
    return {
        "ok": True,
        "today": utc8_date(),
        "state": {
            "updated": state.get("updated") or 0,
            "last_run": state.get("last_run") or {},
            "accounts": state.get("accounts") or {},
            "file": str(CHECKIN_STATE_FILE),
        },
        "log": log_stats(rows),
        "summary": summary(rows),
        "ledger": ledger(days=days, rows=rows),
        "rows": rows,
        "days": days,
        "note": ("签到与任务都会写进同一个收益账本（wb_data/tasks_log.jsonl）。"
                 "签到走 /v2/billing/meter/daily-checkin，与任务中心的行为上报互不影响。"),
    }


if __name__ == "__main__":
    import sys
    argv = sys.argv[1:]
    if argv and argv[0] == "state":
        print(json.dumps(account_state_rows([]), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(report(), ensure_ascii=False, indent=2)[:4000])
