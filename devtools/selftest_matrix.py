# -*- coding: utf-8 -*-
"""网关全流程自测（桩上游 + 可选真上游）。

跑法：
    python devtools/selftest_matrix.py            # 只跑桩上游的 15 项（离线、不花额度）
    python devtools/selftest_matrix.py --real     # 追加真上游端到端（会消耗真实额度）

设计要点：
- 桩上游复用真实协议：网关对上游**永远发 stream=true**，所以桩一律回 SSE，
  非流式客户端由网关的 aggregate_sse_completion 聚合 —— 这样才测到真链路。
- 所有落盘路径改到临时目录（STATE_FILE / usage 库），自测绝不写用户真实状态。
- 每项自测前重置账号池与熔断状态，项与项之间不互相污染。
"""
import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import wb_gateway as g  # noqa: E402

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

GW_PORT = 47897
UP_PORT = 47896
API_KEY = "selftest_key"
#: 自测落盘目录。放在工程内而不是系统临时目录：本机 DSH 文件沙箱
#: 对某些临时路径写入返回 PermissionError（见 pool.save → state.json）。
TMP = ROOT / "devtools" / "_selftest_tmp"
if TMP.exists():
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir(parents=True, exist_ok=True)

#: 桩上游的行为开关。每个用例开始前重设，用例之间互不影响。
STUB = {
    "mode": "sse_ok",      # sse_ok | sse_tool | json_error
    "status": 200,
    "payload": None,
    "hits": 0,
    "last": {},
}

RESULTS = []   # [(name, category, ok, detail)]
FAILURES = []


def sse_frames():
    """按 mode 生成 SSE 帧。"""
    if STUB["mode"] == "sse_tool":
        yield ('data: {"id":"chatcmpl-stub","object":"chat.completion.chunk","model":"deepseek-v4-flash",'
               '"choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_1",'
               '"type":"function","function":{"name":"get_weather","arguments":"{\\"city\\":\\"Shenzhen\\"}"}}]}}]}\n\n')
    else:
        yield ('data: {"id":"chatcmpl-stub","object":"chat.completion.chunk","model":"deepseek-v4-flash",'
               '"choices":[{"index":0,"delta":{"role":"assistant","content":"stub-ok"}}]}\n\n')
    yield ('data: {"id":"chatcmpl-stub","object":"chat.completion.chunk","model":"deepseek-v4-flash",'
           '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
           '"usage":{"prompt_tokens":11,"completion_tokens":3,"total_tokens":14}}\n\n')
    yield "data: [DONE]\n\n"


class StubUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        STUB["hits"] += 1
        try:
            STUB["last"] = json.loads(raw.decode("utf-8"))
        except Exception:
            STUB["last"] = {}

        if STUB["mode"] == "json_error":
            body = json.dumps(STUB["payload"], ensure_ascii=False).encode("utf-8")
            self.send_response(STUB["status"])
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        chunk = "".join(sse_frames()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)


# ===== 请求辅助 =====

def call(path, body=None, key=API_KEY, timeout=60):
    url = "http://127.0.0.1:%d%s" % (GW_PORT, path)
    data = None
    headers = {}
    if key is not None:
        headers["Authorization"] = "Bearer " + key
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data is not None else "GET")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace"), time.time() - t0


def chat(model="deepseek-v4-flash", stream=False, messages=None, extra=None, timeout=60):
    body = {"model": model, "stream": stream,
            "messages": messages if messages is not None else [{"role": "user", "content": "hi"}]}
    if extra:
        body.update(extra)
    return call("/v1/chat/completions", body, timeout=timeout)


def reset_pool(two=True, credits=(500, 400), domain="www.codebuddy.cn"):
    pool = g.pool
    pool.accounts.clear()
    pool.sticky_sessions.clear()
    pool.session_ttl.clear()
    pool.current_account_id = None
    names = [("号A", credits[0]), ("号B", credits[1] if len(credits) > 1 else 400)]
    for i, (nm, cr) in enumerate(names[:2 if two else 1], 1):
        acc = g.Account(id="t%d" % i, name=nm, token="tok%d" % i,
                        domain=domain, credits=cr)
        pool.accounts[acc.id] = acc
    pool.current_account_id = "t1"
    pool.distribution = "priority"
    pool.region = "merged"
    pool.credit_floor = 0.0
    pool.breaker_threshold = 3
    pool.breaker_cooldown = 1800.0
    pool.rate_cooldown = 600.0
    pool.last_pick_policy = ""
    pool.last_pick_reason = ""
    g._ANTIPROBE_EVENTS.clear()
    g._ANTIPROBE_BLOCK_UNTIL = 0.0
    g._ANTIPROBE_BLOCK_LEVEL = 0


def stub(mode="sse_ok", status=200, payload=None):
    STUB["mode"] = mode
    STUB["status"] = status
    STUB["payload"] = payload
    STUB["hits"] = 0
    STUB["last"] = {}


def record(name, category, ok, detail=""):
    RESULTS.append((name, category, ok, detail))
    if not ok:
        FAILURES.append("%s: %s" % (name, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + ("" if ok else "  <- " + detail))


def acc_of(headers):
    raw = headers.get("X-WorkBuddy-Account") or headers.get("x-workbuddy-account") or ""
    return urllib.parse.unquote(raw)


# ===== 用例 =====

def t01_models():
    st, hd, txt, cost = call("/v1/models")
    if st != 200:
        return False, "HTTP %s" % st
    ids = [m.get("id") for m in (json.loads(txt).get("data") or [])]
    if "deepseek-v4-flash" not in ids:
        return False, "模型列表缺 deepseek-v4-flash: %s" % ids[:5]
    return True, "%d 个模型，耗时 %.2fs" % (len(ids), cost)


def t02_bad_key():
    st, hd, txt, cost = call("/v1/chat/completions",
                             {"model": "deepseek-v4-flash", "stream": False,
                              "messages": [{"role": "user", "content": "hi"}]}, key=None)
    if st != 401:
        return False, "无 key 应 401，实际 %s" % st
    st, hd, txt, cost = call("/v1/chat/completions",
                             {"model": "deepseek-v4-flash", "stream": False,
                              "messages": [{"role": "user", "content": "hi"}]}, key="wrong_key")
    if st != 401:
        return False, "错误 key 应 401，实际 %s" % st
    if json.loads(txt).get("error", {}).get("type") != "authentication_error":
        return False, "错误 type: %s" % txt[:120]
    return True, "缺 key / 错 key 均 401 authentication_error"


def t03_chat_nonstream():
    stub()
    st, hd, txt, cost = chat()
    if st != 200:
        return False, "HTTP %s: %s" % (st, txt[:200])
    data = json.loads(txt)
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
    if content != "stub-ok":
        return False, "聚合内容异常: %r" % content
    if not acc_of(hd):
        return False, "缺 X-WorkBuddy-Account 响应头"
    if (data.get("usage") or {}).get("total_tokens") != 14:
        return False, "usage 未聚合: %s" % data.get("usage")
    return True, "SSE 聚合为 chat.completion，content=stub-ok，账号=%s" % acc_of(hd)


def t04_chat_stream():
    stub()
    st, hd, txt, cost = chat(stream=True)
    if st != 200:
        return False, "HTTP %s" % st
    ctype = (hd.get("Content-Type") or hd.get("content-type") or "")
    if "text/event-stream" not in ctype:
        return False, "Content-Type 不是 SSE: %s" % ctype
    if "data:" not in txt or "[DONE]" not in txt:
        return False, "SSE 未透传: %r" % txt[:120]
    return True, "SSE 透传（%d 字节，首包 %.2fs）" % (len(txt), cost)


def t05_tool_calls():
    stub("sse_tool")
    st, hd, txt, cost = chat(extra={"tools": [{"type": "function", "function": {
        "name": "get_weather", "parameters": {"type": "object", "properties": {}}}}]})
    if st != 200:
        return False, "HTTP %s: %s" % (st, txt[:160])
    msg = (json.loads(txt).get("choices") or [{}])[0].get("message", {})
    tcs = msg.get("tool_calls") or []
    if not tcs or tcs[0]["function"]["name"] != "get_weather":
        return False, "tool_calls 未聚合: %s" % json.dumps(msg, ensure_ascii=False)[:200]
    if "tools" not in (STUB["last"] or {}):
        return False, "tools 未透传给上游"
    return True, "tool_calls 跨帧聚合 + tools 透传"


def t06_unknown_model():
    stub()
    st, hd, txt, cost = chat(model="totally-not-a-model")
    if st != 200:
        return False, "HTTP %s: %s" % (st, txt[:160])
    got = (STUB["last"] or {}).get("model")
    if got != "deepseek-v4-flash":
        return False, "未知模型未替换（上游收到 %r）" % got
    return True, "totally-not-a-model -> deepseek-v4-flash"


def t07_client_fault():
    stub("json_error", 400, {"code": 11133, "msg": "模型无法处理此请求，请重试",
                             "displayMsg": {"zh": "模型无法处理此请求，请重试"}})
    st, hd, txt, cost = chat(messages=[])
    if st != 400:
        return False, "应 400，实际 %s: %s" % (st, txt[:160])
    err = json.loads(txt).get("error") or {}
    if err.get("type") != "invalid_request_error":
        return False, "type 应为 invalid_request_error，实际 %r" % err.get("type")
    if "11133" not in err.get("message", ""):
        return False, "上游真实原因未透出: %r" % err.get("message")
    if STUB["hits"] != 1:
        return False, "客户端请求错不该重试/换号，实际打了上游 %d 次" % STUB["hits"]
    fails = sum(a.fail_count for a in g.pool.accounts.values())
    if fails:
        return False, "客户端请求错污染了账号健康（fail_count=%d）" % fails
    return True, "400 invalid_request_error + 上游原文，1 次调用，账号健康未污染"


def t08_antiprobe():
    stub("json_error", 400, {"code": 11128, "msg": "Illegal API invocation from an unapproved channel",
                             "displayMsg": {"zh": "请求被拦截，请重新发送"}})
    st, hd, txt, cost = chat()
    if st != 429:
        return False, "反探测应 429，实际 %s: %s" % (st, txt[:160])
    err = json.loads(txt).get("error") or {}
    if err.get("type") != "rate_limit_error":
        return False, "type 应为 rate_limit_error，实际 %r" % err.get("type")
    ra = hd.get("Retry-After") or hd.get("retry-after")
    if not ra or int(ra) < 1:
        return False, "Retry-After 缺失或 <1: %r" % ra
    if STUB["hits"] > 8:
        return False, "重发过多（%d 次上游调用）" % STUB["hits"]
    fails = sum(a.fail_count for a in g.pool.accounts.values())
    if fails:
        return False, "反探测污染了账号健康（fail_count=%d）" % fails
    return True, "%d 次上游调用后 429 + Retry-After=%s，账号健康未污染" % (STUB["hits"], ra)


def t09_no_usable_account():
    stub()
    now = time.time()
    for a in g.pool.accounts.values():
        a.breaker_until = now + 600
    old_wait = g.AVAIL_WAIT_MAX
    g.AVAIL_WAIT_MAX = 0.5
    try:
        st, hd, txt, cost = chat()
    finally:
        g.AVAIL_WAIT_MAX = old_wait
    if st != 503:
        return False, "应 503，实际 %s: %s" % (st, txt[:160])
    err = json.loads(txt).get("error") or {}
    if err.get("type") != "api_error":
        return False, "503 的 type 必须是 api_error（不是 invalid_request_error），实际 %r" % err.get("type")
    if STUB["hits"]:
        return False, "熔断中仍在打上游（%d 次）" % STUB["hits"]
    return True, "503 api_error + 原因，未打上游"


def t10_switch_account():
    stub()
    st, hd, txt, cost = call("/v1/switch", {"account_id": "t2"})
    if st != 200:
        return False, "switch HTTP %s: %s" % (st, txt[:160])
    if (json.loads(txt).get("account_id")) != "t2":
        return False, "未切到 t2: %s" % txt[:160]
    st, hd, txt, cost = chat()
    if st != 200:
        return False, "切换后请求失败 %s" % st
    if acc_of(hd) != "号B":
        return False, "切换后仍派发到 %r" % acc_of(hd)
    return True, "POST /v1/switch 生效，后续请求派发到 号B"


def t11_health_status():
    st, hd, txt, cost = call("/healthz", key=None)
    if st != 200:
        return False, "/healthz HTTP %s" % st
    st2, hd2, txt2, cost2 = call("/status")
    if st2 != 200:
        return False, "/status HTTP %s" % st2
    data = json.loads(txt2)
    if not isinstance(data, dict):
        return False, "/status 不是 JSON 对象"
    return True, "healthz 200 / status 200（%d 个字段）" % len(data)


def t12_panel_static():
    urls = ["/panel/", "/panel/static/app.css", "/panel/static/app.js",
            "/panel/static/manifest.webmanifest", "/panel/static/icon-192.png",
            "/panel/sw.js"]
    bad = []
    for u in urls:
        st, hd, txt, cost = call(u, key=None)
        if st != 200:
            bad.append("%s=%s" % (u, st))
    if bad:
        return False, "静态资源异常: " + ", ".join(bad)
    return True, "%d 个静态资源全 200" % len(urls)


def t13_panel_api():
    checks = ["/panel/api/models", "/panel/api/pool", "/panel/api/accounts",
              "/panel/api/usage", "/panel/api/usage/meta", "/panel/api/logs?n=5",
              "/panel/api/diagnose"]
    bad, diag = [], ""
    for u in checks:
        st, hd, txt, cost = call(u)
        if st != 200:
            bad.append("%s=%s" % (u, st))
            continue
        if u.endswith("/diagnose"):
            try:
                cs = json.loads(txt).get("checks") or []
                diag = "%d/%d" % (sum(1 for c in cs if c.get("ok")), len(cs))
            except Exception:
                pass
    if bad:
        return False, "面板接口异常: " + ", ".join(bad)
    return True, "%d 个面板接口全 200（diagnose %s）" % (len(checks), diag)


def t14_tasks_list():
    st, hd, txt, cost = call("/tasks/list")
    if st != 200:
        return False, "/tasks/list HTTP %s" % st
    data = json.loads(txt)
    if not isinstance(data, dict):
        return False, "返回不是 JSON 对象"
    tasks = data.get("tasks")
    return True, "/tasks/list 200（%s 个任务）" % (len(tasks) if isinstance(tasks, list) else "?")


def t15_usage_panel_and_record():
    """成功请求后用量接口仍可用（用量库把测试写到临时文件）。"""
    stub()
    st, hd, txt, cost = chat()
    if st != 200:
        return False, "前置请求失败 %s" % st
    st2, hd2, txt2, cost2 = call("/panel/api/usage")
    if st2 != 200:
        return False, "/panel/api/usage HTTP %s" % st2
    try:
        json.loads(txt2)
    except Exception:
        return False, "用量接口返回非 JSON"
    return True, "成功请求后 /panel/api/usage 仍 200"


def t19_unknown_balance_not_floored():
    """余额没采集过 ≠ 余额不足：不能被积分下限静默踢出候选池。"""
    reset_pool(two=True)
    for a in g.pool.accounts.values():
        a.credits = 0
        a.credits_known = False
    g.pool.credit_floor = 100.0
    stub()
    st, hd, txt, cost = chat()
    if st != 200:
        return False, "未采集余额的号被踢出候选池（HTTP %s）: %s" % (st, txt[:160])
    return True, "余额未知仍参与派发（账号 %s）" % acc_of(hd)


def t20_known_zero_balance_floor():
    """余额确认不足时确实被下限挡住，而且面板/摘要要说明原因。"""
    reset_pool(two=True)
    g.pool.accounts["t1"].credits = 0
    g.pool.accounts["t1"].credits_known = True
    g.pool.accounts["t2"].credits = 500
    g.pool.accounts["t2"].credits_known = True
    g.pool.credit_floor = 100.0
    # /status 会 ensure_pool_fresh()：磁盘 auths 与内存池对不上就重载，
    # 重载时 pool.load(STATE_FILE) 会把老余额读回来。所以先把这套值落盘。
    g.pool.save(g.STATE_FILE)
    stub()
    st, hd, txt, cost = chat()
    if st != 200:
        return False, "应派发到余额充足的号，实际 HTTP %s" % st
    if acc_of(hd) != "号B":
        return False, "积分下限没生效，派发到了 %r" % acc_of(hd)
    st2, hd2, txt2, cost2 = call("/status")
    acc = [a for a in (json.loads(txt2).get("accounts") or []) if a.get("id") == "t1"]
    if not acc or "积分不足" not in (acc[0].get("reason") or ""):
        return False, "面板未说明积分下限原因: %r" % (acc[0] if acc else None)
    st3, hd3, txt3, cost3 = call("/panel/api/pool")
    summ = (json.loads(txt3).get("unavailable") or [])
    if not any(u.get("id") == "t1" for u in summ):
        return False, "pool 摘要没列出被下限挡住的号: %s" % summ
    return True, "余额不足被下限挡住且面板给出原因（派发到 %s）" % acc_of(hd)


HERMETIC = [
    ("T01 GET /v1/models 模型列表", "协议", t01_models),
    ("T02 /v1/chat/completions 鉴权（缺 key / 错 key）", "协议", t02_bad_key),
    ("T03 非流式聊天（上游 SSE → 客户端 JSON 聚合）", "聊天", t03_chat_nonstream),
    ("T04 流式聊天（SSE 透传）", "聊天", t04_chat_stream),
    ("T05 tool_calls 跨帧聚合 + tools 透传", "聊天", t05_tool_calls),
    ("T06 未知模型名 → 兜底模型", "模型", t06_unknown_model),
    ("T07 客户端请求错（400）不透传换号、不污染账号", "容错", t07_client_fault),
    ("T08 上游反探测 11128 → 429 + Retry-After", "容错", t08_antiprobe),
    ("T09 全员熔断 → 503 api_error 且不打上游", "容错", t09_no_usable_account),
    ("T10 POST /v1/switch 切号生效", "账号池", t10_switch_account),
    ("T11 /healthz + /status", "面板", t11_health_status),
    ("T12 面板静态资源", "面板", t12_panel_static),
    ("T13 面板 API（models/pool/accounts/usage/logs/diagnose）", "面板", t13_panel_api),
    ("T14 /tasks/list 任务清单", "任务", t14_tasks_list),
    ("T15 用量统计接口", "用量", t15_usage_panel_and_record),
    ("T19 余额未采集不被积分下限踢掉", "账号池", t19_unknown_balance_not_floored),
    ("T20 余额不足按积分下限拦截并说明原因", "账号池", t20_known_zero_balance_floor),
]


def run_hermetic():
    for name, category, fn in HERMETIC:
        reset_pool()
        stub()
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, "%s: %s" % (type(e).__name__, e)
        record(name, category, ok, detail)


# ===== 真上游 =====

def run_real():
    # 先删掉前面用例留下的临时 state.json：否则 load_auths 会把桩账号 t1/t2
    # 连同假 token 一起并回账号池，真上游测试里会先撞一次 401。
    try:
        Path(g.STATE_FILE).unlink()
    except FileNotFoundError:
        pass
    reset_pool()
    g.pool.accounts.clear()
    g.config.setdefault("upstream", {}).pop("base_url", None)
    g.load_auths()
    names = [a.name for a in g.pool.accounts.values()]
    if not names:
        record("T16 真上游：账号加载", "端到端", False, "auths/ 里没有加载到账号")
        return
    record("T16 真上游：账号加载", "端到端", True, "加载 %d 个账号: %s" % (len(names), "、".join(names)))

    st, hd, txt, cost = chat(messages=[{"role": "user", "content": "只回复两个字：正常"}], timeout=120)
    if st == 200:
        content = (json.loads(txt).get("choices") or [{}])[0].get("message", {}).get("content", "")
        record("T17 真上游：非流式聊天", "端到端", True,
               "HTTP 200 账号=%s 内容=%r 耗时 %.1fs" % (acc_of(hd), content[:40], cost))
    else:
        record("T17 真上游：非流式聊天", "端到端", False, "HTTP %s %s" % (st, txt[:160]))

    st, hd, txt, cost = chat(stream=True, timeout=180)
    ok = st == 200 and "data:" in txt
    record("T18 真上游：流式聊天", "端到端", ok,
           "HTTP %s 首包 %.1fs，%d 字节" % (st, cost, len(txt)) if ok else "HTTP %s %s" % (st, txt[:160]))


def main():
    real = "--real" in sys.argv
    # 落盘改到临时目录：自测绝不写用户真实 state/usage
    g.STATE_FILE = TMP / "state.json"
    g.config.setdefault("upstream", {})["base_url"] = "http://127.0.0.1:%d" % UP_PORT
    g.config.setdefault("upstream", {})["timeout_seconds"] = 30
    g.api_key = API_KEY
    if g.wb_usage is not None:
        try:
            g.wb_usage.configure(path=str(TMP / "usage.json"), retention_days=7,
                                 logger=g.log, flush_interval=0.2)
        except Exception as e:
            print("  WARN 用量库改路径失败: %s" % e)

    up = ThreadingHTTPServer(("127.0.0.1", UP_PORT), StubUpstream)
    up.daemon_threads = True
    threading.Thread(target=up.serve_forever, daemon=True).start()
    gw = ThreadingHTTPServer(("127.0.0.1", GW_PORT), g.GatewayHandler)
    gw.daemon_threads = True
    threading.Thread(target=gw.serve_forever, daemon=True).start()
    time.sleep(0.4)

    print("WorkBuddy 网关 · 全流程自测")
    print("临时目录: %s" % TMP)
    print("\n[1/2] 桩上游（离线，不消耗额度）")
    run_hermetic()
    if real:
        print("\n[2/2] 真上游（会消耗真实额度）")
        run_real()
    else:
        print("\n[2/2] 真上游：跳过（加 --real 运行）")

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r[2])
    print("\n结果: %d/%d 通过" % (passed, total))
    if FAILURES:
        print("失败项：")
        for f in FAILURES:
            print("  - " + f)
    out = TMP / "selftest_result.json"
    out.write_text(json.dumps(
        {"passed": passed, "total": total,
         "items": [{"name": n, "category": c, "ok": o, "detail": d} for n, c, o, d in RESULTS]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print("明细: %s" % out)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
