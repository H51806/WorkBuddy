#!/usr/bin/env python3
"""
WorkBuddy 成长任务与保活自动化引擎
====================================
从零重写的一体化任务引擎：

- 不搬运上游 Go 源码，不复用 task_common.py 的代码结构
- 仅复用已知的公开接口路径与字段形态
- 统一处理账号发现、任务扫描、accept / report / claim、保活刷新
- 全部函数保持幂等：已完成自动跳过，重复执行不会产生重复奖励

依赖：Python 标准库 + 本地 auths/*.json 凭证文件
      （如果还没有 auth 凭证，请先用 wb_credits_fetcher.py 的扫码流程完成一次登录，
        或把 OAuth token 保存为 auths/<account>.json）
"""

import json
import os
import re
import time
import glob
import uuid
import hashlib
import logging
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ===== 基础配置 =====
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "wb_data"
AUTH_DIR = BASE_DIR / "auths"
TASKS_LOG = DATA_DIR / "tasks_log.jsonl"

CHAT_BASE = "https://copilot.tencent.com"
BILL_BASE = "https://www.codebuddy.cn"
WEB_BASE = "https://www.workbuddy.cn"

UA_CLI = "WorkBuddy/5.5.6 CLI/2.63.2"
UA_DESKTOP = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"
UA_WEB = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
UA_BROWSER = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"

# 桌面客户端指纹常量（与 panel-main/internal/upstream/desktop.go 对齐）。
# 缺这些字段 → 服务端 200 但静默丢弃，growth 进度不会涨。
DESKTOP_COMMIT = "5f9692923c93033111c51ad7b003eb80204a9b75"
DESKTOP_RELEASE_DATE = 1789036585355
DESKTOP_IDE_VERSION = "5.5.6"
DESKTOP_OS = "win32"
DESKTOP_OS_VERSION = "10.0.26220"
DESKTOP_CPU_CORES = 20
DESKTOP_MEMORY_SIZE = 24
DESKTOP_EXT_NAME = "workbuddy-desktop"
DESKTOP_PRODUCT = "SaaS"

# ===== 日志 =====
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger("wb-tasks")

# ===== 任务目录 =====
# 每条任务：code=上游任务码；kind=执行策略；reward=奖励说明；manual=不可自动的原因
TASKS: List[Dict[str, Any]] = [
    {"code": "first_buddy", "kind": "first_buddy", "reward": "+300c +8e", "desc": "领取第一只 Buddy"},
    {"code": "create_canvas", "kind": "canvas", "reward": "+300c +5e", "desc": "设计创意模式创建画布"},
    {"code": "chat_5", "kind": "chat5", "reward": "+100c +5e", "desc": "和 AI 聊天 5 次"},
    {"code": "Model_chat_GLM5.2", "kind": "model_chat_glm", "reward": "+100c +5e", "desc": "体验 GLM-5.2 模型对话一次"},
    {"code": "RichMeow_Chat", "kind": "richmeow", "reward": "+100c +5e +限定 Buddy", "desc": "桌面端对话 1 次"},
    {"code": "Buddy_App", "kind": "buddy_app", "reward": "+100c +5e", "desc": "进入任意 Buddy 应用"},
    {"code": "Buddy_App_QQ", "kind": "buddy_app_qq", "reward": "+50c +5e", "desc": "企鹅教师助手进入事件"},
    {"code": "automation_1", "kind": "automation", "reward": "+100c +5e", "desc": "设置自动化任务"},
    {"code": "Library_read", "kind": "library_read", "reward": "+100c +5e", "desc": "资料库阅读点击"},
    {"code": "template_5", "kind": "template5", "reward": "+100c +5e", "desc": "使用 5 个模板"},
    {"code": "playbook_prompt", "kind": "playbook_prompt", "reward": "+100c +5e", "desc": "灵感案例「做同款」发送事件"},
    {"code": "expert_5", "kind": "expert5", "reward": "+100c +5e", "desc": "真实专家召唤+使用链 ×5"},
    {"code": "Expert_team_use_3", "kind": "expert_team3", "reward": "+100c +5e", "desc": "专家团召唤+使用链 ×3"},
    {"code": "Hp_Appearance", "kind": "appearance", "reward": "+100c +5e", "desc": "主题设置 + 皮肤生效事件"},
    {"code": "Expert_lighthouse", "kind": "expert_lighthouse", "reward": "+100c +5e", "desc": "轻量云专家召唤+使用链"},
    {"code": "skill_1", "kind": "skill1", "reward": "+100c +5e", "desc": "真实对话 + 技能加载事件"},
    {"code": "Expert_Philanthropy", "kind": "manual", "reward": "需真实捐款", "desc": "需真实捐款，无法纯 API 完成"},
]

# 任务中心默认关注的码（未自动化的任务会被标记为 manual）
AUTO_TASK_CODES = [t["code"] for t in TASKS if t["kind"] != "manual"]


# ===== 工具函数 =====
def _mkdirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _log_task(entry: Dict[str, Any]):
    _mkdirs()
    entry = dict(entry)
    entry["ts"] = datetime_iso()
    with open(TASKS_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def datetime_iso() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _task_reward(code: str) -> str:
    """按任务码查奖励文案（账本里存人可读的 +300c +8e）。"""
    for t in TASKS:
        if t.get("code") == code:
            return str(t.get("reward") or "")
    return ""


# ===== 账号加载 =====
def load_accounts() -> List[Dict[str, Any]]:
    """从 auths/ 目录发现账号。兼容两种结构：
    1) {"auth": {"accessToken": ...}, "account": {"uid": ..., "nickname": ...}}
    2) 扁平结构 {"token": "...", "uid": "...", "name": "..."}
    """
    _mkdirs()
    if not AUTH_DIR.exists():
        return []

    accounts = []
    for fp in sorted(glob.glob(str(AUTH_DIR / "*.json"))):
        try:
            raw = _read_json(Path(fp))
            acc = _normalize_account(raw, Path(fp).name)
            accounts.append(acc)
        except Exception as e:
            log.warning(f"跳过无效 auth 文件 {fp}: {e}")
    return accounts


def _normalize_account(raw: Dict[str, Any], filename: str) -> Dict[str, Any]:
    auth = raw.get("auth") or raw
    account = raw.get("account") or {}
    token = auth.get("accessToken") or auth.get("token") or auth.get("access_token") or ""
    uid = account.get("uid") or auth.get("uid") or account.get("id") or ""
    name = raw.get("name") or account.get("nickname") or account.get("name") or filename
    domain = auth.get("domain") or ""

    if not token or not uid:
        raise ValueError("缺少 accessToken 或 uid")

    return {
        "token": token,
        "uid": str(uid),
        "name": str(name),
        "domain": str(domain),
        "realm": str(raw.get("realm") or auth.get("realm") or ""),
        "file": filename,
        "device_token": raw.get("device_token") or auth.get("device_token") or "",
    }


# ===== 请求封装 =====
def _headers(acc: Dict[str, Any], origin: str, ua: str) -> Dict[str, str]:
    hdr = {
        "Authorization": "Bearer " + acc["token"],
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": ua,
        "Origin": origin,
        "Referer": origin.rstrip("/") + "/",
    }
    if acc.get("uid"):
        hdr["X-User-Id"] = acc["uid"]
    if acc.get("domain"):
        hdr["X-Domain"] = acc["domain"]
    if acc.get("device_token"):
        hdr["X-Device-Token"] = acc["device_token"]
    return hdr


def request_json(acc: Dict[str, Any], method: str, url: str, body: Optional[Dict[str, Any]] = None,
                 origin: str = WEB_BASE, ua: str = UA_WEB, timeout: int = 30) -> Tuple[int, Dict[str, Any]]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(acc, origin, ua), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(txt)
            except Exception:
                return resp.status, {"raw": txt[:500]}
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"raw": txt[:500]}
    except Exception as e:
        return -1, {"err": repr(e)}


def list_tasks(acc: Dict[str, Any]) -> List[Dict[str, Any]]:
    st, body = request_json(acc, "GET", CHAT_BASE + "/v2/activity/growth/tasks", origin=CHAT_BASE, ua=UA_CLI)
    if st != 200:
        raise RuntimeError(f"list_tasks http={st}")
    data = body.get("data") or {}
    return data.get("tasks") or []


def find_task(acc: Dict[str, Any], code: str) -> Optional[Dict[str, Any]]:
    for t in list_tasks(acc):
        if t.get("task_code") == code:
            return t
    return None


def task_done(task: Dict[str, Any]) -> bool:
    return (task or {}).get("accept_status") == "claimed"


def task_progress(task: Dict[str, Any]) -> Tuple[int, int]:
    prog = (task or {}).get("progress") or {}
    return int(prog.get("current", 0) or 0), int(prog.get("target", 0) or 0)


def refetch_task(acc: Dict[str, Any], code: str, settle: float = 1.5,
                 old_task: Optional[Dict[str, Any]] = None,
                 retries: int = 1) -> Optional[Dict[str, Any]]:
    """执行动作后重新拉取该任务的最新 progress / accept_status。

    上游事件上报（/v2/report）是异步的：HTTP 立刻回 200，但成长计划的进度
    要过一会儿才反映到 /growth/tasks 列表里。所以「执行」和「判定是否完成」
    必须用重新拉取后的结果，而不是用执行前缓存的那份 task 对象——否则进度
    永远是旧值，界面只能显示「完成」（其实是上游根本没动）。

    settle：上报后等上游落库的秒数。若拉回的结果与 old_task 进度完全一致
    （可能还没落库），再等一轮重拉，最多 retries 次。
    """
    last = old_task
    for attempt in range(retries + 1):
        if settle > 0:
            time.sleep(settle)
        try:
            cur = find_task(acc, code)
        except Exception:
            cur = None
        if cur:
            if last is not None and attempt < retries and cur.get("progress") == last.get("progress"):
                last = cur
                continue
            return cur
        last = cur
    return last


def accept_task(acc: Dict[str, Any], code: str) -> Tuple[int, Dict[str, Any]]:
    return request_json(acc, "POST", CHAT_BASE + "/v2/activity/growth/tasks/accept",
                        body={"task_codes": [code]}, origin=CHAT_BASE, ua=UA_CLI)


def claim_task(acc: Dict[str, Any], code: str) -> Tuple[int, Dict[str, Any]]:
    """Web 域领奖。上游实测（panel-main tasks.go:227）：
    路径 = {webBase}/activity/growth/tasks/<code>/claim（任务码在路径里，body 为空）
    必需头：x-client-platform: web, Origin: webBase, Referer: webBase/profile/growth-center
    """
    headers = _headers(acc, WEB_BASE, UA_WEB)
    headers["x-client-platform"] = "web"
    headers["Referer"] = WEB_BASE + "/profile/growth-center"
    req = urllib.request.Request(
        WEB_BASE + f"/activity/growth/tasks/{code}/claim",
        data=None,
        headers=headers,
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(txt) if txt else {"raw": txt}
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"raw": txt[:500]}
    except Exception as e:
        return -1, {"err": repr(e)}


def report_events(acc: Dict[str, Any], events: List[Dict[str, Any]],
                  origin: str = BILL_BASE, ua: str = UA_CLI, timeout: int = 30) -> Tuple[int, Dict[str, Any]]:
    return request_json(acc, "POST", origin + "/v2/report", body=events, origin=origin, ua=ua, timeout=timeout)


def chat_completion(acc: Dict[str, Any], model_id: str, prompt: str, max_tokens: int = 32,
                    timeout: int = 60) -> Tuple[int, str]:
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": max_tokens,
    }
    hdr = _headers(acc, CHAT_BASE, UA_CLI)
    hdr["Accept"] = "text/event-stream"
    req = urllib.request.Request(
        CHAT_BASE + "/v2/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers=hdr,
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            first = ""
            for raw in resp:
                line = raw.decode("utf-8", "replace")
                if line.startswith("data: "):
                    payload = line[6:].strip()
                    if payload in ("[DONE]", ""):
                        continue
                    try:
                        obj = json.loads(payload)
                        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                        if delta.get("content") and not first:
                            first = delta["content"]
                    except Exception:
                        pass
            return resp.status, first
        return resp.status, first
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        return e.code, txt[:200]
    except Exception as e:
        return -1, repr(e)[:200]


def chat_event(acc: Dict[str, Any], model_id: str = "deepseek-v4-flash",
               model_name: str = "DeepSeek V4 Flash") -> Dict[str, Any]:
    now = int(time.time() * 1000)
    cid = f"task-{now}-{uuid.uuid4().hex[:8]}"
    return {
        "eventCode": "chat_request_send",
        "timestamp": now,
        "reportDelay": 0,
        "mode": "craft",
        "conversationId": cid,
        "requestId": cid,
        "inputLength": 12,
        "requestModelId": model_id,
        "requestModelName": model_name,
        "isPlan": False,
        "isAutoExecuteTerminal": False,
        "isAutoModify": False,
        "codebaseEnable": False,
        "maxToken": 0,
        "maxSteps": 0,
        "temperature": 0,
        "maxRetries": 0,
        "mentionContexts": [],
        "knowledgeId": [],
        "knowledgeName": [],
        "codebaseId": "",
        "mentionContextCount": 0,
        "command": "",
        "expertId": "",
        "recommendId": "",
        "skillId": "",
        "skillCount": 0,
        "totalCount": 0,
        "fileUri": "",
        "presentAt": now,
        "traceId": "",
        "rootRequestId": cid,
        "parentConversationId": cid,
        "agentName": "default",
        "agentType": "conversation",
        "userId": acc["uid"],
    }


# ===== 桌面客户端指纹（移植自 panel-main/internal/upstream/desktop.go） =====
# 桌面事件缺这些字段 → 服务端 200 但静默丢弃，growth 进度不会涨。
def _derive_id(uid: str, salt: str) -> str:
    """上游 deriveID：sha256(salt+uid) 的前 36 字符。用于 machineId / sessionId / reqId。"""
    return hashlib.sha256(f"{salt}{uid}".encode("utf-8")).hexdigest()[:36]


def desktop_fingerprint(acc: Dict[str, Any], now_ms: Optional[int] = None) -> Dict[str, Any]:
    """每个桌面事件必带的公共指纹字段（desktop.go:64）。"""
    now_ms = now_ms or int(time.time() * 1000)
    uid = acc.get("uid", "")
    return {
        "timezone": "Asia/Shanghai",
        "reportDelay": 2000,
        "userId": uid,
        "username": acc.get("name", uid),
        "userNickname": acc.get("name", uid),
        "product": DESKTOP_PRODUCT,
        "releaseDate": DESKTOP_RELEASE_DATE,
        "commit": DESKTOP_COMMIT,
        "ideName": "WorkBuddy",
        "ideType": "WorkBuddy",
        "ideVersion": DESKTOP_IDE_VERSION,
        "machineId": _derive_id(uid, "machine:"),
        "sessionId": _derive_id(uid, "session:"),
        "extName": DESKTOP_EXT_NAME,
        "extVersion": DESKTOP_IDE_VERSION,
        "os": DESKTOP_OS,
        "arch": "x64",
        "osVersion": DESKTOP_OS_VERSION,
        "cpuCores": DESKTOP_CPU_CORES,
        "memorySize": DESKTOP_MEMORY_SIZE,
        "timestamp": now_ms,
        "presentAt": now_ms,
    }


def desktop_headers(acc: Dict[str, Any]) -> Dict[str, str]:
    """桌面域请求头（desktop.go:43 DesktopHeaders）。"""
    uid = acc.get("uid", "")
    req_id = _derive_id(uid, "req:") + str(int(time.time() * 1e9) % 1_000_000)
    return {
        "Authorization": "Bearer " + acc["token"],
        "Content-Type": "application/json;charset=UTF-8",
        "Accept": "application/json",
        "User-Agent": UA_DESKTOP,
        "X-Domain": CHAT_BASE,
        "X-Product": DESKTOP_PRODUCT,
        "X-User-Id": uid,
        "X-Request-ID": req_id,
        "Origin": CHAT_BASE,
        "Referer": CHAT_BASE + "/",
    }


def desktop_report(acc: Dict[str, Any], events: List[Dict[str, Any]],
                   now_ms: Optional[int] = None) -> Tuple[int, Dict[str, Any]]:
    """向 CHAT_BASE /v2/report 上报桌面指纹事件数组。每个 event 自动注入指纹。"""
    now_ms = now_ms or int(time.time() * 1000)
    fp = desktop_fingerprint(acc, now_ms)
    payload = []
    for ev in events:
        ev = dict(ev)
        ev.update(fp)
        payload.append(ev)
    return request_json(acc, "POST", CHAT_BASE + "/v2/report",
                        body=payload, origin=CHAT_BASE, ua=UA_DESKTOP, timeout=30)


def web_report_event(acc: Dict[str, Any], event_code: str, page_url: str,
                     element_id: str, element_name: str) -> Tuple[int, Dict[str, Any]]:
    """Web 域 /v2/report 上报单条 web_element_click（Library_read 用）。"""
    now_ms = int(time.time() * 1000)
    uid = acc.get("uid", "")
    event = {
        "eventCode": event_code,
        "timestamp": now_ms,
        "reportDelay": 0,
        "pageURL": page_url,
        "elementId": element_id,
        "elementName": element_name,
        "os": "Win32",
        "arch": "",
        "osVersion": "10.0",
        "userAgent": UA_BROWSER,
        "machineId": _derive_id(uid, "webmachine:"),
        "userId": uid,
        "userNickname": acc.get("name", uid),
        "enterpriseId": "",
    }
    hdr = _headers(acc, WEB_BASE, UA_BROWSER)
    hdr["x-client-platform"] = "web"
    hdr["Referer"] = page_url
    return request_json(acc, "POST", WEB_BASE + "/v2/report",
                        body=[event], origin=WEB_BASE, ua=UA_BROWSER, timeout=30)


# ===== 桌面事件链构造函数 =====
def desktop_chat_sequence(conv: str, req: str, msg: str,
                          model_id: str = "fast-model",
                          model_name: str = "fast-model") -> List[Dict[str, Any]]:
    """6 事件对话链（desktop.go:133 DesktopChatSequence）。"""
    now_ms = int(time.time() * 1000)
    return [
        {
            "eventCode": "agent_task_created", "source": "LOCAL", "name": "working",
            "task_target": "local", "mode": "craft",
            "requestModelId": model_id, "requestModelName": model_name,
            "has_repo": False, "repo_type": "none", "workspace_type": "empty",
            "has_connector": False, "connector_types": [],
            "has_mention": False, "mention_types": [],
            "has_template": False, "action": "", "template_name": "",
            "has_expert": False, "expert_id": "", "expert_name": "",
            "expert_industry_id": "", "has_skill": False, "skill_names": [],
            "conversationId": conv, "messageId": msg,
            "buddyId": "", "buddyName": "",
        },
        {
            "eventCode": "chat_message_send",
            "messageId": msg + "-assistant", "historyCount": 0,
            "isContextTruncated": False, "currentStepCount": 1,
            "traceId": req, "rootRequestId": req, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main",
        },
        {
            "eventCode": "chat_request_send",
            "inputLength": 24, "isPlan": False, "isAutoExecuteTerminal": False,
            "isAutoModify": False, "codebaseEnable": False, "maxToken": 0,
            "maxSteps": 500, "temperature": 0, "maxRetries": 0,
            "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
            "codebaseId": "", "mentionContextCount": 0, "command": "",
            "recommendId": "", "skillId": "", "skillCount": 0, "totalCount": 0,
            "traceId": req, "rootRequestId": req, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main",
            "codebuddy.session_id": conv, "codebuddy.conversation_request_id": req,
        },
        {
            "eventCode": "chat_message_response",
            "messageId": msg + "-assistant",
            "responseModelId": model_id,
            "inputToken": 120, "outputToken": 80, "totalToken": 200,
            "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
            "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
            "firstTokenAt": now_ms, "traceId": req, "conversationId": conv,
            "rootRequestId": req, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main",
            "codebuddy.session_id": conv, "codebuddy.conversation_request_id": req,
        },
        {
            "eventCode": "chat_message_status",
            "messageId": msg + "-assistant", "messageErrorCode": "0",
            "traceId": req, "rootRequestId": req, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main",
        },
        {
            "eventCode": "chat_request_response",
            "mode": "craft", "toolCallCount": 0,
            "inputToken": 120, "outputToken": 80, "totalToken": 200,
            "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
            "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
            "rootRequestId": req, "parentConversationId": conv,
        },
    ]


def desktop_buddy_app_sequence(buddy_id: str, buddy_name: str) -> List[Dict[str, Any]]:
    """5 事件 buddyapp 进入链（desktop.go:232）。"""
    base = {"mode": "LOCAL", "buddyId": buddy_id, "buddyName": buddy_name}
    return [
        {**base, "eventCode": "buddyapp_discover_click"},
        {**base, "eventCode": "buddyapp_show",
         "elementId": buddy_id, "elementName": buddy_name, "position": 2},
        {**base, "eventCode": "buddyapp_enter_click",
         "elementId": buddy_id, "elementName": buddy_name, "position": 2, "isFirstPage": "1"},
        {**base, "eventCode": "buddyapp_auth_confirm_click",
         "elementId": buddy_id, "elementName": buddy_name},
        {**base, "eventCode": "buddyapp_bindaccount_skip_click",
         "elementId": buddy_id, "elementName": buddy_name},
    ]


def desktop_automation_create_event(name: str) -> List[Dict[str, Any]]:
    """单事件 automated_task_create_suc（desktop.go:254）。"""
    return [{
        "eventCode": "automated_task_create_suc",
        "name": name, "source": "manually", "modelId": "fast-model",
        "modelIsThinking": True, "connectorCount": 0, "skills": "",
        "skillCount": 0, "scheduleType": "once", "mode": "LOCAL",
    }]


def desktop_template_use_sequence(conv: str, req: str,
                                  tpl_id: str, tpl_name: str) -> List[Dict[str, Any]]:
    """8 事件模板使用链（= DesktopChatSequence + 2）。"""
    msg = f"msg-{tpl_id}"
    events = desktop_chat_sequence(conv, req, msg, "fast-model", "fast-model")
    events.append({
        "eventCode": "agent_task_created_with_template",
        "mode": "working", "isCustomModel": False,
        "id": tpl_id, "name": tpl_name, "requestId": req,
    })
    events.append({
        "eventCode": "template_used",
        "template_id": tpl_id, "task_mode": "working",
    })
    return events


def desktop_playbook_prompt_sequence(conv: str, req: str,
                                     case_id: str, case_name: str) -> List[Dict[str, Any]]:
    """9 事件灵感案例链（= DesktopChatSequence + 3）。"""
    events = desktop_chat_sequence(conv, req, "msg-pb", "fast-model", "fast-model")
    events.append({
        "eventCode": "web_element_click",
        "pageName": "playbook_detail",
        "elementId": "playbook_ctaClick",
        "elementName": case_name,
        "source": "discover",
    })
    events.append({
        "eventCode": "playbook_cta_click",
        "source": "discover", "position": 0,
        "id": case_id, "name": case_name, "type": "document",
        "categoryId": "", "categoryName": "",
    })
    events.append({
        "eventCode": "playbook_prompt_send",
        "conversationId": conv, "requestId": req,
        "id": case_id, "name": case_name, "type": "document",
        "categoryId": "", "categoryName": "",
    })
    return events


def desktop_design_canvas_sequence(conv: str, req: str) -> List[Dict[str, Any]]:
    """8 事件设计画布链（= DesktopChatSequence + 2）。"""
    events = desktop_chat_sequence(conv, req, "msg-canvas", "fast-model", "fast-model")
    ardot_id = f"ardot-file-{req[-8:]}"
    events.append({
        "eventCode": "wbx_design_canvas_task_create",
        "conversationId": conv, "requestId": req,
        "source": "summon_keyword", "cost": 12000, "isSuccessful": True,
    })
    events.append({
        "eventCode": "wbx_design_canvas_open",
        "conversationId": conv, "requestId": req,
        "id": ardot_id, "source": "summon_keyword",
        "type": "page", "cost": 13000, "isSuccessful": True,
    })
    return events


# ===== 专家市场 & 真实 chat =====
def market_expert_list(acc: Dict[str, Any], expert_type: str = "agent") -> List[Dict[str, Any]]:
    """拉专家市场列表（desktop.go:381 MarketExpertList）。"""
    body = {"page": 1, "page_size": 20, "sort_by": "reco_rank",
            "sort_order": "desc", "expert_type": expert_type}
    st, res = request_json(acc, "POST",
                           CHAT_BASE + "/portal/operation-platform/market/expert/list",
                           body=body, origin=CHAT_BASE, ua=UA_DESKTOP, timeout=30)
    if st != 200:
        return []
    data = res.get("data") or {}
    return data.get("experts") or []


def desktop_chat_with_expert(acc: Dict[str, Any], expert_id: str = "",
                             conv_prefix: str = "wb2api-expert") -> Tuple[str, str]:
    """真实 chat SSE（带 X-Expert-Id 可选），返回服务端 (convId, requestId)。
    requestId 正则 ^(cmb-)?[0-9a-f]{32}$。
    """
    now_nano = str(int(time.time() * 1e9))
    conv = f"{conv_prefix}-{now_nano}"
    body = {
        "model": "fast-model",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. 当前处于中文环境，使用简体中文回答。"},
            {"role": "user", "content": "1+1等于几？直接回答。"},
        ],
        "agent": "cli", "temperature": 1, "stream": True,
        "stream_options": {"include_usage": True},
    }
    hdr = desktop_headers(acc)
    hdr["Accept"] = "text/event-stream"
    hdr["X-Conversation-ID"] = conv
    hdr["X-Request-ID"] = now_nano
    hdr["X-Agent-Intent"] = "craft"
    hdr["X-Agent-Type"] = "main"
    hdr["X-IDE-Name"] = "WorkBuddy"
    hdr["X-IDE-Type"] = "WorkBuddy"
    hdr["X-IDE-Version"] = DESKTOP_IDE_VERSION
    hdr["x-codebuddy-request"] = "1"
    if expert_id:
        hdr["X-Expert-Id"] = expert_id
    req = urllib.request.Request(
        CHAT_BASE + "/v2/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers=hdr, method="POST"
    )
    srv_req = ""
    srv_conv = conv
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            buf = ""
            for raw in resp:
                line = raw.decode("utf-8", "replace")
                buf += line
                if not srv_req:
                    m = re.search(r'"id"\s*:\s*"(cmb-[0-9a-f]{32}|[0-9a-f]{32})"', line)
                    if m:
                        srv_req = m.group(1)
                # 抓 convId
                if not srv_conv or srv_conv == conv:
                    mc = re.search(r'"conversation[_-]?id"\s*:\s*"([^"]+)"', line, re.I)
                    if mc:
                        srv_conv = mc.group(1)
                # 读干：限制最大 1MB
                if len(buf) > 1 << 20:
                    break
    except Exception:
        pass
    return srv_conv, srv_req or now_nano


def desktop_expert_summon_sequence(expert: Dict[str, Any]) -> List[Dict[str, Any]]:
    """3 事件召唤链（desktop.go:523 DesktopExpertSummonSequence）。"""
    eid = expert.get("id") or expert.get("expert_id") or ""
    name = expert.get("display_name_zh") or expert.get("name") or eid
    title = expert.get("profession_zh") or name
    etype = expert.get("expert_type") or expert.get("type") or "agent"
    version = expert.get("version") or "1.0.0"
    return [
        {
            "eventCode": "web_element_click",
            "source": eid, "type": etype, "version": version,
            "elementId": "expert_summon_click", "elementName": "立即召唤",
            "pageURL": "/C:/Program%20Files/WorkBuddy/resources/app.asar/renderer/index.html",
        },
        {
            "eventCode": "expert_summon_click",
            "id": eid, "name": name, "expertTitle": title,
            "type": "expert-all", "position": 0, "expertType": etype,
            "version": version, "mode": "LOCAL",
        },
        {
            "eventCode": "expert_summoned",
            "id": eid, "name": name, "expertTitle": title, "type": "expert-all",
        },
    ]


def desktop_expert_actual_use_event(expert: Dict[str, Any], conv: str, req: str,
                                    mode: str = "craft") -> Dict[str, Any]:
    """expert_actual_use 单事件（desktop.go:554）。"""
    eid = expert.get("id") or expert.get("expert_id") or ""
    name = expert.get("display_name_zh") or expert.get("name") or eid
    title = expert.get("profession_zh") or name
    etype = expert.get("expert_type") or expert.get("type") or "agent"
    version = expert.get("version") or "1.0.0"
    return {
        "eventCode": "expert_actual_use",
        "id": eid, "name": name, "expertTitle": title,
        "type": "expert-all", "expertType": etype,
        "source": "builtin", "version": version,
        "cost": 9000, "characterCount": 14,
        "conversationId": conv, "requestId": req,
        "messageId": f"msg-{req[-8:]}",
        "requestModelId": "fast-model", "requestModelName": "fast-model",
        "mode": mode,
    }


def set_appearance_theme(acc: Dict[str, Any], resource_key: str) -> Tuple[int, Dict[str, Any]]:
    """设置主题（desktop.go:206 SetAppearanceTheme）。"""
    hdr = desktop_headers(acc)
    hdr["Accept"] = "application/json, text/plain, */*"
    hdr["Content-Type"] = "application/json;charset=UTF-8"
    req = urllib.request.Request(
        CHAT_BASE + "/v2/user-asset/appearance/set",
        data=json.dumps({"kind": "theme", "resource_key": resource_key},
                        ensure_ascii=False).encode(),
        headers=hdr, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            txt = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(txt) if txt else {}
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, {"raw": txt[:500]}
    except Exception as e:
        return -1, {"err": repr(e)}


# ===== 任务执行动作 =====
def run_first_buddy(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if task_done(task):
        res.update({"skipped": True, "ok": True, "msg": "已完成，无需重复执行"})
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将执行 report + agreement + buddy/first"})
        return res

    _, rep = report_events(acc, [chat_event(acc)], origin=BILL_BASE, ua=UA_CLI)
    res["steps"].append({"report": rep})
    time.sleep(1.5)
    st, ag = request_json(acc, "POST", CHAT_BASE + "/activity/growth/buddy/agreement",
                          body={"agree": True}, origin=CHAT_BASE, ua=UA_CLI)
    res["steps"].append({"agreement": st, "body": ag})
    time.sleep(1.5)
    st2, bf = request_json(acc, "POST", CHAT_BASE + "/activity/growth/buddy/first",
                           body={}, origin=CHAT_BASE, ua=UA_CLI)
    res["steps"].append({"buddy_first": st2, "body": bf})
    res["ok"] = (st == 200 and st2 == 200)
    res["msg"] = f"agree={ag.get('code')} buddy={bf.get('code')}"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_chat_5(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool, count: int = 0) -> Dict[str, Any]:
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if task_done(task):
        res.update({"skipped": True, "ok": True, "msg": "已完成"})
        return res
    cur, target = task_progress(task)
    need = max(0, target - cur)
    if count > 0:
        need = min(need, count)
    if need <= 0:
        res.update({"skipped": True, "ok": True, "msg": f"已满足 {cur}/{target}"})
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": f"将上报 {need} 条 chat_request_send"})
        return res
    events = [chat_event(acc) for _ in range(need)]
    st, body = report_events(acc, events, origin=BILL_BASE, ua=UA_CLI)
    res["steps"].append({"report": {"status": st, "code": body.get("code") if isinstance(body, dict) else None}})
    res["ok"] = (st == 200)
    res["msg"] = f"上报 {need} 条完成，返回 {body.get('code') if isinstance(body, dict) else body}"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_model_chat_glm(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if task_done(task):
        res.update({"skipped": True, "ok": True, "msg": "已完成"})
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将 accept + GLM-5.2 真实对话一次"})
        return res
    st, body = accept_task(acc, task["task_code"])
    res["steps"].append({"accept": st, "body": body})
    st2, first = chat_completion(acc, "glm-5.2", "hi，请回复一句话")
    res["steps"].append({"chat": {"status": st2, "first": first[:80]}})
    st3, rep = report_events(acc, [chat_event(acc, "glm-5.2", "GLM-5.2")], origin=BILL_BASE, ua=UA_CLI)
    res["steps"].append({"report": st3, "body": rep})
    res["ok"] = (st == 200 and st2 == 200 and st3 == 200)
    res["msg"] = "GLM-5.2 对话与上报完成"
    _record_outcome(res, acc, task, dry_run)
    return res


def _record_outcome(res: Dict[str, Any], acc: Dict[str, Any], task: Dict[str, Any],
                    dry_run: bool = False) -> None:
    """把「执行前进度 → 执行后进度」写进结果，并如实判定是否真的完成。

    核心修复点：上报是异步的，HTTP 200 只代表「收到了」，不代表成长计划
    进度已涨。这里重新拉取一次任务状态再判定，避免出现「Web 显示完成、
    上游根本没动」的假成功。

    同时重新定义 ok 的语义：ok = 任务真的达标（进度满 target 或上游标记
    claimed），而不是「上报接口回了 200」。这样界面就不会再假完成。
    """
    code = res.get("code")
    cur, target = task_progress(task)
    res["progress_before"] = {
        "current": cur, "target": target, "accept_status": task.get("accept_status"),
    }

    if dry_run:
        return

    fresh = refetch_task(acc, code, old_task=task)
    if not fresh:
        res["effect"] = "未知"
        res["msg"] = (res.get("msg") or "") + "（未能复查上游状态）"
        res["ok"] = False
        return

    fc, ft = task_progress(fresh)
    res["progress_after"] = {
        "current": fc, "target": ft, "accept_status": fresh.get("accept_status"),
    }
    claimed = fresh.get("accept_status") == "claimed"
    target_met = bool(ft) and fc >= ft

    if fc > cur:
        res["effect"] = "已计入进度"
    else:
        res["effect"] = "未计入进度"

    if claimed:
        res["ok"] = True
        res["msg"] = f"完成 {fc}/{ft}（上游已标记领取）"
    elif target_met:
        res["ok"] = True
        res["msg"] = f"完成 {fc}/{ft}，可领奖"
    elif fc > cur:
        res["ok"] = False
        res["msg"] = f"进度 {cur}/{target} → {fc}/{ft}（未完成，还需 {max(ft - fc, 0)} 次）"
    else:
        res["ok"] = False
        res["msg"] = (f"HTTP 已上报，但上游进度未变（仍为 {cur}/{target}）："
                      "该任务需要真实操作，上报事件不被计入")


def run_generic_report(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool,
                       event_kind: str, repeat: int = 1, model_id: str = "deepseek-v4-flash") -> Dict[str, Any]:
    """通用行为事件上报任务。"""
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if task_done(task):
        res.update({"skipped": True, "ok": True, "msg": "已完成"})
        return res
    cur, target = task_progress(task)
    need = max(1, target - cur) if target else 1
    if repeat:
        need = max(need, repeat)
    if dry_run:
        res.update({"dry_run": True, "msg": f"将上报 {event_kind} x{need}"})
        return res
    events = [chat_event(acc, model_id=model_id) for _ in range(need)]
    st, body = report_events(acc, events, origin=BILL_BASE, ua=UA_CLI)
    res["steps"].append({"report": {"status": st, "code": body.get("code") if isinstance(body, dict) else None}})
    res["ok"] = (st == 200)
    res["msg"] = f"{event_kind} 上报完成 x{need}"
    _record_outcome(res, acc, task, dry_run)
    return res


def _skip_if_done(res: Dict[str, Any], task: Dict[str, Any]) -> bool:
    if task_done(task):
        res.update({"skipped": True, "ok": True, "msg": "已完成"})
        return True
    return False


def run_buddy_app(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool, repeat: int = 1) -> Dict[str, Any]:
    """Buddy_App / Buddy_App_QQ 共用。5 事件 buddyapp 进入链（两账号实测）。"""
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if _skip_if_done(res, task):
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将上报 buddyapp 五连事件（discover→show→enter→auth_confirm→bind_skip）"})
        return res
    events = desktop_buddy_app_sequence("cb_y5Dy46tPQGGWtueMxXbe", "企鹅教师助手")
    st, body = desktop_report(acc, events)
    res["steps"].append({"report": {"status": st, "code": body.get("code") if isinstance(body, dict) else None}})
    res["ok"] = (st == 200)
    res["msg"] = "已上报 buddyapp 五连事件"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_appearance(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """Hp_Appearance：set theme + appearance_skin_apply 事件（两账号实测）。"""
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if _skip_if_done(res, task):
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将 set theme + 上报 appearance_skin_apply"})
        return res
    st, body = set_appearance_theme(acc, "theme-tkmw7j")
    res["steps"].append({"set": {"status": st, "body": body}})
    time.sleep(2.0)
    ev = [{"eventCode": "appearance_skin_apply", "action": "apply",
           "source": "settings_close", "id": "theme-tkmw7j",
           "vipLevel": 0, "series": "", "type": "unknown"}]
    st2, rep = desktop_report(acc, ev)
    res["steps"].append({"report": {"status": st2, "code": rep.get("code") if isinstance(rep, dict) else None}})
    res["ok"] = (st == 200 and st2 == 200)
    res["msg"] = "已设置主题并上报皮肤生效事件"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_library_read(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """Library_read：web 域 web_element_click(library_doc_intro_click)（三账号实测）。"""
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if _skip_if_done(res, task):
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将上报 web_element_click(library_doc_intro_click)"})
        return res
    doc_url = "https://www.workbuddy.cn/space/d/o0KWYeynteVv06UnAZqIFm"
    st, body = web_report_event(acc, "web_element_click", doc_url,
                                "library_doc_intro_click", "WorkBuddy资料库介绍")
    res["steps"].append({"report": {"status": st, "code": body.get("code") if isinstance(body, dict) else None}})
    res["ok"] = (st == 200)
    res["msg"] = "已上报资料库阅读事件"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_automation(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """automation_1：automated_task_create_suc 单事件（两账号实测）。"""
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if _skip_if_done(res, task):
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将上报 automated_task_create_suc"})
        return res
    events = desktop_automation_create_event("wb2api 自动化")
    st, body = desktop_report(acc, events)
    res["steps"].append({"report": {"status": st, "code": body.get("code") if isinstance(body, dict) else None}})
    res["ok"] = (st == 200)
    res["msg"] = "已上报定时任务创建事件"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_template5(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """template_5：5 组 × 8 事件（三账号实测）。"""
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if _skip_if_done(res, task):
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将上报 template_used ×5（8 事件/组）"})
        return res
    templates = [("1", "深度研究"), ("2", "周报生成"), ("3", "竞品分析"),
                 ("4", "活动策划"), ("5", "代码评审")]
    ok = 0
    for i, (tid, tname) in enumerate(templates):
        ms = int(time.time() * 1000)
        conv = f"wb2api-tpl-{ms}-{i}"
        req = f"wb2api-tpl-req-{ms}-{i}"
        events = desktop_template_use_sequence(conv, req, tid, tname)
        st, body = desktop_report(acc, events)
        if st == 200:
            ok += 1
        res["steps"].append({"group": i + 1, "status": st,
                             "code": body.get("code") if isinstance(body, dict) else None})
        time.sleep(0.3)
    res["ok"] = (ok >= 1)
    res["msg"] = f"已上报 template_used {ok}/5 组"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_playbook(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """playbook_prompt：9 事件组（三账号实测）。"""
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if _skip_if_done(res, task):
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将上报 playbook_cta_click + playbook_prompt_send"})
        return res
    ms = int(time.time() * 1000)
    conv = f"wb2api-pb-{ms}"
    req = f"wb2api-pb-req-{ms}"
    events = desktop_playbook_prompt_sequence(conv, req, "pm-gtm-launch-plan", "新产品上市 GTM 发布计划一页纸")
    st, body = desktop_report(acc, events)
    res["steps"].append({"report": {"status": st, "code": body.get("code") if isinstance(body, dict) else None}})
    res["ok"] = (st == 200)
    res["msg"] = "已上报 playbook_cta_click + playbook_prompt_send"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_canvas(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """create_canvas：8 事件组（三账号实测，+300 分）。"""
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if _skip_if_done(res, task):
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将上报 wbx_design_canvas_task_create + open"})
        return res
    ms = int(time.time() * 1000)
    conv = f"wb2api-canvas-{ms}"
    req = f"wb2api-canvas-req-{ms}"
    events = desktop_design_canvas_sequence(conv, req)
    st, body = desktop_report(acc, events)
    res["steps"].append({"report": {"status": st, "code": body.get("code") if isinstance(body, dict) else None}})
    res["ok"] = (st == 200)
    res["msg"] = "已上报 wbx_design_canvas_task_create/open"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_richmeow(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """RichMeow_Chat：6 事件桌面对话链（三账号实测）。"""
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if _skip_if_done(res, task):
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将上报桌面端对话事件链（6 事件）"})
        return res
    ms = int(time.time() * 1000)
    conv = f"wb2api-rm-{ms}"
    req = f"wb2api-rm-req-{ms}"
    msg = f"req-{ms}-user"
    events = desktop_chat_sequence(conv, req, msg, "fast-model", "fast-model")
    st, body = desktop_report(acc, events)
    res["steps"].append({"report": {"status": st, "code": body.get("code") if isinstance(body, dict) else None}})
    res["ok"] = (st == 200)
    res["msg"] = "已按桌面指纹上报完整对话事件链"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_expert_batch(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool,
                     expert_type: str, count: int) -> Dict[str, Any]:
    """expert_5 / Expert_team_use_3 共用实现。
    每位专家：market list → summon 链 → 真实 chat（带 X-Expert-Id）→ actual_use 事件。
    """
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if _skip_if_done(res, task):
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": f"将拉取专家列表 + 召唤 {count} 位 {expert_type}"})
        return res
    experts = market_expert_list(acc, expert_type)
    if not experts:
        res.update({"ok": False, "msg": f"专家市场列表为空（type={expert_type}）"})
        _record_outcome(res, acc, task, dry_run)
        return res
    ok, fail = 0, 0
    for i, e in enumerate(experts[:count]):
        eid = e.get("id") or e.get("expert_id") or ""
        if not eid:
            fail += 1
            continue
        # 1) 召唤链
        summon_events = desktop_expert_summon_sequence(e)
        st, b1 = desktop_report(acc, summon_events)
        # 2) 真实 chat 拿服务端 requestId
        conv, srv_req = desktop_chat_with_expert(acc, eid, "wb2api-expert")
        # 3) actual_use（= DesktopChatSequence 6 + expert_actual_use）
        msg = f"msg-{srv_req[-8:]}"
        use_events = desktop_chat_sequence(conv, srv_req, msg, "fast-model", "fast-model")
        use_events.append(desktop_expert_actual_use_event(e, conv, srv_req, mode="craft"))
        st2, b2 = desktop_report(acc, use_events)
        if st == 200 and st2 == 200:
            ok += 1
        else:
            fail += 1
        res["steps"].append({"idx": i + 1, "expert": eid,
                             "summon": st, "chat_conv": conv, "chat_req": srv_req, "use": st2})
        time.sleep(6.0)  # expertSummonGap
    res["ok"] = (ok >= 1)
    res["msg"] = f"专家链完成 {ok}/{count}（失败 {fail}）"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_expert5(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    return run_expert_batch(acc, task, dry_run, "agent", 5)


def run_expert_team3(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    return run_expert_batch(acc, task, dry_run, "team", 3)


def run_expert_lighthouse(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """Expert_lighthouse：轻量云专家召唤+使用链（两账号实测）。"""
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if _skip_if_done(res, task):
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将召唤轻量云专家 + 上报 expert_actual_use(mode=LOCAL)"})
        return res
    lh_id = "ex_2cvvUZQhDyeJ"
    # 尝试从市场列表拿真实版本，否则用兜底
    expert = {"id": lh_id, "expert_type": "agent",
              "display_name_zh": "腾讯轻量云专家", "profession_zh": "腾讯轻量云专家",
              "version": "1.0.2"}
    for e in market_expert_list(acc, "agent"):
        if (e.get("id") or e.get("expert_id")) == lh_id:
            expert = {**expert, **e}
            break
    # 召唤链
    summon = desktop_expert_summon_sequence(expert)
    st, b1 = desktop_report(acc, summon)
    # 真实 chat
    conv, srv_req = desktop_chat_with_expert(acc, lh_id, "wb2api-lh")
    # 使用事件（agent_task_created 需带 has_expert；mode=LOCAL 而非 craft）
    msg = f"msg-{srv_req[-8:]}"
    use_events = desktop_chat_sequence(conv, srv_req, msg, "fast-model", "fast-model")
    # 修改第 0 条 agent_task_created 加 has_expert
    use_events[0]["has_expert"] = True
    use_events[0]["expert_id"] = lh_id
    use_events[0]["expert_name"] = "腾讯轻量云专家"
    use_events[0]["expert_industry_id"] = ""
    actual = desktop_expert_actual_use_event(expert, conv, srv_req, mode="LOCAL")
    actual["type"] = ""
    actual["cost"] = 0
    use_events.append(actual)
    st2, b2 = desktop_report(acc, use_events)
    res["steps"].append({"summon": st, "chat_conv": conv, "chat_req": srv_req, "use": st2})
    res["ok"] = (st == 200 and st2 == 200)
    res["msg"] = "已召唤轻量云专家并上报使用事件"
    _record_outcome(res, acc, task, dry_run)
    return res


def run_skill1(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    """skill_1：真实 chat + 技能加载事件（finishReason=tool_calls）（1 账号实测）。"""
    res = {"code": task.get("task_code"), "ok": False, "skipped": False, "steps": []}
    if _skip_if_done(res, task):
        return res
    if dry_run:
        res.update({"dry_run": True, "msg": "将真实对话 + 上报 skill_info 事件"})
        return res
    conv, srv_req = desktop_chat_with_expert(acc, "", "wb2api-skill")
    msg = f"msg-{srv_req[-8:]}"
    events = desktop_chat_sequence(conv, srv_req, msg, "fast-model", "fast-model")
    # 修改第 3 条 chat_message_response.finishReason → tool_calls
    for ev in events:
        if ev.get("eventCode") == "chat_message_response":
            ev["finishReason"] = "tool_calls"
            ev["messageId"] = msg
            break
    events.append({
        "eventCode": "skill_info",
        "id": "润泽小馆·日报撰写",
        "skillId": "skill_2097350077599879168",
        "skillVersion": "1.0.0",
        "toolStatus": "success",
        "fileCount": 56,
        "source": "workbuddy-desktop",
        "conversationId": conv, "requestId": srv_req, "messageId": msg,
        "requestModelId": "fast-model", "requestModelName": "fast-model",
        "traceId": srv_req,
    })
    st, body = desktop_report(acc, events)
    res["steps"].append({"report": {"status": st, "code": body.get("code") if isinstance(body, dict) else None}})
    res["ok"] = (st == 200)
    res["msg"] = "已真实对话 + 上报 skill_info 事件"
    _record_outcome(res, acc, task, dry_run)
    return res


# ===== 保活 =====
def keepalive(acc: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """刷新 token / 保活。优先尝试 refresh token 端点，失败则退回到读取 streak。"""
    res = {"ok": False, "skipped": False, "dry_run": dry_run, "status": "ok"}
    if dry_run:
        return res
    # 1) 尝试刷新 token
    st, body = request_json(acc, "POST",
                            CHAT_BASE + "/v2/plugin/auth/token/refresh",
                            body={"refresh_token": acc.get("refresh_token", "")},
                            origin=CHAT_BASE, ua=UA_CLI)
    if st == 200 and body.get("code") in (0, "0"):
        data = body.get("data") or {}
        acc["token"] = data.get("access_token") or data.get("accessToken") or acc["token"]
        acc["refresh_token"] = data.get("refresh_token") or data.get("refreshToken") or acc.get("refresh_token")
        res.update({"ok": True, "msg": "token refreshed"})
        return res
    # 2) 退回到读 streak 作为保活探针
    st2, streak = request_json(acc, "GET", CHAT_BASE + "/activity/growth/streak",
                               origin=CHAT_BASE, ua=UA_CLI)
    if st2 == 200:
        days = ((streak.get("data") or {}).get("streak") or {}).get("days", 0)
        res.update({"ok": True, "msg": f"keepalive probe ok, streak={days}"})
        return res
    res.update({"ok": False, "status": f"refresh failed http={st}; probe failed http={st2}",
                "msg": f"保活失败: {st}/{st2}"})
    return res


# ===== 扫描 & 一键完成 =====
def scan_accounts(accounts: Optional[List[Dict[str, Any]]] = None,
                  codes: Optional[List[str]] = None) -> Dict[str, Any]:
    accounts = accounts or load_accounts()
    codes = codes or AUTO_TASK_CODES
    out = {"ok": True, "accounts": [], "tasks": [], "errors": []}
    for acc in accounts:
        try:
            tasks = list_tasks(acc)
            items = []
            for t in tasks:
                if codes and t.get("task_code") not in codes:
                    continue
                items.append(t)
            out["accounts"].append({
                "uid": acc["uid"],
                "name": acc["name"],
                "task_count": len(tasks),
                "matched": len(items),
            })
            out["tasks"].extend({
                "uid": acc["uid"],
                "name": acc["name"],
                **t,
            })
        except Exception as e:
            out["errors"].append({"uid": acc["uid"], "error": str(e)})
    return out


def run_task(acc: Dict[str, Any], task: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    code = task.get("task_code") or task.get("code")
    kind = next((t["kind"] for t in TASKS if t["code"] == code), "manual")
    actions = {
        "first_buddy": run_first_buddy,
        "chat5": run_chat_5,
        "model_chat_glm": run_model_chat_glm,
        "buddy_app": run_buddy_app,
        "buddy_app_qq": run_buddy_app,
        "automation": run_automation,
        "library_read": run_library_read,
        "template5": run_template5,
        "playbook_prompt": run_playbook,
        "expert5": run_expert5,
        "expert_team3": run_expert_team3,
        "skill1": run_skill1,
        "expert_lighthouse": run_expert_lighthouse,
        "appearance": run_appearance,
        "canvas": run_canvas,
        "richmeow": run_richmeow,
    }
    fn = actions.get(kind)
    if not fn:
        return {"code": code, "ok": False, "skipped": True, "msg": "该任务当前不支持纯 API 自动完成"}
    return fn(acc, task, dry_run=dry_run)


def run_one_account(acc: Dict[str, Any], codes: Optional[List[str]] = None,
                    dry_run: bool = False, claim: bool = True) -> Dict[str, Any]:
    codes = codes or AUTO_TASK_CODES
    result = {"ok": True, "steps": [], "skipped": 0, "errors": 0}
    try:
        tasks = {t.get("task_code"): t for t in list_tasks(acc)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    for code in codes:
        t = tasks.get(code)
        if not t:
            result["steps"].append({"code": code, "skipped": True, "msg": "账号没有此任务"})
            result["skipped"] += 1
            continue
        res = run_task(acc, t, dry_run=dry_run)
        if not res.get("skipped") and not dry_run:
            # 用「执行后重新拉取」的状态决定是否领奖：旧的实现拿执行前缓存的
            # task 判断 task_done()，而上报是异步的，那时进度一定还是 0，
            # 导致领奖永远不被触发，积分也永远拿不到。
            after = res.get("progress_after") or {}
            fc, ft = after.get("current", 0), after.get("target", 0)
            target_met = bool(ft) and fc >= ft
            if claim and target_met and after.get("accept_status") != "claimed":
                try:
                    ccode, cbody = claim_task(acc, code)
                    cdata = cbody.get("data") or cbody if isinstance(cbody, dict) else {}
                    res["claim"] = cbody
                    res["claim_ok"] = (ccode == 200 and
                                       str(cdata.get("code", cbody.get("code", "999") if isinstance(cbody, dict) else "999")) in ("0", "200"))
                    if res["claim_ok"]:
                        res["effect"] = "已领奖"
                        res["ok"] = True
                        res["msg"] = f"完成 {fc}/{ft} 且已领奖"
                except Exception as e:
                    res["claim"] = {"err": str(e)}
                    res["claim_ok"] = False
        if not res.get("ok") and not res.get("skipped"):
            result["errors"] += 1
        # 收益账本：每个任务一行（含跳过与失败），供面板「签到日志」页汇总。
        try:
            _log_task({
                "kind": "task",
                "uid": acc.get("uid"),
                "name": acc.get("name"),
                "code": code,
                "task_code": code,
                "reward": _task_reward(code),
                "ok": bool(res.get("ok")),
                "skipped": bool(res.get("skipped")),
                "effect": res.get("effect") or "",
                "msg": res.get("msg") or "",
                "claim_ok": bool(res.get("claim_ok")),
                "progress_before": res.get("progress_before"),
                "progress_after": res.get("progress_after"),
                "dry_run": bool(dry_run),
            })
        except Exception as e:
            log.warning(f"账本写入失败 {code}: {e}")
        result["steps"].append(res)
    return result


def run_all_accounts(accounts: Optional[List[Dict[str, Any]]] = None,
                     codes: Optional[List[str]] = None,
                     dry_run: bool = False, claim: bool = True) -> Dict[str, Any]:
    accounts = accounts or load_accounts()
    codes = codes or AUTO_TASK_CODES
    out = {"ok": True, "accounts": [], "errors": []}
    for acc in accounts:
        try:
            res = run_one_account(acc, codes=codes, dry_run=dry_run, claim=claim)
            out["accounts"].append({"uid": acc["uid"], "name": acc["name"], **res})
        except Exception as e:
            out["errors"].append({"uid": acc["uid"], "error": str(e)})
    return out


def run_keepalive_all(accounts: Optional[List[Dict[str, Any]]] = None,
                      dry_run: bool = False) -> Dict[str, Any]:
    accounts = accounts or load_accounts()
    out = {"ok": True, "accounts": [], "errors": []}
    for acc in accounts:
        try:
            res = keepalive(acc, dry_run=dry_run)
            out["accounts"].append({"uid": acc["uid"], "name": acc["name"], **res})
        except Exception as e:
            out["errors"].append({"uid": acc["uid"], "error": str(e)})
    return out


if __name__ == "__main__":
    print(json.dumps({
        "accounts": len(load_accounts()),
        "tasks": len(TASKS),
        "auto": len(AUTO_TASK_CODES),
    }, ensure_ascii=False, indent=2))
