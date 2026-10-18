# 🏛️ WorkBuddy 统一管理器 · 架构与原理

> 本文档面向**首次访问本仓库的 GitHub 用户**，说明：
> 1. 这个项目在做什么；
> 2. 由哪些组件构成；
> 3. 一个 `/v1/chat/completions` 请求如何走完整个系统；
> 4. 账号池、冷却熔断、任务引擎等关键机制的原理；
> 5. 与上游 Go 版的差异（哪些没有移植）。
>
> 想要快速上手，请看 [README.md](./README.md)。想要看懂实现细节，继续读本文。

---

## 目录

1. [项目定位](#项目定位)
2. [系统拓扑](#系统拓扑)
3. [目录结构](#目录结构)
4. [组件职责](#组件职责)
5. [请求生命周期](#请求生命周期)
6. [关键机制原理](#关键机制原理)
7. [桌面指纹与出站请求](#桌面指纹与出站请求)
8. [与上游 Go 版的差异](#与上游-go-版的差异)
9. [安全与合规](#安全与合规)

---

## 项目定位

腾讯 CodeBuddy（`copilot.tencent.com`）是官方闭源服务，**不提供任何 OpenAI 兼容的开放 API**。个人用户手里的多个 CodeBuddy 账号因此只能「在网页里用」，无法被本地工具（Cursor、Cline、DeepSeek Harness、自建 Agent、脚本……）以标准化方式调用。

本项目把这一层缺失的 API 边界补齐：

- **对上**：通过 OAuth 设备授权登录，把每个 CodeBuddy 账号变成一个可持续使用的上游身份；
- **对下**：暴露一个 OpenAI 兼容的 `/v1/chat/completions` 端点，本地工具零改造接入；
- **中间**：账号池调度、会话粘性、冷却与熔断、成长任务 / 签到（面板点一下跑一次）……全部本地运行，凭证与数据**不出本机**。

一句话：**本地运行的、开源的、多账号的、OpenAI 兼容反向代理网关 + 自研面板**。

---

## 系统拓扑

```
                    ┌──────────────────────────────────────────┐
   浏览器 / 手机    │  static/ 统一面板（11 视图，PWA 可加桌面） │
   ───────────────▶│  http://127.0.0.1:7863/panel/            │
                    └────────────────┬─────────────────────────┘
                                     │ /panel/api/* · /api/*
    OpenAI SDK /    POST /v1/        ▼
    DSH / Agent ──────────────▶ ┌───────────────────────────────────┐
                                │ wb_gateway.py   :7863             │
                                │  GatewayHandler                   │
                                │  ├─ _check_auth（Bearer）          │
                                │  ├─ AccountPool + 冷却 / 熔断       │
                                │  ├─ chat: SSE 透传 / 非流式聚合      │
                                │  ├─ /panel/api/* 面板后端           │
                                │  └─ /panel/static/* 静态资源托管     │
                                └───┬─────────────┬────────────┬────┘
                                    │             │            │
                      ┌─────────────▼──┐   ┌──────▼──────┐  ┌──▼─────────────┐
                      │ wb_tasks.py    │   │ wb_auth.py  │  │ wb_live_credits │
                      │ 任务 / 签到     │   │ OAuth 登录   │  │ 实时余额查询     │
                      │ wb_checkin.py  │   │ token 刷新   │  │ wb_usage.py     │
                      └───────┬────────┘   └──────┬──────┘  └──┬─────────────┘
                              │                   │            │
                              ▼                   ▼            ▼
                    ┌───────────────────────────────────────────────────┐
                    │ auths/*.json（明文 accessToken / refreshToken）     │
                    │ data/*.json（state / usage / models / checkin）     │
                    │ wb_data/*（credits_history / tasks_log.jsonl）      │
                    └───────────────────────────────────────────────────┘

       上游：copilot.tencent.com · codebuddy.cn · workbuddy.cn（唯一外部依赖）

                      ┌──────────────────────────────────────────┐
   main.py  ─────────▶│ 起网关子进程 + 线程起老控制台 :8789        │
                      │ 老控制台根路径 302 → /panel/，/api/* 保留  │
                      └──────────────────────────────────────────┘
```

**关键点**：整个系统**只有 Python 标准库 + Playwright**（后者仅积分采集器与浏览器实测用）。没有前端打包、没有 Redis、没有数据库、也没有后台定时调度器；凭证（`auths/*.json`）与状态（`data/*.json`、`wb_data/*`）全部落本地磁盘。

---

## 目录结构

```
WorkBuddy_统一管理器/
├── main.py                        # 启动器：建目录 → 起网关子进程 → 起老控制台 → 开浏览器
├── wb_gateway.py                  # 核心：OpenAI 兼容网关 + 账号池 + 冷却/熔断 + 面板后端
├── wb_tasks.py                    # 任务引擎：16 条成长任务 + 签到 + 保活（无排程，点一次跑一次）
├── wb_checkin.py                  # 签到：错峰时点 / 补签 / 强制重签 / 演练 + 收益账本
├── wb_auth.py                     # OAuth 设备授权登录（state → token → account）+ token 刷新
├── wb_live_credits.py             # 实时积分查询（直连上游 billing meter API）
├── wb_credits_fetcher.py          # Playwright 扫码采集积分 / 能量 / Buddy → HTML 报告
├── wb_playwright_transport_patch.py  # Windows asyncio 子进程重叠管道补丁
├── wb_webui.py                    # 老控制台 :8789（只留 /api/*，根路径 302 跳面板，/legacy 兜底）
├── wb_ops.py                      # 采集进程控制 + 四项自检（面板「实时日志」「诊断」后端）
├── wb_models.py                   # 模型目录：内置表 / 区域 / 图片能力 / 推理档位 / 促销 / 覆盖
├── wb_pool_policy.py              # 池策略：priority / round-robin / balanced / sticky
├── wb_usage.py                    # 用量统计：按小时分桶 + 模型 / 客户端 / 区域 / 账号维度
├── wb_snapshot.py                 # 桌面快照发现与导入（login_state_* / identity_*）
├── static/                        # 统一面板本体（8 个文件：index.html / app.css / app.js / manifest / sw.js / 3 图标）
├── dsh/                           # DSH 接入配置片段（cordis.workbuddy.patch.yml）
├── devtools/                      # 开发期验证页（不对外暴露，可删）
├── selfcheck.py                   # 自检：py_compile 全部模块 + 页面 JS 语法 + static/ 齐全性
├── browser_check.py               # 真实浏览器实测：老控制台按钮 + 统一面板逐页
├── config.py                      # 账号清单示例（网关不读这个文件）
├── config.example.json            # 网关配置模板（只含真正生效的字段，可 git 提交）
├── config.json                    # 用户真实配置（.gitignore 排除）
├── README.md / ARCHITECTURE.md    # 快速上手 / ← 本文档
├── DSH接入说明.md                  # 已装进 DSH 的路由说明（怎么验证 / 怎么回滚）
├── LICENSE / .gitignore
├── 一键启动.bat                    # Windows 双击启动
├── auths/                         # [被排除] 明文 accessToken / refreshToken / uid
├── data/                          # [被排除] state.json / usage.json / models.json / checkin_state.json
└── wb_data/                       # [被排除] credits_history* / tasks_log.jsonl / collect_log.txt
```

> 安卓外壳工程在上一级目录 `android-app/`（WebView + `build_apk.bat`），不在本目录内。

---

## 组件职责

| 模块 | 行数 | 职责 |
|---|---|---|
| `main.py` | 185 | 单一入口：以子进程启动 `wb_gateway.py`（网关 7863，**面板也由它托管**）；后台线程用 `ThreadingHTTPServer` 起老控制台（8789，只留 `/api/*`，根路径 302 跳面板）；1.5 秒后打开浏览器 |
| `wb_gateway.py` | 2712 | **心脏**。OpenAI 兼容网关 + `Account` / `AccountPool` + 选号 + 冷却/熔断 + SSE 透传/聚合 + 面板 HTTP API + 静态资源托管 |
| `wb_webui.py` | 1267 | **老控制台**（`:8789`）：只保留 `/api/*` 数据接口与 `/legacy` 旧界面 |
| `static/` | 8 个文件 | **统一面板本体**：`index.html` + `app.css` + `app.js` + `manifest.webmanifest` + `sw.js` + 3 个图标 |
| `wb_ops.py` | 258 | 采集进程控制（启停 / 日志 / 状态）与四项自检（Python / 网关 / 依赖 / 文件） |
| `wb_models.py` | 855 | 模型目录：内置模型表、区域、图片能力、推理档位、促销、上游发现与手工覆盖 |
| `wb_pool_policy.py` | 526 | 池策略：候选筛选、各策略排序 / 加权、选号理由与「下一个轮到谁」预览 |
| `wb_usage.py` | 833 | 用量统计：按小时分桶、模型 / 客户端 / 区域 / 账号维度、User-Agent 排行，落 `data/usage.json` |
| `wb_snapshot.py` | 499 | 桌面快照发现与导入（`login_state_*` / `identity_*`） |
| `wb_checkin.py` | 602 | 签到：错峰时点、补签、强制重签、演练，以及收益账本与最近记录 |
| `wb_tasks.py` | 1418 | 任务引擎：16 条成长任务 + `keepalive()`；全部幂等，**没有任何排程器** |
| `wb_credits_fetcher.py` | 1142 | Playwright 扫码登录 → 采集积分 / 能量 / Buddy → 生成 HTML 报告；数据仅存本地 |
| `wb_auth.py` | 261 | OAuth 设备授权三步流程（`auth/state` → `auth/token` → `login/account`），落盘 `auths/workbuddy-<uid>.json`；uid 白名单校验防路径穿越 |
| `wb_live_credits.py` | 213 | 绕过本地快照，直连上游 billing 接口查询实时余额（30 秒内存缓存） |
| `wb_playwright_transport_patch.py` | 202 | Windows 下修复 `asyncio.create_subprocess_exec` 无法创建重叠管道的问题（须在 import playwright 之前调用） |
| `selfcheck.py` | 332 | `py_compile` 全部模块 + 页面 / `static/*.js` 语法检查 + `static/` 交付文件齐全性 |
| `browser_check.py` | 183 | 用真实浏览器打开旧控制台点按钮，再逐页走统一面板的 11 个视图 |
| `config.py` | 51 | 账号清单与默认值示例——**网关不读它**（真正生效的是 `config.json`） |
| `config.example.json` | 模板 | 只含真正会被读取的配置键（`DEFAULT_CONFIG` 的裁剪版） |

---

## 请求生命周期

一个典型的 `/v1/chat/completions` 请求从进入到返回：

```
① 客户端发送请求
   POST http://127.0.0.1:7863/v1/chat/completions
   Authorization: Bearer <api_key>
   Body: { "model": "deepseek-v4-flash", "messages": [...], "stream": true }
        │
        ▼
② do_POST 分发（wb_gateway.py:1600-1602）
   _check_auth()（:1365）比对 Authorization 与 config.json 的 api_key，不通过 → 401 authentication_error
        │
        ▼
③ _handle_chat_completions（:1716）
   按 Content-Length 读 body（:1719-1723，**没有大小上限**）→ json.loads（失败当作空对象）
   session_id = body["user"]，缺省 uuid4（:1730）
        │
        ▼
④ 选号 pool.pick(session_id)（:1738）→ wb_pool_policy.select()
   候选筛选：账号 active · 未熔断 / 未冷却 · 区域匹配 · 余额 ≥ credit_floor
   顺序：会话粘性 → current_account_id →（prefer_expiring 软排序）→ 所选策略
   选不出来 → 503 No available accounts: <账号(原因)>（:1740-1753）
        │
        ▼
⑤ 构造上游请求
   chat_upstream_url()（:1061-1076）→ 默认 https://copilot.tencent.com/v2/chat/completions
   resolve_model()（:637）：未知 / 跨区 / 被隐藏的模型换成可用模型，并 log「模型 X -> Y」
   强制 stream=true（:1764）—— 上游只接受流式，非流式会 400 code=11101
   timeout = upstream.timeout_seconds（默认 120，:1788）
        │
        ▼
⑥ 向上游发起请求
   ├── 客户端 stream=true ──▶ 原样透传：每 1KiB 一段 wfile.write + flush（:1800-1810）
   │                          不重建帧、不剥字段、不补 [DONE]；StreamUsageTap 顺手抓 usage
   └── 客户端 stream=false ──▶ aggregate_sse_completion()（:1084）把 SSE 聚合成
                              单个 OpenAI chat.completion（content / tool_calls / usage）
        │
        ▼
⑦ 收尾（:1841-1877）
   成功 → pool.record_success()（request_count++ / success_count++ / last_used=now / fail_count=0）
   HTTPError 429 → record_failure(is_rate_limit=True) → 该账号冷却 cooldown.soft_rate（默认 600s）
   其它失败 → fail_count++；连续 ≥ pool.breaker_threshold（默认 3）→ 熔断 pool.breaker_cooldown（默认 30m）
   客户端 4xx/5xx：错误体固定为 {"error": {"message": "Upstream error: <code>", ...}}
        │
        ▼
⑧ 用量与日志
   用量写入 data/usage.json（按小时分桶，flush 5 秒 / 保留 90 天）
   访问日志一行：log_message（:1363）→ log.info("<客户端地址> - <请求行>")
```

**为什么强制上游 `stream=true`**：CodeBuddy 上游实测对非流式请求直接返回业务错误 `11101`。网关在客户端侧「拆流式再聚合」比「让上游开非流式」更稳定——聚合逻辑就在 `aggregate_sse_completion()`。

---

## 关键机制原理

### 1. 账号池调度（`AccountPool.pick` → `wb_pool_policy.select`）

`Account` 是 dataclass，每个账号维护：

```python
id, name, token, phone, domain, credits, energy, active,
last_used,            # 最近一次被选中的时间戳
request_count,        # 累计请求数
success_count,        # 成功数
fail_count,           # 失败数
breaker_until,        # 熔断到期时间（epoch 秒）
cooldown_until,       # 限流冷却到期时间（epoch 秒）
balance_checked_at    # 上次刷新余额的时间戳
```

候选筛选（`wb_pool_policy.scope`）：区域不匹配、余额低于 `credit_floor`、`active=False`、熔断中、冷却中的账号直接出局。剩下的按四策略之一排序：

| 策略 | 键 | 说明 |
|---|---|---|
| `priority`（默认） | 最近使用时间倒序 + 余额 | 先用完一个，用尽或被熔断才换 |
| `round-robin` | 按 id 顺序 + 游标 | 负载平均摊开，游标落 `data/state.json` |
| `balanced` | 闲置时长加权随机 | 闲置越久权重越高（`idle_weight_per_hour` / `idle_weight_max`） |
| `sticky` | 会话绑定优先 | 一个对话固定一个账号，保住上游 prompt 缓存 |

顺序上还有两条前置规则：**会话粘性命中**（`sticky-hit`）优先于一切；没有会话时才考虑 `current_account_id`（面板「切换」设的当前账号）。`prefer_expiring=True` 会把快过期账号提前。选出的账号带上一个 `reason`（`sticky-hit` / `current` / `priority` / `balanced` / `sticky-new` / `fallback`），面板「账号池」页直接展示。

选不出账号（全熔断 / 冷却 / 余额不足 / 池为空）→ 503 `No available accounts`。

### 2. 冷却、熔断与失败计数（`record_failure` / `record_success`）

| 上游响应 | 网关动作 |
|---|---|
| HTTP 429 | 该账号 `cooldown_until = now + cooldown.soft_rate`（默认 600s），日志「账号 X 被限流，冷却 N 分钟」 |
| 其它失败（4xx / 5xx / 超时） | `fail_count++`；达到 `pool.breaker_threshold`（默认 3）→ `breaker_until = now + pool.breaker_cooldown`（默认 30m），日志「连续失败 N 次，熔断 M 分钟」 |
| 任意一次成功 | `fail_count` 清零，`last_used` 更新 |

`_is_available(acc)` = `acc.active AND now >= breaker_until AND now >= cooldown_until`。

**本分支没有**：`402`（额度耗尽）/ `404` 的分档处置、`code=6004` 模型级限流解析、指数退避与封顶、硬冷却到次日、Redis 状态镜像、在途并发上限（`pool.max_in_flight` 没有读取者）。上游错误在这里只有「记一次失败 + 上面两条冷却规则」这一种处理。

### 3. 会话粘性（`sticky_sessions`）

多轮对话如果跳号，上下文断裂。所以 `session_id` 会绑定账号：

- 会话键 = 请求体里的 `user` 字段；客户端没给就每次生成 uuid4（等价于不粘性）；
- 命中且账号可用 → 直接用（`sticky-hit`）并续期；命中但账号不可用 → 解绑后重新选；
- **策略路径不设 TTL**（`select` 传的 `sticky_ttl` 默认 0）；只有 `wb_pool_policy` 缺失时的旧逻辑会写 1800 秒；
- 绑定关系落 `data/state.json`（`pool.save`），重启不丢；把账号「移除」时会清掉指向它的绑定。

### 4. Token 刷新（`refresh_auth_tokens`）

上游 accessToken 是 JWT，有效期有限。刷新入口只有两处：

1. **任务执行前自动**：`handle_task_action()` 在处理 `scan` / `run` / `keepalive` 前调用 `refresh_auth_tokens(force=False)`（wb_gateway.py:796-797），只刷新即将过期的；
2. **手动**：面板「刷新 Token」按钮 / `GET /api/refresh-tokens`（`force=True`，:1560）。

刷新用本地保存的 refreshToken 向 codebuddy 域换新 token，写回 `auths/workbuddy-<uid>.json` 并更新内存里的账号。**聊天链路本身不检查 token 过期**——账号能不能用由上游响应决定（401/403 会计入失败计数）。

### 5. SSE 帧透传与非流式聚合

- **流式客户端**：网关按 1KiB 原样转发上游字节流，**不重建帧、不剥字段、不补 `[DONE]`**；`StreamUsageTap` 从经过的帧里抓 `usage` 记进用量库；
- **非流式客户端**：`aggregate_sse_completion(resp, model)`（:1084）逐帧解析 `delta.content` / `delta.reasoning_content`，合并 `tool_calls`（按 index），取 `finish_reason` 与 `usage`，组装成一个 OpenAI JSON 响应后一次性返回。

### 6. 签到 / 任务 / 保活：没有调度器

`config.json` 里的 `schedule.*`、`config.py` 里的 `AUTOTASKS` 都**没有执行者**；`main.py` 只负责起进程。所有动作都是**面板点一下跑一次**（或手动跑脚本）：

| 动作 | 面板位置 | 行为 |
|---|---|---|
| 签到 | 「签到日志」页 | 错峰时点 / 补签 / 强制重签 / 演练（`dry_run` 不发请求）；每账号签到状态 + 收益账本 |
| 成长任务 | 「任务中心」页 | 扫描进度 / 一键完成 / 演练；结果落 `wb_data/tasks_log.jsonl` |
| 账号保活 | 「任务中心」页 | `keepalive()` 对每个账号强刷一次 token |
| 积分采集 | 「实时日志」页 | 起停 `wb_credits_fetcher.py`（Playwright），原地跟读日志 |
| 实时刷新 | 顶栏「实时 Ns」 | **只刷新页面数据**，不向上游发起任何动作 |

任务引擎全部**幂等**：已完成的任务自动跳过，重复执行不会重复领奖（上游对已完成任务返回 200 但不再加积分）。

### 7. 成长任务一键完成

任务目录收录 **17 条**：**16 条走公开 API 就能完成**（`first_buddy` / `create_canvas` / `chat_5` / `Model_chat_GLM5.2` / `RichMeow_Chat` / `Buddy_App` / `Buddy_App_QQ` / `automation_1` / `Library_read` / `template_5` / `playbook_prompt` / `expert_5` / `Expert_team_use_3` / `Hp_Appearance` / `Expert_lighthouse` / `skill_1`），`Expert_Philanthropy` 需要真实捐款，标记为「需手工」。

任务引擎对每个任务做：

1. **扫描**：`GET /v2/activity/growth/tasks` 拿当前完成状态；
2. **accept**：未开始先 `POST …/tasks/accept`；
3. **report**：调对应业务接口产生「完成证据」（部分任务还需要桌面指纹事件数组）；
4. **claim**：等异步计分落定后 `POST /activity/growth/tasks/<code>/claim` 领积分；
5. **落盘**：写入 `wb_data/tasks_log.jsonl`，面板「签到日志」页可回看。

### 8. 模型目录与兜底

- 内置目录 16 个模型（`wb_models.py`），字段含区域、图片能力、推理档位、倍率、促销窗口；
- `data/models.json` 存「上游发现」与「手工覆盖」；文件不存在就用内置表；
- `GET /v1/models` 只在目录里**有证据**时才输出 `context_window` / `max_tokens`（宁可缺字段也不给猜的数字），并附 `workbuddy_meta`（区域 / 图片 / 推理档位 / twins / 促销）；
- 请求的模型不可用时由 `resolve_model()` 换成兜底模型，替换原因一路传到日志、响应头与面板。

---

## 桌面指纹与出站请求

CodeBuddy 上游有一个隐蔽的防御机制：**成长任务的请求体里如果缺少桌面客户端指纹字段，服务端会返回 HTTP 200 但静默丢弃**（不报错，但积分不涨）。这是从上游 Go 版 `internal/upstream/desktop.go` 逆向对齐的字段集合。

`wb_tasks.py` 中定义了一组桌面指纹常量，任务上报时必须完整带上：

```python
DESKTOP_COMMIT          = "5f9692923c93033111c51ad7b003eb80204a9b75"
DESKTOP_RELEASE_DATE    = 1789036585355
DESKTOP_IDE_VERSION     = "5.5.6"
DESKTOP_OS              = "win32"
DESKTOP_OS_VERSION      = "10.0.26220"
DESKTOP_CPU_CORES       = 20
DESKTOP_MEMORY_SIZE     = 24
DESKTOP_EXT_NAME        = "workbuddy-desktop"
DESKTOP_PRODUCT         = "SaaS"
```

相关的出站头（同样只在任务链路）：`X-IDE-Name` / `X-IDE-Type` / `X-IDE-Version`（wb_tasks.py:678-680）与 `X-Device-Token`（凭据文件里有 `device_token` 时带上，wb_tasks.py:187-188）。UA 是代码内常量（`wb_tasks.py:40-43`），不是配置项。

**注意**：聊天链路（`/v1/chat/completions`）**不做任何请求体改写**，也不做指纹清洗、不做提示词替换——客户端 `messages` 与 `model` 原样转发（唯一改动是强制 `stream=true`）。上游 Go 版那套「系统提示词体系 + 客户端指纹黑名单清洗」在本分支不存在。

---

## 与上游 Go 版的差异

本项目是 [Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) 的 fork 分支，上游现已删除。核心改造点：

| 维度 | 上游 Go 版 | 本项目 |
|---|---|---|
| 语言 | Go 1.22.5 | Python 3.10+ |
| 部署形态 | 单二进制 / Docker | 双击 `一键启动.bat` / `python main.py` |
| 任务引擎 | 混在 `task_common.py` | **从零重写**，扁平 TASKS 目录，全部幂等 |
| 可视化 | 嵌入式 Go 面板 | 自研静态面板 `static/`（11 视图，PWA） |
| 自动排程 | 内置调度器（签到 / 旅行 / 活跃 / 保活） | **没有**：全部靠面板点按钮触发 |
| 请求改写 | 提示词体系 + 指纹脱敏 + UA 可配 | **没有**：只强制 `stream=true` |
| 错误分类 | 429 / 402 / 404 / `6004` / `11128` 分档 + 退避封顶 | **没有**：429 → 冷却；其它 → 失败计数 → 熔断 |
| 状态存储 | 本地 JSON + 可选 Redis 镜像 | 本地 JSON（`data/state.json`），无 Redis |
| 自检 | 无 | `selfcheck.py` + `browser_check.py` |
| 依赖 | Go 标准库 | Python 标准库 + Playwright（仅采集 / 实测用） |

**保留的设计思路**：账号池多策略调度、会话粘性、OpenAI 兼容协议、模型兜底、任务幂等。

**没有移植的功能**：定时调度器、系统提示词体系、请求体净化 / 指纹脱敏、错误码精细分级、企业号判定、Redis 镜像、在途并发上限。写在这里是为了避免「读上游文档以为本分支也有」。

---

## 安全与合规

### 安全边界

1. **凭证安全**：`auths/*.json` 保存**明文** accessToken / refreshToken / uid——**这就是为什么 `.gitignore` 强制排除该目录**。任何「收费版」「卡密版」如果让你上传凭证到别人的服务器，都是把账号交给陌生人。
2. **数据边界**：网关、任务引擎、采集器**全部本地运行**，不经过任何第三方服务器；唯一外部依赖是上游腾讯域。
3. **网络边界**：网关默认监听 `127.0.0.1:7863`（仅本机），**面板由它托管**；8789 的老控制台只剩 `/api/*` 数据接口。若需要局域网访问面板（手机 PWA / 安卓外壳），把 `config.json` 的 `listen` 改成 `0.0.0.0:7863` 并放行防火墙，同时**务必保留非空 `api_key`**。
4. **来源可查**：用量库按客户端 User-Agent 归类（面板「Token 统计」页有排行），网关日志每个请求打一行「客户端地址 - 请求行」；**没有**按请求落盘的归档文件。
5. **脱敏展示**：手机号在面板上掩码显示（如 `138****8831`）；uid / phone 只在面板与内部使用，不透给上游。

### 合规立场

本项目 fork 自 [Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)（上游已删除），基于 MIT 协议开源，并保留上游作者的以下立场（详见 [README 使用声明](./README.md#使用声明必读)）：

- **仅限本人授权账号、本机 / 私有环境使用**；
- **禁止批量注册小号**、禁止对外提供付费 API / 共享池 / 代充；
- **禁止二次打包、加壳、捆绑卡密售卖**，或以「公益服」「低价中转」变相收费分发；
- **上游 CodeBuddy 服务条款**本身禁止批量注册与商业转售——滥用行为正在毁掉所有正常使用者的环境。

本项目不提供、也不支持任何第三方分发版本。若发现上述滥用行为，作者保留随时停止维护、关闭或删除仓库的权利。

### 免责

- 本项目为**非官方**工具，与腾讯、CodeBuddy、WorkBuddy 无关联；
- 使用本项目的一切后果由使用者自行承担；
- 项目按「原样」提供，无任何明示或暗示的担保；
- 使用前请阅读 [MIT License](./LICENSE) 与 [README 使用声明](./README.md#使用声明必读)。

---

## 想深入阅读源码？

推荐按这个顺序：

1. `main.py`（185 行）—— 看清楚系统怎么起来的（网关 + 老控制台 + 打开浏览器）；
2. `wb_gateway.py` L1–L340 —— `Account` / `AccountPool` 数据模型、选号、冷却与熔断；
3. `wb_gateway.py` L1716–L1877 —— `_handle_chat_completions`，请求生命周期的核心；
4. `wb_pool_policy.py` —— 四种分配策略与候选筛选（想看「为什么选了这个号」就读它）；
5. `wb_auth.py` —— OAuth 三步登录，理解凭证怎么落盘的；
6. `wb_tasks.py` 头部常量区 —— 桌面指纹与任务定义表；`keepalive()` 在 L1237；
7. `wb_checkin.py` —— 签到、错峰时点与收益账本；
8. `static/app.js` —— 面板全部视图与交互（一个文件，函数名即页面名，如 `pageCheckin`）；
9. `wb_credits_fetcher.py` 头部 —— Playwright 采集器与报告生成。

有任何问题，欢迎提 Issue；如果理解了代码，欢迎提 PR。
