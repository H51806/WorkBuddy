"""
wb_live_credits.py — 实时积分查询（直连上游 billing meter API）

用途：绕过 credits_history.json 快照，直接问上游 get-user-resource 拿当前余额。
上游返回的是"活跃 + 将过期"的积分包列表，remain 累加 = 当前余额。

依赖：wb_auth 拿 accessToken。
"""
import base64
import datetime as _dt
import json
import os
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BILL_BASE = "https://www.codebuddy.cn"
UA = "Mozilla/5.0"

# productCode: 与上游客户端一致
# Status: 0=active, 3=即将过期(但还可用)
_QUERY_STATUS = [0, 3]
_QUERY_PRODUCT = "p_tcaca"


def _load_access_tokens():
    """扫描 auths/*.json，返回 [{uid, phone, nickname, token}]。"""
    auth_dir = os.path.join(BASE_DIR, "auths")
    out = []
    if not os.path.isdir(auth_dir):
        return out
    for f in sorted(os.listdir(auth_dir)):
        if not f.endswith(".json"):
            continue
        p = os.path.join(auth_dir, f)
        try:
            with open(p, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        auth = d.get("auth") or {}
        token = auth.get("accessToken") or d.get("accessToken")
        if not token:
            continue
        uid = os.path.basename(f).replace("workbuddy-", "").replace(".json", "")
        if not uid:
            uid = d.get("metadata", {}).get("source", f)
        # 从 JWT 解析手机号和 nickname
        phone = d.get("phone") or ""
        nickname = d.get("nickname") or ""
        # account 字段可能是 dict {uid, enterpriseId, nickname} 也可能是字符串
        acc_field = d.get("account") or ""
        if isinstance(acc_field, dict):
            if not nickname:
                nickname = str(acc_field.get("nickname") or "")
            if not uid:
                uid = str(acc_field.get("uid") or "")
        elif isinstance(acc_field, str) and acc_field and not nickname:
            nickname = acc_field
        # 从 JWT payload 解析
        try:
            jwt = token.split(".")[1]
            jwt += "=" * (-len(jwt) % 4)
            payload = json.loads(base64.urlsafe_b64decode(jwt).decode())
            phone = payload.get("preferred_username") or phone
            # name 字段实测是 dict {uid, enterpriseId, nickname}，不是纯字符串
            name_field = payload.get("name") or ""
            if isinstance(name_field, dict):
                nickname = nickname or str(name_field.get("nickname") or "")
            elif isinstance(name_field, str):
                nickname = nickname or name_field
        except Exception:
            pass
        out.append({"uid": uid, "phone": phone, "nickname": nickname, "token": token})
    return out


def _post(token, path, body):
    headers = {
        "User-Agent": UA,
        "x-client-platform": "web",
        "Accept": "*/*",
        "Content-Type": "application/json;charset=UTF-8",
        "Authorization": f"Bearer {token}",
        "Origin": BILL_BASE,
        "Referer": BILL_BASE + "/",
    }
    req = urllib.request.Request(
        BILL_BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw[:400]}


def query_user_resource(token):
    """
    POST /v2/billing/meter/get-user-resource

    返回 {remain, total, expiring, packages: [{name, remain, size, end_time}], total_count}
    """
    now = _dt.datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    body = {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": _QUERY_PRODUCT,
        "Status": list(_QUERY_STATUS),
        "PackageEndTimeRangeBegin": now.strftime(fmt),
        "PackageEndTimeRangeEnd": (now + _dt.timedelta(days=365 * 101)).strftime(fmt),
    }
    status, d = _post(token, "/v2/billing/meter/get-user-resource", body)
    if status != 200 or d.get("code") != 0:
        return {
            "remain": 0,
            "total": 0,
            "expiring": 0,
            "packages": [],
            "total_count": 0,
            "_error": f"HTTP {status} code={d.get('code')} msg={d.get('msg')}",
            "_raw": d,
        }
    data = d.get("data", {}).get("Response", {}).get("Data", {}) or {}
    accts = data.get("Accounts") or []
    remain = total = expiring = 0
    packages = []
    now_ts = now.timestamp()
    for a in accts:
        # packageRemainUsed 逻辑：CycleCapacitySize > 0 时优先 Cycle*，否则 Capacity*
        if a.get("CycleCapacitySize", 0) > 0:
            r = a.get("CycleCapacityRemain", 0) or 0
            sz = a.get("CycleCapacitySize", 0) or 0
        else:
            r = a.get("CapacityRemain", 0) or 0
            sz = a.get("CapacitySize", 0) or 0
        if r < 0:
            r = 0
        if r > sz:
            r = sz
        remain += r
        total += sz
        # 到期时间
        end_str = a.get("CycleEndTime") or ""
        end_ts = None
        try:
            if end_str:
                end_ts = _dt.datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            pass
        # 快过期：7 天内
        if r > 0 and end_ts and now_ts < end_ts <= now_ts + 7 * 86400:
            expiring += r
        packages.append({
            "name": a.get("PackageName", ""),
            "remain": r,
            "size": sz,
            "used": (a.get("CycleCapacityUsed") or a.get("CapacityUsed") or 0),
            "end_time": end_str,
            "status": a.get("Status"),
            "type": a.get("PackageType"),
        })
    return {
        "remain": int(remain),
        "total": int(total),
        "expiring": int(expiring),
        "packages": packages,
        "total_count": int(data.get("TotalCount") or len(accts)),
        "total_dosage": int(data.get("TotalDosage") or 0),
    }


def query_all():
    """对所有 auths 账号查询实时余额。返回 {account_key: {...}}。"""
    accounts = _load_access_tokens()
    out = {}
    for acc in accounts:
        try:
            result = query_user_resource(acc["token"])
        except Exception as e:
            result = {"_error": str(e), "remain": 0, "total": 0, "expiring": 0, "packages": []}
        result["uid"] = acc["uid"]
        result["phone"] = acc["phone"]
        result["nickname"] = acc["nickname"]
        result["queried_at"] = _dt.datetime.now().isoformat(timespec="seconds")
        out[acc["uid"]] = result
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        for acc in _load_access_tokens():
            print(f"  {acc['uid']}  phone={acc['phone']}  nick={acc['nickname']}")
    else:
        for uid, r in query_all().items():
            print(f"{uid}")
            print(f"  phone:     {r.get('phone')}")
            print(f"  nickname:  {r.get('nickname')}")
            print(f"  remain:    {r.get('remain')}")
            print(f"  total:     {r.get('total')}")
            print(f"  expiring:  {r.get('expiring')}  (7d)")
            print(f"  packages:  {len(r.get('packages', []))}")
            print(f"  _error:    {r.get('_error')}")
