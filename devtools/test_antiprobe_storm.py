# -*- coding: utf-8 -*-
"""回归测试：上游连续反探测拦截（code=11128）时的自保行为。

跑法：python devtools/test_antiprobe_storm.py

覆盖三点：
1. 全部账号都被 11128 拦住时，网关回 429 + Retry-After（不是 503「服务不可用」）；
2. 30 秒内累计够 ANTIPROBE_STORM_N 次拦截后打开全局熔断；
3. 熔断期间不再打上游（请求立刻返回，桩计数不增长）。
"""
import json
import re
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
    g._ANTIPROBE_BLOCK_LEVEL = 0

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

    # 文案/头一致性：不能出现「请 0 秒后重试」，Retry-After 至少 1
    # 注意用正则而不是 "0 秒后" 子串：20 秒 / 40 秒 里也含 "0 秒"。
    if re.search(r"请\s*0\s*秒", txt):
        failures.append("429 文案印出了 0 秒: %s" % txt[:120])
    ra = hd.get("Retry-After")
    try:
        if ra is not None and int(ra) < 1:
            failures.append("Retry-After 必须 >= 1，实际 %s" % ra)
    except (TypeError, ValueError):
        failures.append("Retry-After 不是整数: %r" % (ra,))

    # 退避阶梯：第二轮翻倍，成功一次回到第一档
    def _trip() -> float:
        """清掉本轮熔断后重新打到熔断，返回这一档还剩多少秒。"""
        g._ANTIPROBE_BLOCK_UNTIL = 0.0
        g._ANTIPROBE_EVENTS.clear()
        for _ in range(g.ANTIPROBE_STORM_N):
            g.note_antiprobe()
        return g.antiprobe_block_left()

    # 「剩 0.3 秒」时文案不能退化成「请 0 秒后重试」
    g._ANTIPROBE_BLOCK_UNTIL = time.time() + 0.3
    if g.antiprobe_retry_after() < 1:
        failures.append("熔断剩 0.3s 时 Retry-After 仍应 >= 1，实际 %s"
                        % g.antiprobe_retry_after())
    g._ANTIPROBE_BLOCK_UNTIL = 0.0

    g._ANTIPROBE_BLOCK_LEVEL = 0        # 阶梯从第一档开始算
    lvl1 = _trip()
    lvl2 = _trip()
    if not (lvl1 * 1.7 <= lvl2 <= lvl1 * 2.3):
        failures.append("第二轮熔断应翻倍（%.0f -> %.0f）" % (lvl1, lvl2))
    g.clear_antiprobe_backoff()
    lvl1_again = _trip()
    if not (lvl1 * 0.7 <= lvl1_again <= lvl1 * 1.3):
        failures.append("成功一次后阶梯应回到第一档（%.0f -> %.0f）" % (lvl1, lvl1_again))
    if lvl2 > g.ANTIPROBE_BLOCK_MAX:
        failures.append("熔断不得超过 ANTIPROBE_BLOCK_MAX（%.0f > %.0f）"
                        % (lvl2, g.ANTIPROBE_BLOCK_MAX))

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
