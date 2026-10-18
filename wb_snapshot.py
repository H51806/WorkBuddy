#!/usr/bin/env python3
"""
桌面快照发现与导入
==================
把「旧版/桌面端登录留下的快照」找出来，尽可能转成网关能用的 auths/<uid>.json。

快照形态（本仓库实测）：
1) wb_data/login_state_<名字>.json    Playwright storage_state：
   {"cookies":[{name,value,domain,expires,...}], "origins":[{origin,localStorage:[{name,value}]}]}
   其中 KEYCLOAK_IDENTITY 是 JWT，payload.sub 就是 uid；localStorage 里
   CODEBUDDY_IDE_SELECTED_ACCOUNT_ID 也是 uid。
2) wb_data/identity_<名字>.json       {"nickname","phone","fetched_at"}（昵称可能被
   页面文案污染，例如整句「参与对象：WorkBuddy 国内用户。」，这里会标记 name_suspect）
3) data/login_sessions.json           设备授权登录过程中的会话（wb_auth 维护）

诚实原则：**没有 API accessToken 的快照不会被硬塞进 auths/**。
只有网页会话 Cookie 的快照会在面板上标记为「只有网页 Cookie，请用设备授权登录扫码」，
绝不拿 Cookie 冒充 accessToken —— 那样网关请求会一直 401，用户却以为导入成功了。
"""

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
WB_DATA_DIR = BASE_DIR / "wb_data"
AUTH_DIR = BASE_DIR / "auths"
LOGIN_SESSIONS = DATA_DIR / "login_sessions.json"

SNAPSHOT_VERSION = 1
JWT_RE = re.compile(r"[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
UID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SUSPECT_CHARS = ("：", "。", "，", "\n")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("wb-snapshot")


def now_ts() -> int:
    return int(time.time())


def fmt_ts(value: Any) -> str:
    try:
        ts = int(value)
    except Exception:
        return ""
    if ts <= 0:
        return ""
    if ts > 99999999999:
        ts = int(ts / 1000)
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _load_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.warning("读取 %s 失败: %s", path, e)
        return default


def jwt_payload(token: str) -> Optional[Dict[str, Any]]:
    import base64
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return None
    seg = parts[1].replace("-", "+").replace("_", "/")
    pad = "=" * (-len(seg) % 4)
    try:
        raw = base64.b64decode(seg + pad).decode("utf-8", errors="replace")
        data = json.loads(raw)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _looks_like_access_token(token: str) -> bool:
    payload = jwt_payload(token)
    if not payload:
        return False
    keys = set(payload.keys())
    return bool(keys & {"sub", "uid", "user_id", "exp", "iat", "preferred_username", "scope"})


def _deep_collect(obj: Any, out: List[Tuple[str, str]], path: str = "", depth: int = 0) -> None:
    """递归找 JWT 形态的字符串。"""
    if depth > 8:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            _deep_collect(v, out, path + "/" + str(k), depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _deep_collect(v, out, path + "/[" + str(i) + "]", depth + 1)
    elif isinstance(obj, str):
        if len(obj) > 40:
            m = JWT_RE.search(obj)
            if m:
                out.append((path, m.group(0)))


def _clean_name(name: Any) -> Tuple[str, bool]:
    text = str(name or "").strip()
    suspect = False
    if len(text) > 20 or any(ch in text for ch in SUSPECT_CHARS):
        suspect = True
    return text, suspect


def auths_index() -> Dict[str, Dict[str, Any]]:
    """已有 auths/*.json 的索引：uid -> {file, realm, name, has_token, expires_at}。"""
    index: Dict[str, Dict[str, Any]] = {}
    if not AUTH_DIR.exists():
        return index
    for fp in sorted(AUTH_DIR.glob("*.json")):
        raw = _load_json(fp, {})
        if not isinstance(raw, dict):
            continue
        auth = raw.get("auth") or {}
        account = raw.get("account") or {}
        token = str(auth.get("accessToken") or auth.get("token") or "")
        uid = str(account.get("uid") or auth.get("uid") or "").strip()
        payload = jwt_payload(token) if token else None
        if not uid and payload:
            uid = str(payload.get("sub") or payload.get("uid") or "")
        if not uid:
            continue
        index[uid] = {
            "file": fp.name,
            "path": str(fp),
            "realm": str(raw.get("realm") or auth.get("realm") or ""),
            "name": str(account.get("nickname") or account.get("name") or ""),
            "phone": str(account.get("phone") or ""),
            "has_token": bool(token),
            "expires_at": (payload or {}).get("exp"),
            "expires_text": fmt_ts((payload or {}).get("exp")),
        }
    return index


def _candidate_from_login_state(fp: Path, raw: Dict[str, Any],
                                ident: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cookies = raw.get("cookies") if isinstance(raw.get("cookies"), list) else []
    origins = raw.get("origins") if isinstance(raw.get("origins"), list) else []
    row: Dict[str, Any] = {
        "file": fp.name,
        "path": str(fp),
        "kind": "login_state",
        "size": fp.stat().st_size,
        "mtime": int(fp.stat().st_mtime),
        "uid": "",
        "name": "",
        "phone": "",
        "realm": "cn",
        "logged_in": False,
        "cookies": len(cookies),
        "cookie_names": [],
        "cookie_expires_at": None,
        "cookie_expires_text": "",
        "cookie_expired": None,
        "token_found": False,
        "token_source": "",
        "name_suspect": False,
        "has_web_session": False,
        "note": "",
    }
    names: List[str] = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        cname = str(c.get("name") or "")
        value = str(c.get("value") or "")
        names.append(cname)
        domain = str(c.get("domain") or "")
        if "workbuddy.ai" in domain:
            row["realm"] = "global"
        if cname in ("session", "session_2", "KEYCLOAK_SESSION", "KEYCLOAK_IDENTITY"):
            row["has_web_session"] = True
        if cname == "KEYCLOAK_IDENTITY":
            payload = jwt_payload(value)
            if payload:
                sub = str(payload.get("sub") or "")
                if sub:
                    row["uid"] = sub
                row["cookie_expires_at"] = payload.get("exp")
                row["cookie_expires_text"] = fmt_ts(payload.get("exp"))
                row["cookie_expired"] = bool(payload.get("exp") and int(payload["exp"]) < now_ts())
        if _looks_like_access_token(value) and not row["token_found"]:
            row["token_found"] = True
            row["token_source"] = "cookie:" + cname
            payload = jwt_payload(value) or {}
            if not row["uid"]:
                row["uid"] = str(payload.get("sub") or payload.get("uid") or "")
    row["cookie_names"] = names[:24]

    for o in origins:
        if not isinstance(o, dict):
            continue
        origin = str(o.get("origin") or "")
        if "workbuddy.ai" in origin:
            row["realm"] = "global"
        for kv in (o.get("localStorage") or []):
            if not isinstance(kv, dict):
                continue
            key = str(kv.get("name") or "")
            value = str(kv.get("value") or "")
            if key == "CODEBUDDY_IDE_SELECTED_ACCOUNT_ID" and value and not row["uid"]:
                row["uid"] = value.strip()
            if key == "loggedIn" and value.lower() == "true":
                row["logged_in"] = True
            if _looks_like_access_token(value) and not row["token_found"]:
                row["token_found"] = True
                row["token_source"] = "localStorage:" + key
                payload = jwt_payload(value) or {}
                if not row["uid"]:
                    row["uid"] = str(payload.get("sub") or payload.get("uid") or "")

    if ident:
        name, suspect = _clean_name(ident.get("nickname"))
        row["name"] = name
        row["name_suspect"] = suspect
        row["phone"] = str(ident.get("phone") or "")
        row["identity_file"] = ident.get("_file") or ""
        row["identity_fetched_at"] = str(ident.get("fetched_at") or "")
    return row


def _candidate_from_identity(fp: Path, raw: Dict[str, Any]) -> Dict[str, Any]:
    name, suspect = _clean_name(raw.get("nickname"))
    return {
        "file": fp.name,
        "path": str(fp),
        "kind": "identity",
        "size": fp.stat().st_size,
        "mtime": int(fp.stat().st_mtime),
        "uid": "",
        "name": name,
        "phone": str(raw.get("phone") or ""),
        "realm": "cn",
        "logged_in": False,
        "cookies": 0,
        "cookie_names": [],
        "cookie_expires_at": None,
        "cookie_expires_text": "",
        "cookie_expired": None,
        "token_found": False,
        "token_source": "",
        "name_suspect": suspect,
        "has_web_session": False,
        "identity_fetched_at": str(raw.get("fetched_at") or ""),
        "note": "只有昵称与手机号，没有 token；用于补齐其它快照的账号信息。",
    }


def _candidate_from_login_sessions(fp: Path, raw: Any) -> List[Dict[str, Any]]:
    found: List[Tuple[str, str]] = []
    _deep_collect(raw, found)
    rows: List[Dict[str, Any]] = []
    if not found:
        return rows
    for path, token in found[:10]:
        payload = jwt_payload(token) or {}
        uid = str(payload.get("sub") or payload.get("uid") or "")
        rows.append({
            "file": fp.name,
            "path": str(fp),
            "kind": "login_session",
            "size": fp.stat().st_size,
            "mtime": int(fp.stat().st_mtime),
            "uid": uid,
            "name": "",
            "phone": "",
            "realm": "global" if "workbuddy.ai" in fp.name else "cn",
            "logged_in": True,
            "cookies": 0,
            "cookie_names": [],
            "cookie_expires_at": payload.get("exp"),
            "cookie_expires_text": fmt_ts(payload.get("exp")),
            "cookie_expired": bool(payload.get("exp") and int(payload["exp"]) < now_ts()) if payload.get("exp") else None,
            "token_found": True,
            "token_source": "login_sessions" + path,
            "name_suspect": False,
            "has_web_session": False,
            "note": "设备授权登录过程中留下的 token，可直接导入。",
        })
    return rows


def scan(extra_roots: Optional[List[str]] = None, include_identity_only: bool = True) -> Dict[str, Any]:
    """扫描项目内（以及额外目录）的登录快照。"""
    roots: List[Path] = [WB_DATA_DIR, DATA_DIR, AUTH_DIR]
    for extra in (extra_roots or []):
        try:
            roots.append(Path(extra).expanduser())
        except Exception:
            continue

    index = auths_index()
    snapshots: List[Dict[str, Any]] = []
    identities: Dict[str, Dict[str, Any]] = {}
    seen: Dict[str, int] = {}

    def _key(fp: Path) -> str:
        stem = fp.stem
        if "_" in stem:
            return stem.split("_", 1)[1]
        return stem

    for root in roots:
        if not root.exists():
            continue
        candidates = list(root.glob("login_state_*.json")) + list(root.glob("*/login_state_*.json"))
        for fp in sorted(set(candidates)):
            raw = _load_json(fp, {})
            if isinstance(raw, dict):
                row = _candidate_from_login_state(fp, raw)
                snapshots.append(row)
                seen[str(fp)] = len(snapshots) - 1
        if include_identity_only:
            idents = list(root.glob("identity_*.json")) + list(root.glob("*/identity_*.json"))
            for fp in sorted(set(idents)):
                raw = _load_json(fp, {})
                if isinstance(raw, dict):
                    raw["_file"] = fp.name
                    raw["_path"] = str(fp)
                    identities[_key(fp)] = raw
    # 把 identity 合并进同名 login_state（昵称/手机号补齐）
    for i, row in enumerate(snapshots):
        if row.get("kind") != "login_state":
            continue
        raw_key = _key(Path(row["path"]))
        ident = identities.get(raw_key)
        if ident:
            name, suspect = _clean_name(ident.get("nickname"))
            row["name"] = name or row.get("name") or ""
            row["name_suspect"] = suspect
            row["phone"] = str(ident.get("phone") or row.get("phone") or "")
            row["identity_file"] = ident.get("_file") or ""
            row["identity_fetched_at"] = str(ident.get("fetched_at") or "")
    # 没配对的 identity 单独列出
    used = set()
    for row in snapshots:
        if row.get("identity_file"):
            used.add(row["identity_file"])
    for key, ident in identities.items():
        fname = str(ident.get("_file") or "")
        if fname and fname not in used:
            snapshots.append(_candidate_from_identity(Path(str(ident.get("_path") or (WB_DATA_DIR / fname))), ident))

    if LOGIN_SESSIONS.exists():
        raw = _load_json(LOGIN_SESSIONS, None)
        snapshots.extend(_candidate_from_login_sessions(LOGIN_SESSIONS, raw))

    for row in snapshots:
        uid = str(row.get("uid") or "")
        known = index.get(uid) if uid else None
        row["auths_file"] = known.get("file") if known else ""
        row["auths_has_token"] = bool(known and known.get("has_token"))
        if row.get("token_found"):
            row["importable"] = bool(uid or row.get("token"))
            row["status"] = "可导入"
            row["action"] = "import"
        elif known and known.get("has_token"):
            row["importable"] = False
            row["status"] = "已是用网关账号"
            row["action"] = "none"
            row["note"] = row.get("note") or ("auths/" + str(known.get("file")) + " 已存在且带 accessToken")
        elif row.get("has_web_session"):
            row["importable"] = False
            row["status"] = "只有网页 Cookie"
            row["action"] = "none"
            row["note"] = ("快照里只有网页会话 Cookie（session / KEYCLOAK_*），没有 API accessToken。"
                           "网关请求需要 Bearer token，硬用 Cookie 只会一直 401——请用「设备授权登录」扫码获取。")
        elif row.get("kind") == "identity":
            row["importable"] = False
            row["status"] = "仅身份信息"
            row["action"] = "none"
        else:
            row["importable"] = False
            row["status"] = "无可用凭据"
            row["action"] = "none"
        if not row.get("uid") and row.get("status") == "可导入":
            row["status"] = "可导入（缺 uid）"

    snapshots.sort(key=lambda r: (0 if r.get("kind") == "login_state" else 1, str(r.get("file") or "")))
    return {
        "ok": True,
        "version": SNAPSHOT_VERSION,
        "roots": [str(r) for r in roots],
        "scan_at": now_ts(),
        "auths_dir": str(AUTH_DIR),
        "accounts": [
            dict(index[uid], uid=uid) for uid in sorted(index.keys())
        ],
        "snapshots": snapshots,
        "importable": [r for r in snapshots if r.get("importable")],
        "note": ("扫描范围：wb_data/、data/、auths/（可传 extra_roots 追加）。"
                 "只有带 API accessToken 的快照能被导入；网页 Cookie 快照只做展示与提示。"),
    }


def find_token_in_file(path: Any) -> Tuple[str, str]:
    """服务端自己从快照文件里取 accessToken（面板只传路径，不接收 token）。"""
    fp = Path(str(path))
    if not fp.exists():
        return "", ""
    raw = _load_json(fp, None)
    found: List[Tuple[str, str]] = []
    if isinstance(raw, dict) and isinstance(raw.get("cookies"), list):
        for c in raw.get("cookies") or []:
            if isinstance(c, dict) and _looks_like_access_token(str(c.get("value") or "")):
                found.append(("cookie:" + str(c.get("name") or ""), str(c.get("value"))))
        for o in (raw.get("origins") or []):
            if not isinstance(o, dict):
                continue
            for kv in (o.get("localStorage") or []):
                if isinstance(kv, dict) and _looks_like_access_token(str(kv.get("value") or "")):
                    found.append(("localStorage:" + str(kv.get("name") or ""), str(kv.get("value"))))
    if not found:
        deep: List[Tuple[str, str]] = []
        _deep_collect(raw, deep)
        for item in deep:
            if _looks_like_access_token(item[1]):
                found.append(("json" + item[0], item[1]))
    if not found:
        return "", ""
    return found[0][1], found[0][0]


def import_snapshot(snapshot: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """把一个快照导入成 auths/<uid>.json（仅当它真的带 accessToken）。"""
    path = str((snapshot or {}).get("path") or "")
    uid = str((snapshot or {}).get("uid") or "")
    if not path:
        return {"ok": False, "error": "缺少快照路径"}
    token, source = find_token_in_file(path)
    if not token:
        return {"ok": False, "status": "unsupported",
                "error": ("该快照没有 API accessToken（只有网页会话 Cookie），无法导入为网关账号。"
                          "请用面板的「设备授权登录」扫码获取 token。")}
    if not uid:
        uid = str((jwt_payload(token) or {}).get("sub") or (jwt_payload(token) or {}).get("uid") or "")
    if not uid:
        return {"ok": False, "error": "快照里没有 uid，无法命名 auths/<uid>.json"}
    if not UID_RE.match(uid):
        return {"ok": False, "error": "uid 形态不合法：" + uid}

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    target = AUTH_DIR / ("workbuddy-" + uid + ".json")
    if target.exists():
        return {"ok": False, "status": "exists", "error": "同名账号文件已存在：" + target.name, "file": str(target)}

    payload = jwt_payload(token) or {}
    record = {
        "realm": str((snapshot or {}).get("realm") or ("global" if "workbuddy.ai" in path else "cn")),
        "account": {
            "uid": uid,
            "enterpriseId": "",
            "nickname": str((snapshot or {}).get("name") or ""),
            "phone": str((snapshot or {}).get("phone") or ""),
        },
        "auth": {
            "accessToken": token,
            "tokenType": "Bearer",
            "expiresAt": payload.get("exp") or 0,
        },
        "metadata": {
            "imported_from": path,
            "imported_at": now_ts(),
            "imported_at_text": time.strftime("%Y-%m-%d %H:%M:%S"),
            "token_source": source,
            "source_kind": str((snapshot or {}).get("kind") or ""),
            "note": "由「桌面快照发现」导入；凭据来源为本地快照文件。",
        },
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "file": str(target), "record": record}

    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    tmp.replace(target)
    return {"ok": True, "file": str(target), "uid": uid, "record": record}


if __name__ == "__main__":
    print(json.dumps(scan(), ensure_ascii=False, indent=2)[:6000])
