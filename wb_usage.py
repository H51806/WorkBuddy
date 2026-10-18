#!/usr/bin/env python3
"""
WorkBuddy 统一管理器 - Token 用量统计
=====================================
记录每一次**经本网关转发**的模型请求的 Token 用量，并按
「天 / 小时 / 区域 / 客户端 / 模型 / 账号」聚合成面板需要的形状。

设计要点
--------
* 只统计经过本网关的流量。桌面 App 等客户端直连上游的用量本进程看不到，
  所以客户端维度是「按调用来源的 UA / Header 归类」的近似值，UI 必须如实标注。
* 上游在 SSE 的最后一个 chunk 里回传 usage（网关已下发
  stream_options.include_usage），所以流式请求也要落账：用 StreamUsageTap
  边透传边抓取，不缓冲整个响应体。
* 存储是单文件 data/usage.json：紧凑数组 + 自描述 fields 表，
  避免十万级请求把文件撑大；按 retention_days 逐日清理。

用法
----
    from wb_usage import get_store
    st = get_store()
    st.record(account_id="...", region="cn", client="dsh", model="glm-5.3",
              usage={"prompt_tokens": 100, "completion_tokens": 20,
                     "cachedTokens": 90}, duration_ms=1200)
    st.summary(range_key="7d", region="merged")
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ===== 路径与常量 =====

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("WORKBUDDY_DATA_DIR") or (BASE_DIR / "data"))
USAGE_FILE = DATA_DIR / "usage.json"

CN_TZ = timezone(timedelta(hours=8))

#: 存储格式版本。v4 是上游 Go 面板留下的空壳（buckets 为列表，从未写入），
#: v5 改成分桶字典 + 自描述字段表。
USAGE_VERSION = 5

#: buckets 里每条记录的分量顺序，与文件里的 fields 表一一对应。
BUCKET_FIELDS: Tuple[str, ...] = (
    "requests", "success", "failures", "input", "output",
    "cached_read", "cache_write", "cache_miss", "duration_ms",
)
#: accounts 里每条记录的分量顺序。
ACCOUNT_FIELDS: Tuple[str, ...] = (
    "requests", "success", "failures", "input", "output", "cached_read",
)

#: 面板上的客户端标签页顺序（对应图三顶部的四个标签）。
CLIENT_ORDER: Tuple[str, ...] = (
    "workbuddy", "codebuddy-cli", "codebuddy-ide", "workbuddy-ai",
    "dsh", "api", "other",
)
CLIENT_LABELS: Dict[str, str] = {
    "workbuddy": "WorkBuddy",
    "codebuddy-cli": "CodeBuddy CLI",
    "codebuddy-ide": "CodeBuddy IDE",
    "workbuddy-ai": "WorkBuddy AI",
    "dsh": "DSH / Harness",
    "api": "API 调用",
    "other": "其他",
}

#: 时间范围键：今天 / 近 7 天 / 近 30 天 / 总计。
RANGE_KEYS: Tuple[str, ...] = ("today", "7d", "30d", "all")
RANGE_LABELS: Dict[str, str] = {
    "today": "今天", "7d": "近 7 天", "30d": "近 30 天", "all": "总计",
}

#: UA 归类规则，按特异性从高到低排列。全部是**启发式**，
#: 客户端可用请求头 x-workbuddy-client 显式覆盖。
CLIENT_RULES: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("codebuddy-cli", ("codebuddy-cli", "codebuddycli", "cb-cli")),
    ("codebuddy-ide", ("codebuddy", "codebuddyide")),
    ("workbuddy-ai", ("workbuddy-ai", "workbuddy ai", "workbuddyai")),
    ("workbuddy", ("workbuddy/", "workbuddy ")),
    ("dsh", (
        "deepseek-harness", "dsh/", "dsh-", "pi-ai",
        "openai-python", "openai-node", "httpx", "undici", "node-fetch",
    )),
    ("api", (
        "curl/", "postmanruntime", "insomnia", "okhttp",
        "axios", "go-http-client", "python-urllib", "java/",
    )),
)


# ===== 时间与解析小工具 =====

def now_ts() -> float:
    return time.time()


def day_hour(ts: Optional[float] = None) -> Tuple[str, int]:
    """把时间戳换算成北京时间下的 (YYYY-MM-DD, 小时)。"""
    dt = datetime.fromtimestamp(ts if ts is not None else time.time(), CN_TZ)
    return dt.strftime("%Y-%m-%d"), dt.hour


def fmt_day(day: str) -> str:
    """把 YYYY-MM-DD 缩成 MM-DD 给坐标轴用。"""
    parts = str(day).split("-")
    return "-".join(parts[1:]) if len(parts) == 3 else str(day)


def parse_duration(value: Any, default: float) -> float:
    """解析 "30m" / "2h" / "600s" / 600 这类时长，返回秒。"""
    if isinstance(value, bool):
        return float(default)
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().lower()
    if not text:
        return float(default)
    unit = text[-1]
    try:
        number = float(text[:-1]) if unit.isalpha() else float(text)
    except ValueError:
        return float(default)
    if unit == "s":
        return number
    if unit == "m":
        return number * 60.0
    if unit == "h":
        return number * 3600.0
    if unit == "d":
        return number * 86400.0
    return number


def header_get(headers: Any, name: str) -> str:
    """大小写不敏感地取一个请求头。headers 可以是 dict 或 email.message 风格。"""
    if headers is None:
        return ""
    want = name.lower()
    try:
        items = headers.items()
    except AttributeError:
        return ""
    for key, value in items:
        if str(key).lower() == want:
            return str(value or "")
    return ""


def detect_client(user_agent: str = "", headers: Any = None) -> str:
    """按请求来源归类客户端。启发式，可用 x-workbuddy-client 显式覆盖。"""
    explicit = header_get(headers, "x-workbuddy-client").strip().lower()
    if explicit in CLIENT_LABELS:
        return explicit

    # 桌面 App 的指纹（见 MIGRATION_CHECKLIST：X-Product: SaaS + WorkBuddy/5.5.6）。
    if header_get(headers, "x-product").strip().lower() == "saas":
        return "workbuddy"
    # 网页版指纹（Chrome UA + x-client-platform: web）。
    if header_get(headers, "x-client-platform").strip().lower() == "web":
        return "workbuddy-ai"

    ua = str(user_agent or "").lower()
    if ua:
        for client, needles in CLIENT_RULES:
            for needle in needles:
                if needle in ua:
                    return client
    return "other"


def _pick_int(source: Any, *keys: str) -> int:
    """从字典里按顺序取第一个整数型字段。"""
    if not isinstance(source, dict):
        return 0
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def normalize_usage(usage: Any) -> Optional[Dict[str, int]]:
    """把上游五花八门的 usage 结构归一化成统一字段。

    上游同时存在两套命名：OpenAI 风格的 prompt_tokens / completion_tokens，
    和 WorkBuddy 自己的 cachedTokens / cachedWriteTokens / cachedMissTokens。
    语义按「缓存读取已包含在输入里」处理（与面板口径一致）：
        新增输入 = 输入 - 缓存读取
        缓存命中率 = 缓存读取 / 输入
    """
    if not isinstance(usage, dict) or not usage:
        return None

    details = usage.get("prompt_tokens_details")
    if not isinstance(details, dict):
        details = {}

    input_tokens = _pick_int(usage, "prompt_tokens", "input_tokens")
    output_tokens = _pick_int(usage, "completion_tokens", "output_tokens")
    total_tokens = _pick_int(usage, "total_tokens")

    cached_read = _pick_int(
        usage, "cachedTokens", "cached_tokens",
        "cache_read_input_tokens", "cache_read_tokens",
    )
    if not cached_read:
        cached_read = _pick_int(details, "cached_tokens", "cache_read_tokens")

    cache_write = _pick_int(
        usage, "cachedWriteTokens", "cache_creation_input_tokens", "cache_write_tokens",
    )
    cache_miss = _pick_int(usage, "cachedMissTokens", "cache_miss_tokens")

    if not input_tokens and total_tokens:
        input_tokens = max(total_tokens - output_tokens, 0)
    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    if not cache_miss and input_tokens:
        cache_miss = max(input_tokens - cached_read, 0)

    if not any((input_tokens, output_tokens, cached_read, cache_write)):
        return None

    return {
        "input": input_tokens,
        "output": output_tokens,
        "total": total_tokens,
        "cached_read": cached_read,
        "cache_write": cache_write,
        "cache_miss": cache_miss,
    }


class StreamUsageTap:
    """边透传边从 SSE 流里抓 usage / model，不缓冲整个响应。

    网关按 1KB 读上游，SSE 帧可能被切断，所以这里保留一段滚动尾部缓冲，
    只在完整帧（以空行分隔）上解析。缓冲上限 16KB，防止长回复把内存吃光。
    """

    MAX_TAIL = 16384

    def __init__(self) -> None:
        self.usage: Optional[Dict[str, int]] = None
        self.model: str = ""
        self.frames: int = 0
        self.bytes: int = 0
        self._tail: str = ""
        self._finished: bool = False

    def feed(self, chunk: bytes) -> None:
        """喂入一段刚透传给客户端的原始字节。"""
        if not chunk or self._finished:
            return
        self.bytes += len(chunk)
        try:
            text = chunk.decode("utf-8", "ignore")
        except Exception:
            return
        if "\r" in text:
            text = text.replace("\r\n", "\n").replace("\r", "\n")
        self._tail += text
        while True:
            index = self._tail.find("\n\n")
            if index < 0:
                break
            frame, self._tail = self._tail[:index], self._tail[index + 2:]
            self._consume(frame)
        if len(self._tail) > self.MAX_TAIL:
            self._tail = self._tail[-self.MAX_TAIL:]

    def finish(self) -> Optional[Dict[str, int]]:
        """流结束：收尾解析剩余缓冲并返回最终 usage。"""
        if self._tail.strip():
            self._consume(self._tail)
        self._tail = ""
        self._finished = True
        return self.usage

    def _consume(self, frame: str) -> None:
        for line in frame.split("\n"):
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            self.frames += 1
            if not self.model:
                model = obj.get("model")
                if isinstance(model, str):
                    self.model = model
            candidate = obj.get("usage")
            normalized = normalize_usage(candidate)
            if normalized:
                self.usage = normalized


# ===== 存储 =====

class UsageStore:
    """data/usage.json 的读写与聚合。

    线程安全：网关是 ThreadingHTTPServer，多请求并发落账。
    写盘用「脏标记 + 最小间隔」节流，避免每来一个请求就 fsync 一次。
    """

    def __init__(
        self,
        path: Path = USAGE_FILE,
        retention_days: int = 90,
        flush_interval: float = 5.0,
        logger: Any = None,
    ) -> None:
        self.path = Path(path)
        self.retention_days = int(retention_days)
        self.flush_interval = float(flush_interval)
        self._lock = threading.RLock()
        self._buckets: Dict[str, List[int]] = {}
        self._accounts: Dict[str, List[int]] = {}
        self._ua_seen: Dict[str, int] = {}
        self._dirty = False
        self._last_flush = 0.0
        self._dropped = 0
        self._log = logger
        self.load()

    # ----- 内部日志 -----
    def _warn(self, message: str) -> None:
        if self._log is not None:
            try:
                self._log.warning("[usage] %s", message)
                return
            except Exception:
                pass
        print("[usage] " + message)

    # ----- 读写 -----
    def load(self) -> None:
        with self._lock:
            self._buckets = {}
            self._accounts = {}
            self._ua_seen = {}
            if not self.path.exists():
                return
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except Exception as exc:
                # 坏文件不能让网关起不来：备份后重新开始。
                backup = self.path.with_suffix(".corrupt.json")
                try:
                    if backup.exists():
                        backup.unlink()
                    self.path.rename(backup)
                    self._warn("usage.json 解析失败(%s)，已备份到 %s" % (exc, backup.name))
                except Exception:
                    self._warn("usage.json 解析失败(%s)，已忽略" % exc)
                return
            if not isinstance(data, dict):
                return

            buckets = data.get("buckets")
            if isinstance(buckets, dict):
                for key, value in buckets.items():
                    row = self._as_row(value, BUCKET_FIELDS)
                    if row is not None:
                        self._buckets[str(key)] = row
            elif isinstance(buckets, list) and buckets:
                # v4 及更早：列表形式。上游 Go 面板的空壳是 []，非空则尽量兼容读取。
                for item in buckets:
                    if not isinstance(item, dict):
                        continue
                    key = self._legacy_key(item)
                    if key and key not in self._buckets:
                        self._buckets[key] = self._legacy_row(item)

            accounts = data.get("accounts")
            if isinstance(accounts, dict):
                for key, value in accounts.items():
                    row = self._as_row(value, ACCOUNT_FIELDS)
                    if row is not None:
                        self._accounts[str(key)] = row

            seen = data.get("ua_seen")
            if isinstance(seen, dict):
                for key, value in seen.items():
                    if isinstance(value, (int, float)):
                        self._ua_seen[str(key)[:160]] = int(value)

    @staticmethod
    def _as_row(value: Any, fields: Tuple[str, ...]) -> Optional[List[int]]:
        """把 [{field: n}] 或 [[n]] 都读成定长数组。"""
        if isinstance(value, list):
            row = [0] * len(fields)
            for index in range(min(len(value), len(fields))):
                item = value[index]
                if isinstance(item, bool):
                    continue
                if isinstance(item, (int, float)):
                    row[index] = int(item)
            return row
        if isinstance(value, dict):
            return [int(value.get(name) or 0) for name in fields]
        return None

    def _legacy_key(self, item: Dict[str, Any]) -> str:
        day = str(item.get("day") or "")
        if not day:
            return ""
        return "|".join((
            day,
            str(item.get("hour", 0)),
            str(item.get("region") or "cn"),
            str(item.get("client") or "other"),
            str(item.get("model") or "unknown"),
        ))

    def _legacy_row(self, item: Dict[str, Any]) -> List[int]:
        return [int(item.get(name) or 0) for name in BUCKET_FIELDS]

    def save(self) -> bool:
        """原子写盘。返回是否真的写了。"""
        with self._lock:
            if not self._dirty:
                return False
            payload = {
                "version": USAGE_VERSION,
                "saved": datetime.now(CN_TZ).isoformat(timespec="seconds"),
                "tz": "+08:00",
                "fields": list(BUCKET_FIELDS),
                "account_fields": list(ACCOUNT_FIELDS),
                "retention_days": self.retention_days,
                "buckets": self._buckets,
                "accounts": self._accounts,
                "ua_seen": self._ua_seen,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            try:
                with open(temp, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                os.replace(temp, self.path)
            except Exception as exc:
                self._warn("写入失败: %s" % exc)
                try:
                    if temp.exists():
                        temp.unlink()
                except Exception:
                    pass
                return False
            self._dirty = False
            self._last_flush = time.time()
            return True

    def flush(self) -> bool:
        return self.save()

    # ----- 落账 -----
    def record(
        self,
        account_id: str = "",
        region: str = "cn",
        client: str = "other",
        model: str = "unknown",
        usage: Any = None,
        success: bool = True,
        duration_ms: int = 0,
        user_agent: str = "",
        ts: Optional[float] = None,
        count_request: bool = True,
    ) -> bool:
        """记一笔请求用量。usage 为 None 时仍然计入调用次数（拿不到用量也要计次）。"""
        normalized = normalize_usage(usage)
        stamp = ts if ts is not None else time.time()
        day, hour = day_hour(stamp)
        region = str(region or "cn")
        client = str(client or "other")
        model = str(model or "unknown") or "unknown"
        account_id = str(account_id or "")

        with self._lock:
            key = "|".join((day, str(hour), region, client, model))
            row = self._buckets.get(key)
            if row is None:
                row = [0] * len(BUCKET_FIELDS)
                self._buckets[key] = row

            if count_request:
                row[0] += 1
                if success:
                    row[1] += 1
                else:
                    row[2] += 1
            if normalized:
                row[3] += normalized["input"]
                row[4] += normalized["output"]
                row[5] += normalized["cached_read"]
                row[6] += normalized["cache_write"]
                row[7] += normalized["cache_miss"]
            if duration_ms:
                row[8] += int(duration_ms)

            if account_id:
                akey = "|".join((day, region, account_id))
                arow = self._accounts.get(akey)
                if arow is None:
                    arow = [0] * len(ACCOUNT_FIELDS)
                    self._accounts[akey] = arow
                if count_request:
                    arow[0] += 1
                    if success:
                        arow[1] += 1
                    else:
                        arow[2] += 1
                if normalized:
                    arow[3] += normalized["input"]
                    arow[4] += normalized["output"]
                    arow[5] += normalized["cached_read"]

            if user_agent:
                short = str(user_agent)[:160]
                self._ua_seen[short] = self._ua_seen.get(short, 0) + 1

            self._dirty = True
            should_flush = (time.time() - self._last_flush) >= self.flush_interval
        if should_flush:
            self._prune()
            self.save()
        return normalized is not None

    # ----- 清理 -----
    def _prune(self) -> None:
        """按保留天数丢弃过期桶。调用方需已持锁（或接受竞态）。"""
        if self.retention_days <= 0:
            return
        cutoff = (datetime.now(CN_TZ) - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        with self._lock:
            for key in [k for k in self._buckets if k.split("|", 1)[0] < cutoff]:
                del self._buckets[key]
                self._dropped += 1
            for key in [k for k in self._accounts if k.split("|", 1)[0] < cutoff]:
                del self._accounts[key]

    # ----- 聚合 -----
    def _window(self, range_key: str, now: Optional[float] = None) -> Tuple[str, str, str]:
        """返回 (起始日, 结束日, 粒度) —— 粒度是 'hour' 或 'day'。"""
        now = now if now is not None else time.time()
        today = datetime.fromtimestamp(now, CN_TZ)
        today_str = today.strftime("%Y-%m-%d")
        key = range_key if range_key in RANGE_KEYS else "today"
        if key == "today":
            return today_str, today_str, "hour"
        if key == "7d":
            start = (today - timedelta(days=6)).strftime("%Y-%m-%d")
            return start, today_str, "day"
        if key == "30d":
            start = (today - timedelta(days=29)).strftime("%Y-%m-%d")
            return start, today_str, "day"
        return "0000-01-01", today_str, "day"

    def summary(
        self,
        range_key: str = "today",
        region: str = "merged",
        client: Optional[str] = None,
        model: Optional[str] = None,
        now: Optional[float] = None,
    ) -> Dict[str, Any]:
        """按范围 / 区域 / 客户端 / 模型聚合出面板要的全部维度。"""
        start, end, grain = self._window(range_key, now)
        want_region = (region or "merged").strip()
        want_client = (client or "").strip()
        want_model = (model or "").strip()

        totals = self._empty_totals()
        by_model: Dict[str, List[int]] = {}
        by_client: Dict[str, List[int]] = {}
        by_region: Dict[str, List[int]] = {}
        by_account: Dict[str, List[int]] = {}
        slots: Dict[str, List[int]] = {}

        with self._lock:
            bucket_items = list(self._buckets.items())
            account_items = list(self._accounts.items())

        for key, row in bucket_items:
            parts = key.split("|")
            if len(parts) < 5:
                continue
            day, hour, row_region, row_client, row_model = parts[0], parts[1], parts[2], parts[3], parts[4]
            if day < start or day > end:
                continue
            if want_region and want_region != "merged" and row_region != want_region:
                continue
            if want_client and row_client != want_client:
                continue
            if want_model and row_model != want_model:
                continue

            self._accumulate(totals, row)
            self._accumulate_into(by_model, row_model, row)
            self._accumulate_into(by_client, row_client, row)
            self._accumulate_into(by_region, row_region, row)
            slot_key = day if grain == "day" else day + "T" + hour.zfill(2)
            self._accumulate_into(slots, slot_key, row)

        for key, row in account_items:
            parts = key.split("|")
            if len(parts) < 3:
                continue
            day, row_region, account_id = parts[0], parts[1], parts[2]
            if day < start or day > end:
                continue
            if want_region and want_region != "merged" and row_region != want_region:
                continue
            # 账号行只有 ACCOUNT_FIELDS 那么长，不能直接喂给按
            # BUCKET_FIELDS 索引的 _accumulate，否则会 IndexError。
            account_totals = by_account.setdefault(account_id, self._empty_totals())
            for index, name in enumerate(ACCOUNT_FIELDS):
                account_totals[name] = account_totals.get(name, 0) + int(row[index])

        return {
            "range": range_key if range_key in RANGE_KEYS else "today",
            "region": want_region or "merged",
            "client": want_client,
            "model": want_model,
            "grain": grain,
            "since": start,
            "until": end,
            "totals": self._finalize(totals),
            "by_model": self._finalize_map(by_model, order_by_total=True),
            "by_client": self._finalize_map(by_client, prefer_order=CLIENT_ORDER),
            "by_region": self._finalize_map(by_region, prefer_order=("cn", "global")),
            "by_account": self._finalize_map(by_account, order_by_total=True),
            "series": self._finalize_series(slots, grain),
            "clients": [
                {"key": key, "label": CLIENT_LABELS.get(key, key)}
                for key in CLIENT_ORDER
            ],
        }

    @staticmethod
    def _empty_totals() -> Dict[str, int]:
        return {name: 0 for name in BUCKET_FIELDS}

    @staticmethod
    def _accumulate(target: Dict[str, int], row: Iterable[int]) -> None:
        for index, name in enumerate(BUCKET_FIELDS):
            target[name] = target.get(name, 0) + int(row[index])

    @staticmethod
    def _accumulate_into(store: Dict[str, List[int]], key: str, row: Iterable[int]) -> None:
        bucket = store.get(key)
        if bucket is None:
            bucket = [0] * len(BUCKET_FIELDS)
            store[key] = bucket
        for index in range(len(BUCKET_FIELDS)):
            bucket[index] += int(row[index])

    @staticmethod
    def _finalize(totals: Dict[str, int]) -> Dict[str, Any]:
        """补上面板要的派生量：总 Token、命中率、四项占比。"""
        input_tokens = int(totals.get("input") or 0)
        output_tokens = int(totals.get("output") or 0)
        cached_read = int(totals.get("cached_read") or 0)
        cache_write = int(totals.get("cache_write") or 0)
        cache_miss = int(totals.get("cache_miss") or 0)
        if not cache_miss and input_tokens:
            cache_miss = max(input_tokens - cached_read, 0)
        total_tokens = input_tokens + output_tokens
        basis = cached_read + cache_miss
        return {
            "requests": int(totals.get("requests") or 0),
            "success": int(totals.get("success") or 0),
            "failures": int(totals.get("failures") or 0),
            "input": input_tokens,
            "output": output_tokens,
            "total": total_tokens,
            "cached_read": cached_read,
            "cache_write": cache_write,
            "cache_miss": cache_miss,
            "cache_hit_rate": round(cached_read / basis, 4) if basis > 0 else 0.0,
            "duration_ms": int(totals.get("duration_ms") or 0),
            "share": {
                "cached_read": round(cached_read / total_tokens, 4) if total_tokens else 0.0,
                "cache_miss": round(cache_miss / total_tokens, 4) if total_tokens else 0.0,
                "output": round(output_tokens / total_tokens, 4) if total_tokens else 0.0,
                "cache_write": round(cache_write / total_tokens, 4) if total_tokens else 0.0,
            },
        }

    def _finalize_map(
        self,
        store: Dict[str, List[int]],
        prefer_order: Optional[Tuple[str, ...]] = None,
        order_by_total: bool = False,
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for key, bucket in store.items():
            totals = self._empty_totals()
            # by_account 的分桶是按字段名存的 dict（见 _empty_totals），其余维度是
            # 按 BUCKET_FIELDS 顺序存的 list。两种形状都要认，否则 by_account
            # 会在 bucket[0] 上抛 KeyError: 0，整个 /panel/api/usage 500。
            for index, name in enumerate(BUCKET_FIELDS):
                if isinstance(bucket, dict):
                    totals[name] = int(bucket.get(name) or 0)
                else:
                    totals[name] = int(bucket[index] or 0)
            entry = self._finalize(totals)
            entry["key"] = key
            entry["label"] = CLIENT_LABELS.get(key, key)
            rows.append(entry)
        if prefer_order:
            ranks = {name: index for index, name in enumerate(prefer_order)}
            rows.sort(key=lambda item: (ranks.get(item["key"], 999), -item["total"]))
        elif order_by_total:
            rows.sort(key=lambda item: -item["total"])
        else:
            rows.sort(key=lambda item: item["key"])
        return rows

    def _finalize_series(self, slots: Dict[str, List[int]], grain: str) -> List[Dict[str, Any]]:
        """把分桶摊平成连续的序列，缺失的时间点补零，否则图表会错位。"""
        series: List[Dict[str, Any]] = []
        for key in sorted(slots.keys()):
            totals = self._empty_totals()
            bucket = slots[key]
            for index, name in enumerate(BUCKET_FIELDS):
                if isinstance(bucket, dict):
                    totals[name] = int(bucket.get(name) or 0)
                else:
                    totals[name] = int(bucket[index] or 0)
            entry = self._finalize(totals)
            if grain == "hour":
                day, _, hour = key.partition("T")
                entry["slot"] = key
                entry["day"] = day
                entry["hour"] = int(hour or 0)
                entry["label"] = "%s:00" % (hour or "00")
            else:
                entry["slot"] = key
                entry["day"] = key
                entry["hour"] = None
                entry["label"] = fmt_day(key)
            series.append(entry)
        return series

    # ----- 诊断 -----
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": USAGE_VERSION,
                "path": str(self.path),
                "buckets": len(self._buckets),
                "accounts": len(self._accounts),
                "retention_days": self.retention_days,
                "dirty": self._dirty,
                "dropped": self._dropped,
                "ua_seen": len(self._ua_seen),
                "size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            }

    def top_user_agents(self, limit: int = 12) -> List[Dict[str, Any]]:
        """面板上「未知来源」排查用：最近见过的 UA 计数。"""
        with self._lock:
            items = sorted(self._ua_seen.items(), key=lambda kv: -kv[1])[: max(0, int(limit))]
        return [{"user_agent": key, "count": value} for key, value in items]


# ===== 模块级默认实例 =====

_store: Optional[UsageStore] = None
_store_lock = threading.Lock()


def get_store() -> UsageStore:
    """拿全局默认 UsageStore（线程安全地懒加载）。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = UsageStore()
    return _store


def configure(
    path: Any = None,
    retention_days: Any = None,
    logger: Any = None,
    flush_interval: Any = None,
) -> UsageStore:
    """按 config 重建默认实例（网关 load_config 后调用）。"""
    global _store
    with _store_lock:
        if _store is not None:
            try:
                _store.flush()
            except Exception:
                pass
        resolved = Path(path) if path else USAGE_FILE
        try:
            interval = float(flush_interval) if flush_interval else 5.0
        except (TypeError, ValueError):
            interval = 5.0
        _store = UsageStore(
            path=resolved,
            retention_days=int(retention_days) if retention_days else 90,
            flush_interval=interval,
            logger=logger,
        )
        return _store


def record_request(**kwargs: Any) -> bool:
    return get_store().record(**kwargs)


def summary(**kwargs: Any) -> Dict[str, Any]:
    return get_store().summary(**kwargs)


def flush() -> bool:
    if _store is None:
        return False
    return _store.flush()
