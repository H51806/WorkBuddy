#!/usr/bin/env python3
"""
WorkBuddy OAuth 设备登录 / auths 写入模块
==========================================

用途：用 Python 标准库重写 WorkBuddy CN OAuth 设备授权流程，把成功登录后获得的
accessToken / refreshToken / uid 落盘为 auths/workbuddy-<uid>.json，供：
  - wb_gateway.py 账号池
  - wb_tasks.py 成长任务引擎
  - 保活/刷新 token 模块
统一读取。

安全边界：
  - 不依赖上游 Go binary；不复制 login.go 代码结构
  - 只使用上游公开的 auth/state、auth/token、login/account 三个接口路径
  - uid 会做白名单校验，防止路径穿越
  - auths/*.json 包含明文 token，请放在本机目录中妥善保管
"""

import json
import time
import logging
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, Tuple

BASE_DIR = Path(__file__).parent
AUTH_DIR = BASE_DIR / "auths"
AUTH_SESSION_FILE = BASE_DIR / "data" / "login_sessions.json"

CHAT_BASE = "https://copilot.tencent.com"
BILL_BASE = "https://www.codebuddy.cn"
WEB_BASE = "https://www.workbuddy.cn"
ORIGIN_CN = "https://www.codebuddy.cn"
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
LOGINS = {}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("wb-auth")


def _write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _read_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _headers(origin: str = ORIGIN_CN) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin.rstrip("/") + "/",
        "User-Agent": CLIENT_UA,
    }


def _url_request(method: str, url: str, body: Dict[str, Any] = None, bearer: str = "", origin: str = ORIGIN_CN) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = _headers(origin)
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            txt = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(txt)
            except Exception:
                return resp.status, {"raw": txt[:500]}
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"raw": txt[:500]}
    except Exception as e:
        return -1, {"error": str(e)}


def _unwrap(data: Dict[str, Any]) -> Dict[str, Any]:
    """兼容 {code,msg,data} 与裸对象。"""
    if isinstance(data, dict) and "data" in data and ("code" in data or "msg" in data):
        if data.get("code") not in (0, "0"):
            raise RuntimeError(data.get("msg") or data.get("message") or f"code={data.get('code')}")
        return data.get("data") or {}
    return data


def _valid_uid(uid: str) -> bool:
    if not uid or len(uid) > 64:
        return False
    return all(c.isalnum() or c in "-_" for c in uid)


def _auth_file_for_uid(uid: str) -> Path:
    if not _valid_uid(uid):
        raise ValueError(f"非法 uid，拒绝写入文件: {uid!r}")
    return AUTH_DIR / f"workbuddy-{uid}.json"


def _make_auth_payload(token_info: Dict[str, Any], account_info: Dict[str, Any], realm: str = "cn") -> Dict[str, Any]:
    uid = str(account_info.get("uid") or account_info.get("user_id") or "")
    if not uid:
        raise ValueError("登录完成但无法获取 uid")
    expires_in = int(token_info.get("expiresIn") or token_info.get("expires_in") or 0)
    expires_at = int(time.time()) + expires_in
    return {
        "realm": realm,
        "account": {
            "uid": uid,
            "enterpriseId": account_info.get("enterpriseId") or account_info.get("enterprise_id") or "",
            "nickname": account_info.get("nickname") or "",
        },
        "auth": {
            "accessToken": token_info.get("accessToken") or token_info.get("access_token") or "",
            "refreshToken": token_info.get("refreshToken") or token_info.get("refresh_token") or "",
            "expiresAt": expires_at,
            "expiresIn": expires_in,
            "domain": token_info.get("domain") or "",
        },
        "metadata": {
            "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source": "wb_auth_device_login",
        },
    }


def login_start(realm: str = "cn") -> Dict[str, Any]:
    """发起设备授权，返回浏览器需要打开的 authUrl。"""
    if realm not in ("cn", "global"):
        realm = "cn"
    base = "https://www.workbuddy.ai" if realm == "global" else CHAT_BASE
    origin = "https://www.workbuddy.ai" if realm == "global" else ORIGIN_CN
    url = base + "/v2/plugin/auth/state?platform=CLI"
    status, raw = _url_request("POST", url, body={}, origin=origin)
    if status < 0:
        return {"ok": False, "error": f"请求 auth/state 失败: {raw.get('error')}"}
    try:
        data = _unwrap(raw)
    except Exception as e:
        return {"ok": False, "error": f"auth/state 返回异常: {e} {raw}"}
    state = data.get("state") or ""
    auth_url = data.get("authUrl") or data.get("auth_url") or ""
    if not state or not auth_url:
        return {"ok": False, "error": f"auth/state 缺少 state/authUrl: {data}"}
    LOGINS[state] = {"state": state, "realm": realm, "created": time.time()}
    _write_json(AUTH_SESSION_FILE, LOGINS)
    log.info("OAuth 登录已发起 realm=%s state=%s...", realm, state[:8])
    return {"ok": True, "url": auth_url, "state": state, "realm": realm}


def _restore_sessions():
    if not LOGINS:
        saved = _read_json(AUTH_SESSION_FILE, {}) or {}
        for state, sess in saved.items():
            if isinstance(sess, dict):
                LOGINS[state] = sess


def _get_session(state: str) -> Dict[str, Any] | None:
    _restore_sessions()
    sess = LOGINS.get(state)
    if not sess:
        return None
    # TTL 30 分钟，避免长期残留
    if time.time() - float(sess.get("created") or 0) > 1800:
        LOGINS.pop(state, None)
        _write_json(AUTH_SESSION_FILE, LOGINS)
        return None
    return sess


def daily_checkin(access_token: str, uid: str, enterprise_id: str = "", domain: str = "") -> Dict[str, Any]:
    """CN 每日签到；失败不阻断 auth 写入。"""
    headers = _headers()
    headers["Authorization"] = "Bearer " + access_token
    headers["X-User-Id"] = uid
    if enterprise_id:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    if domain:
        headers["X-Domain"] = domain
    req = urllib.request.Request(BILL_BASE + "/v2/billing/meter/daily-checkin", data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
            return {"ok": True, "body": body}
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            return {"ok": False, "body": json.loads(txt)}
        except Exception:
            return {"ok": False, "error": f"http {e.code}: {txt[:200]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def login_poll(state: str) -> Dict[str, Any]:
    """轮询登录结果；完成时落盘 auths/workbuddy-<uid>.json 并做幂等签到。"""
    sess = _get_session(state)
    if not sess:
        return {"ok": False, "error": "登录状态不存在或已过期，请重新点击添加账号", "status": 404}
    realm = sess.get("realm", "cn")
    base = "https://www.workbuddy.ai" if realm == "global" else CHAT_BASE
    origin = "https://www.workbuddy.ai" if realm == "global" else ORIGIN_CN

    token_status, token_raw = _url_request("GET", base + "/v2/plugin/auth/token?state=" + state, origin=origin)
    try:
        token_data = _unwrap(token_raw)
    except Exception:
        return {"ok": True, "done": False, "message": "等待登录...", "raw": token_raw}
    access_token = token_data.get("accessToken") or token_data.get("access_token") or ""
    if not access_token:
        return {"ok": True, "done": False, "message": "等待登录...", "raw": token_data}

    account_status, account_raw = _url_request("GET", base + "/v2/plugin/login/account?state=" + state, bearer=access_token, origin=origin)
    try:
        account_data = _unwrap(account_raw)
    except Exception:
        account_data = {}
    uid = str(account_data.get("uid") or account_data.get("user_id") or "")
    if not uid:
        return {"ok": False, "error": "登录完成但没有取到 uid，请重试", "raw": account_raw}
    if not _valid_uid(uid):
        return {"ok": False, "error": f"上游返回非法 uid，拒绝写入: {uid!r}"}

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    payload = _make_auth_payload(token_data, account_data, realm)
    _write_json(_auth_file_for_uid(uid), payload)
    nickname = account_data.get("nickname") or ""
    ent_id = account_data.get("enterpriseId") or account_data.get("enterprise_id") or ""
    checkin = daily_checkin(access_token, uid, ent_id, token_data.get("domain") or "") if realm == "cn" else {"ok": True, "message": "global realm 跳过 CN 签到"}

    LOGINS.pop(state, None)
    _write_json(AUTH_SESSION_FILE, LOGINS)
    log.info("OAuth 登录完成并写入 auth: uid=%s nickname=%s realm=%s", uid, nickname, realm)
    return {
        "ok": True,
        "done": True,
        "uid": uid,
        "nickname": nickname,
        "realm": realm,
        "file": str(_auth_file_for_uid(uid).relative_to(BASE_DIR)),
        "expires_at": payload["auth"]["expiresAt"],
        "refresh_token": bool(payload["auth"]["refreshToken"]),
        "checkin": checkin,
    }
