"""WorkBuddy 模型目录（wb_models）。

职责：
  1. 模型元数据表 —— 上下文窗口、输出上限、图片能力、推理档位、积分倍率、区域可用性；
  2. 区域分区 —— 国内版（copilot.tencent.com / codebuddy.cn）与国际版（workbuddy.ai）各自独立；
  3. 模型开关过滤 —— 可隐藏模型，使其从模型选择器与 /v1/models 里消失；
  4. 免费行 -> 付费行退路 —— 上游按「行」限流，被限流时点名同名的付费行；
  5. 促销徽章 —— 限时免费 / 夜间折扣 / 限时折扣，带生效时段，过期自动撤销。

诚实边界（必须传达给使用方）：
  - 本模块内置的目录只包含「确实知道」的身份信息（模型 id、区域、已知的推理档位与倍率）。
    上下文窗口 / 输出上限等未知字段一律留 None，绝不用猜测值填充。
  - 上游模型列表接口是这些字段的唯一权威来源。取到之后用 merge_discovered() 落盘，
    /v1/models 会标注每个字段来自 upstream 还是 builtin。
  - 促销徽章来自服务端 modelPromotions，内置表默认为空：没有证据就不显示徽章。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ===== 路径与常量 =====

BASE_DIR = Path(__file__).parent.parent  # 仓库根（本文件在 core/ 下）


def _default_data_dir() -> Path:
    override = os.environ.get("WORKBUDDY_DATA_DIR")
    return Path(override) if override else (BASE_DIR / "data")


DATA_DIR = _default_data_dir()
MODELS_FILE = DATA_DIR / "models.json"

CATALOG_VERSION = 1
CN_TZ = timezone(timedelta(hours=8))

REGIONS = ("cn", "global")
REGION_LABELS = {"cn": "国内版", "global": "国际版"}
REGION_MERGED = "merged"

# 兜底模型：请求了一个未知 / 被隐藏 / 已下架的模型时改用它。
DEFAULT_MODEL_CN = "deepseek-v4-flash"
DEFAULT_MODEL_GLOBAL = "deepseek-v4.1-flash"

# 目录字段的来源标记
SOURCE_BUILTIN = "builtin"
SOURCE_UPSTREAM = "upstream"


# ===== 模型条目 =====

@dataclass
class ModelSpec:
    """一个模型的已知事实。未知一律 None / 空，不做猜测。"""

    id: str
    name: str = ""
    regions: Tuple[str, ...] = ("cn", "global")
    context_window: Optional[int] = None
    max_tokens: Optional[int] = None
    supports_image: Optional[bool] = None
    reasoning_efforts: Tuple[str, ...] = ()
    rate: Optional[float] = None
    family: str = ""
    tier: str = ""                      # "" | "free" | "promo" | "paid"
    note: str = ""
    twins: Dict[str, str] = field(default_factory=dict)   # region -> 同名的另一「行」id

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["regions"] = list(self.regions)
        data["reasoning_efforts"] = list(self.reasoning_efforts)
        return data


def _spec(**kwargs) -> ModelSpec:
    return ModelSpec(**kwargs)


# 内置目录：只写有证据的事实。
#   - 区域：README 明说国内版/国际版各自的模型与「行」；
#   - 推理档位：README 明说 GLM-5.3 / GLM-5.3-Flash 有 low/high/max；
#   - 图片能力：README 明说多数模型收图，GLM-5.1 等纯文本模型明确提示不支持；
#   - 倍率：README 举例 GLM-5.2 为 x0.79；
#   - twins：README 明说被限流时退到同名付费行（国内 hy3 -> hy3-x；国际 deepseek-v4.1-flash -> -sg）。
_BUILTIN: Tuple[ModelSpec, ...] = (
    # ---- DeepSeek 系 ----
    _spec(id="deepseek-v4-flash", name="DeepSeek-V4-Flash", regions=("cn", "global"),
          family="deepseek", tier="paid", supports_image=True,
          note="网关默认兜底模型"),
    _spec(id="deepseek-v4.1-flash", name="DeepSeek-V4.1-Flash", regions=("cn", "global"),
          family="deepseek", tier="paid", supports_image=True,
          twins={"global": "deepseek-v4.1-flash-sg"},
          note="国际版被限流时退到 deepseek-v4.1-flash-sg"),
    _spec(id="deepseek-v4.1-flash-sg", name="DeepSeek-V4.1-Flash (SG)", regions=("global",),
          family="deepseek", tier="paid",
          note="国际版同名的另一条「行」，限流退路"),
    _spec(id="deepseek-v4-pro", name="DeepSeek-V4-Pro", regions=("cn", "global"),
          family="deepseek", tier="paid", supports_image=True),
    _spec(id="deepseek-v3", name="DeepSeek-V3", regions=("cn", "global"),
          family="deepseek", tier="paid"),
    _spec(id="deepseek-v3.2", name="DeepSeek-V3.2", regions=("cn", "global"),
          family="deepseek", tier="paid"),
    _spec(id="deepseek-r1", name="DeepSeek-R1", regions=("cn", "global"),
          family="deepseek", tier="paid", reasoning_efforts=("low", "high")),
    # ---- GLM 系 ----
    _spec(id="glm-5.3", name="GLM-5.3", regions=("cn", "global"),
          family="glm", tier="paid", supports_image=True,
          reasoning_efforts=("low", "high", "max"),
          note="README 明说带 low/high/max 推理档位"),
    _spec(id="glm-5.3-flash", name="GLM-5.3-Flash", regions=("cn", "global"),
          family="glm", tier="paid", supports_image=True,
          reasoning_efforts=("low", "high", "max")),
    _spec(id="glm-5.2", name="GLM-5.2", regions=("cn", "global"),
          family="glm", tier="paid", supports_image=True, rate=0.79,
          note="README 举例倍率为 x0.79"),
    _spec(id="glm-5.1", name="GLM-5.1", regions=("cn", "global"),
          family="glm", tier="paid", supports_image=False,
          note="纯文本模型，README 明说不支持发图"),
    # ---- 其他厂商 ----
    _spec(id="kimi-k3", name="Kimi-K3", regions=("cn", "global"), family="kimi", tier="paid"),
    _spec(id="minimax-m3", name="MiniMax-M3", regions=("cn", "global"), family="minimax", tier="paid"),
    _spec(id="hy3", name="Hy3", regions=("cn",), family="hunyuan", tier="paid",
          twins={"cn": "hy3-x"}, note="国内版被限流时退到 hy3-x"),
    _spec(id="hy3-x", name="Hy3-X", regions=("cn",), family="hunyuan", tier="paid",
          note="国内版同名的另一条「行」，限流退路"),
    _spec(id="hy4-preview", name="Hy4 preview", regions=("cn", "global"),
          family="hunyuan", tier="promo",
          note="README 举例徽章：Hy4 preview · x0.00 · 限时免费"),
)

_BUILTIN_BY_ID: Dict[str, ModelSpec] = {s.id: s for s in _BUILTIN}


# ===== 基础工具 =====

def now_ts() -> float:
    return time.time()


def normalize_region(region: Optional[str]) -> Optional[str]:
    """把各种写法收敛成 cn / global；merged 与空值返回 None（表示不限区域）。"""
    text = str(region or "").strip().lower()
    if text in ("cn", "china", "国内", "国内版"):
        return "cn"
    if text in ("global", "intl", "international", "国际", "国际版", "overseas"):
        return "global"
    return None


def normalize_id(model: Optional[str]) -> str:
    return str(model or "").strip().lower()


def default_model(region: Optional[str] = None) -> str:
    if normalize_region(region) == "global":
        return DEFAULT_MODEL_GLOBAL
    return DEFAULT_MODEL_CN


# ===== 目录查询 =====

def builtin_specs() -> Dict[str, ModelSpec]:
    return dict(_BUILTIN_BY_ID)


def get_spec(model: Optional[str], region: Optional[str] = None) -> Optional[ModelSpec]:
    """取模型条目（内置 + 上游覆盖合并后的视图）。区域不匹配时返回 None。"""
    mid = normalize_id(model)
    if not mid:
        return None
    return get_prefs().spec(mid, region)


def is_known(model: Optional[str], region: Optional[str] = None) -> bool:
    return get_spec(model, region) is not None


def catalog(region: Optional[str] = None, enabled_only: bool = False) -> List[ModelSpec]:
    """列出某区域的模型条目。region 为空表示不限区域。"""
    return get_prefs().catalog(region, enabled_only=enabled_only)


def model_ids(region: Optional[str] = None, enabled_only: bool = True) -> List[str]:
    return [s.id for s in catalog(region, enabled_only=enabled_only)]


def display_name(model: Optional[str]) -> str:
    spec = get_spec(model)
    return spec.name if spec and spec.name else str(model or "")


def is_image_capable(model: Optional[str]) -> Optional[bool]:
    """图片能力三态：True 收图 / False 纯文本 / None 未知。"""
    spec = get_spec(model)
    return None if spec is None else spec.supports_image


def reasoning_efforts(model: Optional[str]) -> Tuple[str, ...]:
    """已声明的推理档位。空元组表示「未声明」——不等于「不支持」。"""
    spec = get_spec(model)
    return () if spec is None else tuple(spec.reasoning_efforts)


def paid_twin(model: Optional[str], region: Optional[str] = None) -> Optional[str]:
    """被限流时该退到的同名付费「行」id。"""
    spec = get_spec(model)
    if spec is None:
        return None
    if region:
        hit = spec.twins.get(normalize_region(region) or "")
        if hit:
            return hit
    return next(iter(spec.twins.values()), None)


def is_paid_twin(model: Optional[str], other: Optional[str]) -> bool:
    """判断 other 是否是 model 的同名付费行（双向）。"""
    a, b = normalize_id(model), normalize_id(other)
    if not a or not b or a == b:
        return False
    if paid_twin(a) == b:
        return True
    return paid_twin(b) == a


# ===== 促销徽章 =====

@dataclass
class Promotion:
    """一条促销：限时免费 / 夜间折扣 / 限时折扣。

    生效时段是二选一：
      - 绝对时段：start_ts / end_ts（epoch 秒，0 表示不限）；
      - 每日时段：start_hour / end_hour（本地 0-23，end 可小于 start 表示跨天，如 23->8）。
    """

    model: str
    label: str
    kind: str = "promo"          # promo | free | night
    start_ts: float = 0.0
    end_ts: float = 0.0
    start_hour: Optional[int] = None
    end_hour: Optional[int] = None
    note: str = ""
    origin: str = SOURCE_UPSTREAM

    def active_at(self, ts: Optional[float] = None) -> bool:
        now = now_ts() if ts is None else float(ts)
        if self.start_ts and now < self.start_ts:
            return False
        if self.end_ts and now > self.end_ts:
            return False
        if self.start_hour is not None and self.end_hour is not None:
            try:
                local_hour = datetime.fromtimestamp(now, CN_TZ).hour
            except Exception:
                return True
            sh, eh = int(self.start_hour) % 24, int(self.end_hour) % 24
            if sh == eh:
                return True
            if sh < eh:
                return sh <= local_hour < eh
            # 跨天：23 -> 8 表示 23,0..7 点
            return local_hour >= sh or local_hour < eh
        return True

    def window_text(self) -> str:
        if self.start_hour is not None and self.end_hour is not None:
            return "%02d:00-%02d:00" % (int(self.start_hour) % 24, int(self.end_hour) % 24)
        if self.start_ts and self.end_ts:
            fmt = "%Y-%m-%d %H:%M"
            return "%s ~ %s" % (
                datetime.fromtimestamp(self.start_ts, CN_TZ).strftime(fmt),
                datetime.fromtimestamp(self.end_ts, CN_TZ).strftime(fmt),
            )
        if self.end_ts:
            return "至 " + datetime.fromtimestamp(self.end_ts, CN_TZ).strftime("%Y-%m-%d %H:%M")
        if self.start_ts:
            return "自 " + datetime.fromtimestamp(self.start_ts, CN_TZ).strftime("%Y-%m-%d %H:%M")
        return ""

    def to_dict(self, ts: Optional[float] = None) -> Dict[str, Any]:
        return {
            "model": self.model,
            "label": self.label,
            "kind": self.kind,
            "active": self.active_at(ts),
            "window": self.window_text(),
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "start_hour": self.start_hour,
            "end_hour": self.end_hour,
            "note": self.note,
            "origin": self.origin,
        }


def _coerce_promotion(raw: Any) -> Optional[Promotion]:
    if not isinstance(raw, dict):
        return None
    model = normalize_id(raw.get("model") or raw.get("id"))
    label = str(raw.get("label") or raw.get("text") or "").strip()
    kind = str(raw.get("kind") or "promo").strip().lower()
    if not label:
        label = {"free": "限时免费", "night": "夜间折扣", "promo": "限时折扣"}.get(kind, "促销")
    if not model:
        return None

    def _num(key: str, default: Optional[float] = None) -> Optional[float]:
        value = raw.get(key)
        if value is None or value == "":
            return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _hour(key: str) -> Optional[int]:
        value = _num(key)
        return None if value is None else int(value) % 24

    return Promotion(
        model=model,
        label=label,
        kind=kind,
        start_ts=_num("start_ts", 0.0) or 0.0,
        end_ts=_num("end_ts", 0.0) or 0.0,
        start_hour=_hour("start_hour"),
        end_hour=_hour("end_hour"),
        note=str(raw.get("note") or ""),
        origin=str(raw.get("origin") or SOURCE_UPSTREAM),
    )


# ===== 偏好与发现结果的持久化 =====

@dataclass
class _State:
    version: int = CATALOG_VERSION
    updated: float = 0.0
    disabled: Dict[str, List[str]] = field(default_factory=lambda: {r: [] for r in REGIONS})
    overrides: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    discovered: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    promotions: List[Dict[str, Any]] = field(default_factory=list)
    src_files: List[str] = field(default_factory=list)


class ModelPrefs:
    """模型目录的可变部分：开关、上游覆盖、发现结果、促销。线程安全。"""

    def __init__(self, path: Path = MODELS_FILE, logger: Any = None) -> None:
        self.path = Path(path)
        self.logger = logger
        self._lock = threading.RLock()
        self._state = _State()
        self._dirty = False
        self.load()

    # ---- 日志 ----
    def _log(self, level: str, msg: str, *args: Any) -> None:
        if self.logger is None:
            return
        try:
            getattr(self.logger, level)("模型目录 " + msg, *args)
        except Exception:
            pass

    # ---- 读写 ----
    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._state = _State(updated=0.0)
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:
                self._log("warning", "读取失败，改用内置目录: %s", exc)
                self._quarantine()
                self._state = _State()
                return
            state = _State()
            if isinstance(raw, dict):
                state.version = int(raw.get("version") or CATALOG_VERSION)
                state.updated = float(raw.get("updated") or 0.0)
                disabled = raw.get("disabled")
                if isinstance(disabled, dict):
                    for region in REGIONS:
                        values = disabled.get(region)
                        if isinstance(values, list):
                            state.disabled[region] = sorted({normalize_id(v) for v in values if normalize_id(v)})
                for key, target in (("overrides", state.overrides), ("discovered", state.discovered)):
                    block = raw.get(key)
                    if isinstance(block, dict):
                        target.update({normalize_id(k): v for k, v in block.items() if isinstance(v, dict)})
                promos = raw.get("promotions")
                if isinstance(promos, list):
                    for item in promos:
                        promo = _coerce_promotion(item)
                        if promo is not None:
                            state.promotions.append(asdict(promo))
                files = raw.get("src_files")
                if isinstance(files, list):
                    state.src_files = [str(f) for f in files]
            self._state = state

    def _quarantine(self) -> None:
        try:
            if self.path.exists():
                broken = self.path.with_suffix(".corrupt.json")
                os.replace(str(self.path), str(broken))
                self._log("warning", "坏文件已重命名: %s", broken)
        except Exception:
            pass

    def save(self) -> None:
        with self._lock:
            self._state.updated = now_ts()
            payload = asdict(self._state)
            payload["version"] = CATALOG_VERSION
            data = json.dumps(payload, ensure_ascii=False, indent=2)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(data, encoding="utf-8")
                os.replace(str(tmp), str(self.path))
                self._dirty = False
            except Exception as exc:
                self._log("warning", "保存失败: %s", exc)

    def flush(self) -> None:
        if self._dirty:
            self.save()

    # ---- 合并视图 ----
    def _merged(self, mid: str) -> Optional[ModelSpec]:
        base = _BUILTIN_BY_ID.get(mid)
        if base is None:
            # 只认「上游列表里发现过」或「人工覆盖过」的 id。以前这里对任何字符串都凭空
            # 造一个条目，于是 spec() 永不返回 None、resolve_requested() 里
            # unknown -> 兜底模型 的分支成了死代码：坏模型名被原样转发给上游，白挨一个
            # 400（11102 model ... service info not found），还被算成账号失败进而熔断。
            if mid not in self._state.discovered and mid not in self._state.overrides:
                return None
            base = ModelSpec(id=mid, name=mid, regions=("cn", "global"),
                             note="由上游列表发现，内置目录里没有它的身份信息")
        spec = ModelSpec(**base.to_dict())
        spec.regions = tuple(base.regions)
        spec.reasoning_efforts = tuple(base.reasoning_efforts)
        spec.twins = dict(base.twins)
        for block in (self._state.discovered.get(mid), self._state.overrides.get(mid)):
            if not isinstance(block, dict):
                continue
            if block.get("name"):
                spec.name = str(block["name"])
            for key in ("context_window", "max_tokens"):
                value = block.get(key)
                if isinstance(value, (int, float)) and value > 0:
                    setattr(spec, key, int(value))
            if "supports_image" in block and block["supports_image"] is not None:
                spec.supports_image = bool(block["supports_image"])
            efforts = block.get("reasoning_efforts")
            if isinstance(efforts, list) and efforts:
                spec.reasoning_efforts = tuple(str(e) for e in efforts)
            rate = block.get("rate")
            if isinstance(rate, (int, float)):
                spec.rate = float(rate)
            if block.get("regions"):
                regs = tuple(r for r in (normalize_region(x) for x in block["regions"]) if r)
                if regs:
                    spec.regions = regs
            if block.get("tier"):
                spec.tier = str(block["tier"])
            if block.get("note"):
                spec.note = str(block["note"])
        return spec

    def spec(self, mid: str, region: Optional[str] = None) -> Optional[ModelSpec]:
        with self._lock:
            spec = self._merged(mid)
        if spec is None:
            return None
        target = normalize_region(region)
        if target and target not in spec.regions:
            return None
        return spec

    def all_specs(self, region: Optional[str] = None) -> List[ModelSpec]:
        with self._lock:
            ids = list(_BUILTIN_BY_ID.keys())
            for extra in self._state.discovered.keys():
                if extra not in _BUILTIN_BY_ID:
                    ids.append(extra)
            specs = [s for s in (self._merged(mid) for mid in ids) if s is not None]
        target = normalize_region(region)
        if target:
            specs = [s for s in specs if target in s.regions]
        return sorted(specs, key=lambda s: (s.family, s.id))

    def catalog(self, region: Optional[str] = None, enabled_only: bool = False) -> List[ModelSpec]:
        specs = self.all_specs(region)
        if enabled_only:
            target = normalize_region(region)
            specs = [s for s in specs if self.is_enabled(s.id, target)]
        return specs

    # ---- 开关 ----
    def is_enabled(self, model: Optional[str], region: Optional[str] = None) -> bool:
        mid = normalize_id(model)
        if not mid:
            return False
        with self._lock:
            target = normalize_region(region)
            regions = [target] if target else list(REGIONS)
            for reg in regions:
                if mid in self._state.disabled.get(reg, []):
                    return False
            return True

    def set_enabled(self, model: Optional[str], enabled: bool, region: Optional[str] = None) -> bool:
        """开 / 关某模型。region 为空表示对两版同时生效。返回是否发生变化。"""
        mid = normalize_id(model)
        if not mid:
            return False
        with self._lock:
            target = normalize_region(region)
            regions = [target] if target else list(REGIONS)
            changed = False
            for reg in regions:
                bucket = self._state.disabled.setdefault(reg, [])
                if enabled and mid in bucket:
                    bucket.remove(mid)
                    changed = True
                elif not enabled and mid not in bucket:
                    bucket.append(mid)
                    bucket.sort()
                    changed = True
            if changed:
                self._dirty = True
                self.save()
            return changed

    def toggle(self, model: Optional[str], region: Optional[str] = None) -> bool:
        mid = normalize_id(model)
        with self._lock:
            new_state = not self.is_enabled(mid, region)
        self.set_enabled(mid, new_state, region)
        return new_state

    def bulk_set(self, models: Iterable[str], enabled: bool, region: Optional[str] = None) -> int:
        count = 0
        for mid in models:
            if self.set_enabled(mid, enabled, region):
                count += 1
        return count

    def enabled_ids(self, region: Optional[str] = None) -> List[str]:
        return [s.id for s in self.catalog(region, enabled_only=True)]

    def disabled_ids(self, region: Optional[str] = None) -> List[str]:
        return [s.id for s in self.catalog(region, enabled_only=False) if not self.is_enabled(s.id, region)]

    # ---- 上游发现结果 ----
    def merge_discovered(self, entries: Any, region: Optional[str] = None, save: bool = True) -> int:
        """把上游模型列表合并进目录。

        entries 支持两种形状：
          - [{"id": "...", "context_window": 200000, ...}, ...]
          - {"model-id": {...}, ...}
        只写入「确实取到」的字段，取不到的保持 None。
        """
        rows: List[Tuple[str, Dict[str, Any]]] = []
        if isinstance(entries, dict):
            rows = [(normalize_id(k), v) for k, v in entries.items() if isinstance(v, dict)]
        elif isinstance(entries, list):
            for item in entries:
                if not isinstance(item, dict):
                    continue
                mid = normalize_id(item.get("id") or item.get("model") or item.get("name"))
                if mid:
                    rows.append((mid, item))
        count = 0
        with self._lock:
            for mid, item in rows:
                if not mid:
                    continue
                block: Dict[str, Any] = {}
                if item.get("display_name") or item.get("displayName") or item.get("title"):
                    block["name"] = str(item.get("display_name") or item.get("displayName") or item.get("title"))
                for key in ("context_window", "contextWindow", "context_length", "contextLength"):
                    value = item.get(key)
                    if isinstance(value, (int, float)) and value > 0:
                        block["context_window"] = int(value)
                        break
                for key in ("max_tokens", "maxTokens", "max_output_tokens", "maxOutputTokens"):
                    value = item.get(key)
                    if isinstance(value, (int, float)) and value > 0:
                        block["max_tokens"] = int(value)
                        break
                modalities = item.get("input") or item.get("input_modalities") or item.get("inputModalities")
                if isinstance(modalities, list):
                    block["supports_image"] = any(
                        str(m).strip().lower() in ("image", "vision", "img") for m in modalities
                    )
                efforts = item.get("reasoning_efforts") or item.get("reasoningEfforts")
                if isinstance(efforts, dict):
                    block["reasoning_efforts"] = sorted(str(k) for k in efforts.keys())
                elif isinstance(efforts, list) and efforts:
                    block["reasoning_efforts"] = [str(e) for e in efforts]
                rate = item.get("rate")
                if isinstance(rate, (int, float)):
                    block["rate"] = float(rate)
                if region and isinstance(region, str):
                    block.setdefault("regions", [normalize_region(region) or "cn"])
                if block:
                    self._state.discovered.setdefault(mid, {}).update(block)
                    count += 1
            if count and save:
                self._dirty = True
                self.save()
        return count

    # ---- 覆盖（人工修正） ----
    def set_override(self, model: Optional[str], **fields: Any) -> bool:
        mid = normalize_id(model)
        if not mid:
            return False
        allowed = {"name", "context_window", "max_tokens", "supports_image",
                   "reasoning_efforts", "rate", "regions", "tier", "note"}
        clean = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not clean:
            return False
        with self._lock:
            self._state.overrides.setdefault(mid, {}).update(clean)
            self._dirty = True
            self.save()
        return True

    def clear_overrides(self, model: Optional[str] = None) -> None:
        with self._lock:
            if model is None:
                self._state.overrides.clear()
                self._state.discovered.clear()
            else:
                mid = normalize_id(model)
                self._state.overrides.pop(mid, None)
                self._state.discovered.pop(mid, None)
            self._dirty = True
            self.save()

    # ---- 促销 ----
    def set_promotions(self, raw: Any, save: bool = True) -> int:
        """整体替换促销表（服务端 modelPromotions 是权威来源）。"""
        items: List[Dict[str, Any]] = []
        source = raw.get("promotions") if isinstance(raw, dict) else raw
        if isinstance(source, list):
            for item in source:
                promo = _coerce_promotion(item)
                if promo is not None:
                    items.append(asdict(promo))
        with self._lock:
            self._state.promotions = items
            if save:
                self._dirty = True
                self.save()
        return len(items)

    def promotions_for(self, model: Optional[str] = None, active_only: bool = True,
                       ts: Optional[float] = None) -> List[Promotion]:
        mid = normalize_id(model)
        out: List[Promotion] = []
        with self._lock:
            raw = list(self._state.promotions)
        for item in raw:
            promo = _coerce_promotion(item)
            if promo is None:
                continue
            if mid and promo.model != mid:
                continue
            if active_only and not promo.active_at(ts):
                continue
            out.append(promo)
        return out

    def prune_promotions(self, ts: Optional[float] = None) -> int:
        """清掉已过期且带明确结束时间的促销。返回清掉的条数。"""
        now = now_ts() if ts is None else float(ts)
        with self._lock:
            kept = []
            for item in self._state.promotions:
                promo = _coerce_promotion(item)
                if promo is None:
                    continue
                if promo.end_ts and now > promo.end_ts:
                    continue
                kept.append(asdict(promo))
            removed = len(self._state.promotions) - len(kept)
            if removed:
                self._state.promotions = kept
                self._dirty = True
                self.save()
            return removed

    # ---- 视图 ----
    def describe(self, model: Optional[str], region: Optional[str] = None,
                 ts: Optional[float] = None) -> Dict[str, Any]:
        """给面板 / /v1/models 用的完整描述，含字段来源与徽章。"""
        mid = normalize_id(model)
        spec = self.spec(mid, region)
        if spec is None:
            return {"id": mid, "known": False, "enabled": False, "regions": []}
        with self._lock:
            discovered = self._state.discovered.get(mid) or {}
            overrides = self._state.overrides.get(mid) or {}
        source = {}
        for key in ("context_window", "max_tokens", "supports_image", "rate"):
            if key in overrides:
                source[key] = "override"
            elif key in discovered:
                source[key] = SOURCE_UPSTREAM
            else:
                source[key] = SOURCE_BUILTIN
        promos = self.promotions_for(mid, active_only=False, ts=ts)
        return {
            "id": spec.id,
            "name": spec.name or spec.id,
            "known": True,
            "family": spec.family,
            "tier": spec.tier,
            "regions": list(spec.regions),
            "region_labels": [REGION_LABELS.get(r, r) for r in spec.regions],
            "context_window": spec.context_window,
            "max_tokens": spec.max_tokens,
            "supports_image": spec.supports_image,
            "reasoning_efforts": list(spec.reasoning_efforts),
            "rate": spec.rate,
            "note": spec.note,
            "twins": dict(spec.twins),
            "enabled": self.is_enabled(mid, region),
            "sources": source,
            "badges": [p.to_dict(ts) for p in promos if p.active_at(ts)],
            "promotions": [p.to_dict(ts) for p in promos],
        }

    def snapshot(self, region: Optional[str] = None) -> Dict[str, Any]:
        ts = now_ts()
        specs = self.catalog(region, enabled_only=False)
        items = [self.describe(s.id, region, ts) for s in specs]
        return {
            "region": normalize_region(region) or REGION_MERGED,
            "regions": [{"key": REGION_MERGED, "label": "合并"}] +
                       [{"key": r, "label": REGION_LABELS[r]} for r in REGIONS],
            "items": items,
            "enabled": [i["id"] for i in items if i["enabled"]],
            "disabled": [i["id"] for i in items if not i["enabled"]],
            "updated": self._state.updated,
            "stats": self.stats(),
        }

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": CATALOG_VERSION,
                "path": str(self.path),
                "builtin": len(_BUILTIN_BY_ID),
                "discovered": len(self._state.discovered),
                "overrides": len(self._state.overrides),
                "promotions": len(self._state.promotions),
                "disabled": {r: list(self._state.disabled.get(r, [])) for r in REGIONS},
                "updated": self._state.updated,
            }


# ===== 请求解析：把用户给的模型名收敛成一个可用的 id =====

def resolve_requested(requested: Optional[str], region: Optional[str] = None) -> Dict[str, Any]:
    """决定这次请求实际用哪个模型。

    返回 {model, requested, replaced, reason, spec}
      replaced=True 时 model 是兜底模型，reason 说明原因：
        empty / unknown / disabled / wrong_region / unmatched_region
    注意：目录里出现的模型即使区域不匹配也不会被静默替换成兜底，
    因为调用方可能是跨版调用；只有未知或明确被隐藏才替换。
    """
    mid = normalize_id(requested)
    fallback = default_model(region)
    if not mid:
        return {"model": fallback, "requested": "", "replaced": True, "reason": "empty",
                "spec": None}

    exact = get_spec(mid)                       # 不限区域，先看目录里有没有这个 id
    if exact is None:
        return {"model": fallback, "requested": mid, "replaced": True, "reason": "unknown",
                "spec": None}

    if not get_prefs().is_enabled(mid, region):
        return {"model": fallback, "requested": mid, "replaced": True, "reason": "disabled",
                "spec": exact}

    target = normalize_region(region)
    if target and target not in exact.regions:
        # 账号池只有一个区域时，跨区模型不可用
        return {"model": fallback, "requested": mid, "replaced": True, "reason": "wrong_region",
                "spec": exact}

    return {"model": mid, "requested": mid, "replaced": False, "reason": "", "spec": exact}


# ===== 模块级单例 =====

_prefs: Optional[ModelPrefs] = None
_prefs_lock = threading.RLock()
_logger: Any = None


def get_prefs() -> ModelPrefs:
    global _prefs
    with _prefs_lock:
        if _prefs is None:
            _prefs = ModelPrefs(logger=_logger)
        return _prefs


def configure(path: Optional[str] = None, logger: Any = None) -> ModelPrefs:
    """按配置初始化。path 为空表示沿用默认的 data/models.json。"""
    global _prefs, _logger
    with _prefs_lock:
        _logger = logger if logger is not None else _logger
        target = Path(path) if path else MODELS_FILE
        _prefs = ModelPrefs(path=target, logger=_logger)
        return _prefs


def flush() -> None:
    with _prefs_lock:
        if _prefs is not None:
            _prefs.flush()


if __name__ == "__main__":   # pragma: no cover - 手工排查用
    prefs = get_prefs()
    snap = prefs.snapshot()
    print(json.dumps(snap["stats"], ensure_ascii=False, indent=2))
    for item in snap["items"]:
        flags = []
        if item["supports_image"]:
            flags.append("收图")
        if item["reasoning_efforts"]:
            flags.append("推理" + "/".join(item["reasoning_efforts"]))
        if item["rate"] is not None:
            flags.append("x%s" % item["rate"])
        print("%-28s %-22s %s" % (item["id"], ",".join(item["region_labels"]), " ".join(flags)))
