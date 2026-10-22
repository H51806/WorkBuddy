# -*- coding: utf-8 -*-
"""回归测试：上游连续反探测拦截（code=11128）时的自保行为。

跑法：python devtools/test_antiprobe_storm.py

覆盖三点：
1. 全部账号都被 11128 拦住时，网关回 429 + Retry-After（不是 503「服务不可用」）；
2. 30 秒内累计够 ANTIPROBE_STORM_N 次拦截后打开全局熔断；
3. 熔断期间不再打上游（请求立刻返回，桩计数不增长）。
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

import wb_gateway as g  # noqa: E402

UPSTREAM_HITS = Counter()
STUB_PORT = 7899
GW_PORT = 7898
BODY_11128 = json.dumps({
    "code": 11128,
    "msg": "Illegal API invocation from an unapproved channel",
    "displayMsg": {"en": "Request blocked. Please send it again",
                   "zh": "请求被拦截，请重新发送"},
}).encode()


class StubUpstream(BaseHTTPRequestHandler):
    """一律返回 11128，模拟上游反探测封禁窗口。"""

    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
        except Exception:
            pass
        UPSTREAM_HITS[self.path] += 1
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(BODY_11128)))
        self.end_headers()
        self.wfile.write(BODY_11128)


def start_servers():
    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), StubUpstream)
    stub.daemon_threads = True
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    g.config.setdefault("upstream", {})["base_url"] = "http://127.0.0.1:%d" % STUB_PORT
    g.api_key = "test_key"
    g.wb_usage = None                      # 别把测试写进真实用量
    g._ANTIPROBE_EVENTS.clear()
    g._ANTIPROBE_BLOCK_UNTIL = 0.0

    pool = g.pool
    pool.accounts.clear()
    pool.current_account_id = None
    for i in (1, 2):
        acc = g.Account(id="test-account-%d" % i, name="测试号%d" % i,
                        token="fake-token-%d" % i, domain="www.codebuddy.cn",
                        credits=500)
        pool.accounts[acc.id] = acc
    if pool.current_account_id is None:
        pool.current_account_id = "test-account-1"
    pool.distribution = "priority"
    pool.region = "merged"
    pool.credit_floor = 0.0

    srv = ThreadingHTTPServer(("127.0.0.1", GW_PORT), g.GatewayHandler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.3)
    return srv


def send(timeout=60):
    body = {"model": "deepseek-v4-flash", "stream": False,
            "messages": [{"role": "user", "content": "hi"}]}
    req = urllib.request.Request(
        "http://127.0.0.1:%d/v1/chat/completions" % GW_PORT,
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer test_key", "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(600).decode("utf-8", "replace"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read(600).decode("utf-8", "replace"), time.time() - t0


def main():
    start_servers()
    failures = []

    st, hd, txt, cost = send()
    if st != 429:
        failures.append("首个请求应为 429（临时拦截），实际 %s: %s" % (st, txt[:120]))
    if not hd.get("Retry-After"):
        failures.append("429 必须带 Retry-After 头，实际没有")
    if "反探测" not in txt:
        failures.append("429 文案未说明是上游反探测拦截: %s" % txt[:120])

    # 继续打到熔断打开
    storm_opened = False
    for _ in range(4):
        st, hd, txt, _c = send()
        if g.antiprobe_block_left() > 0:
            storm_opened = True
            break
    if not storm_opened:
        failures.append("连续拦截后未打开全局熔断")

    hits_before = UPSTREAM_HITS["/v2/chat/completions"]
    st, hd, txt, cost = send()
    hits_after = UPSTREAM_HITS["/v2/chat/completions"]
    if st != 429:
        failures.append("熔断期间应回 429，实际 %s" % st)
    if cost > 1.0:
        failures.append("熔断期间应立刻返回，实际耗时 %.1fs" % cost)
    if hits_after != hits_before:
        failures.append("熔断期间仍在打上游（%d -> %d）" % (hits_before, hits_after))

    if failures:
        print("FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("PASS: 429+Retry-After、全局熔断、熔断期间不打上游")
    print("      上游被调用 %d 次即止损（期间共 %d 个客户端请求）"
          % (hits_after, 6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
