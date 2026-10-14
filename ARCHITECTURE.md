# 🏛️ WorkBuddy 统一管理器 · 架构与原理

> 本文档面向**首次访问本仓库的 GitHub 用户**，说明：
> 1. 这个项目在做什么；
> 2. 由哪些组件构成；
> 3. 一个 `/v1/chat/completions` 请求如何走完整个系统；
> 4. 账号池、熔断、保活、任务引擎等关键机制的原理；
> 5. 与上游 Go 版的差异，以及为什么这样设计。
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
7. [客户端指纹白名单](#客户端指纹白名单)
8. [与上游 Go 版的差异](#与上游-go-版的差异)
9. [安全与合规](#安全与合规)

---

## 项目定位

腾讯 CodeBuddy（`copilot.tencent.com`）是官方闭源服务，**不提供任何 OpenAI 兼容的开放 API**。个人用户手里的多个 CodeBuddy 账号因此只能「在网页里用」，无法被本地工具（Cursor、Cline、DeepSeek Harness、自建 Agent、脚本……）以标准化方式调用。

本项目把这一层缺失的 API 边界补齐：

- **对上**：通过 OAuth 设备授权登录，把每个 CodeBuddy 账号变成一个可持续使用的上游身份；
- **对下**：暴露一个 OpenAI 兼容的 `/v1/chat/completions` 端点，本地工具零改造接入；
- **中间**：账号池、会话粘性、熔断、冷却、保活、定时任务、成长任务……全部本地运行，凭证与数据**不出本机**。

一句话：**本地运行的、开源的、多账号的、OpenAI 兼容反向代理网关**。

---

## 系统拓扑

```
                              ┌─────────────────────────────────────┐
                              │        浏览器 · WebUI 面板            │
                              │  (http://127.0.0.1:8789)             │
                              │  账号池 / 用量 / 任务 / 配置 / 日志     │
                              └─────────────┬───────────────────────┘
                                            │ HTTP (JSON)
                                            ▼
┌─────────────┐    OpenAI SDK / HTTP    ┌──────────────────────────┐
│  Cursor     │ ──── ─── ─── ─── ─── ──▶  │  wb_gateway.py           │
│  Cline      │    POST /v1/             │  ────────────────────    │
│  Claude     │    chat/completions      │  GatewayHandler          │
│  其它 Agent  │    (流式 / 非流式)          │  ├─ AccountPool         │
└─────────────┘                          │  ├─ 熔断 / 冷却           │
                                          │  ├─ SSE 透传 / 聚合       │
                                          │  └─ 面板 API              │
                                          └──────┬──────────┬───────┘
                                                 │          │
                                    REST (JWT)  │          │ 定时任务
                                                 ▼          ▼
                                     ┌──────────────┐   ┌────────────┐
                                     │ wb_tasks.py   │   │ wb_auth.py  │
                                     │ 任务引擎       │   │ OAuth 登录  │
                                     │ 成长 / 签到 /  │   │ token 刷新  │
                                     │ 保活 / 猫猫    │   └────────────┘
                                     └───────┬───────┘            │
                                             │                    │
                                             ▼                    ▼
                              ┌──────────────────────────────────────┐
                              │  auths/*.json   (明文 accessToken)    │
                              │  wb_data/*.json (积分快照 / 任务日志)   │
                              └──────────────────────────────────────┘
                                                     ▲
                              ┌──────────────────────────────────────┐
                              │ wb_credits_fetcher.py + wb_live_credits │
                              │ Playwright 扫码采集 + billing 直连查询    │
                              └──────────────────────────────────────┘

                     上游：https://copilot.tencent.com  (唯一外部依赖)
```

**关键点**：整个系统**只有 Python 标准库 + Playwright**，没有前端打包、没有 Redis 依赖（可选）、没有数据库。凭证（`auths/*.json`）与状态（`data/*.json`、`wb_data/*.json`）全部落本地磁盘。

---

## 目录结构

```
WorkBuddy_统一管理器/
├── main.py                        # 启动器：拉起 gateway 子进程 + WebUI 服务
├── wb_gateway.py                  # 核心：OpenAI 兼容网关 + 账号池 + 熔断 + 面板
├── wb_tasks.py                    # 任务引擎：17/18 成长任务 / 签到 / 保活 / 猫猫
├── wb_auth.py                     # OAuth 设备授权登录（三步：state → token → account）
├── wb_credits_fetcher.py          # Playwright 扫码采集积分/能量/Buddy
├── wb_live_credits.py             # 实时积分查询（直连 billing meter API）
├── wb_playwright_transport_patch.py  # Windows asyncio 子进程重叠管道补丁
├── wb_webui.py                    # Web 管理面板（单页 HTML + 后端 JSON API）
├── selfcheck.py                   # 自检：py_compile 全部模块 + JS 转义陷阱检查
├── browser_check.py               # 浏览器实测：用 Edge 打开控制台，确认按钮可点
├── test_webui.py                  # WebUI 单元测试
├── config.py                      # 账号清单（含示例手机号，脱敏占位）
├── config.example.json            # 网关配置模板（可 git 提交）
├── config.json                    # 用户真实配置（.gitignore 排除）
├── README.md                      # 快速上手 + 使用声明
├── ARCHITECTURE.md                # ← 本文档
├── LICENSE                        # MIT + 上游归属声明
├── .gitignore                     # 10 类排除规则
├── 一键启动.bat                    # Windows 双击启动
├── build.bat                      # Windows 打包脚本
├── auths/                         # [被排除] 明文 accessToken / refreshToken / uid
├── data/                          # [被排除] state.json / usage.json / request-logs/
└── wb_data/                       # [被排除] credits_history_*.json / tasks_log.jsonl
```

---

## 组件职责

| 模块 | 行数 | 职责 |
|---|---|---|
| `main.py` | 155 | 单一入口：以子进程方式启动 `wb_gateway.py`（网关 7863），用 `ThreadingHTTPServer` 启动 WebUI（8789）；统一优雅退出 |
| `wb_gateway.py` | 1673 | **心脏**。OpenAI 兼容网关 + `Account` / `AccountPool` dataclass + 熔断 + 会话粘性 + SSE 透传/聚合 + 面板 HTTP API |
| `wb_webui.py` | 1304 | 面板前端：单文件 HTML/CSS/JS，`__API_KEY__` 占位符在模块末尾 `.replace()` 注入，避免源码泄露真实 key |
| `wb_tasks.py` | 1352 | 任务引擎：17 个成长任务 + 签到 + 保活 + 猫猫旅行 + 连登管家；从**零重写**，不搬运上游 Go 源码 |
| `wb_credits_fetcher.py` | 1076 | Playwright 扫码登录 → 采集积分/能量/Buddy → 生成 HTML 报告；数据仅存本地 |
| `wb_auth.py` | 254 | OAuth 设备授权三步流程（`auth/state` → `auth/token` → `login/account`），落盘 `auths/workbuddy-<uid>.json`；uid 白名单校验防路径穿越 |
| `wb_live_credits.py` | 205 | 绕过本地快照，直连 `https://www.codebuddy.cn/api/billing/get-user-resource` 查询实时余额 |
| `wb_playwright_transport_patch.py` | 198 | Windows 下修复 `asyncio.create_subprocess_exec` 无法创建重叠管道的问题（须在 import playwright 之前调用） |
| `selfcheck.py` | 257 | `py_compile` 全部模块 + 检测 JS 三引号转义陷阱（HTML 内嵌 JS 里 `'\\n'` 被 Python 双转义成真实换行导致按钮全部失效的隐蔽 bug） |
| `browser_check.py` | 132 | 用真实 Edge 打开 WebUI，点击按钮，确认页面真的能用（`selfcheck.py` 查不出来的问题在这里发现） |
| `config.py` | 48 | 账号清单 + 网关 / 控制台 / 采集器 / 定时任务默认配置 |
| `config.example.json` | 1990 | 网关 `DEFAULT_CONFIG` 的可 git 提交模板（`config.json` 会被 `.gitignore` 排除） |

---

## 请求生命周期

一个典型的 `/v1/chat/completions` 请求从进入到返回，走的路径如下：

```
① 客户端发送请求
   POST http://127.0.0.1:7863/v1/chat/completions
   Authorization: Bearer sk-xxxx
   Body: { "model": "deepseek-v4-flash", "messages": [...], "stream": true }
        │
        ▼
② GatewayHandler._check_auth
   比对 Authorization 头与 config.api_key，不通过 → 401
        │
        ▼
③ GatewayHandler._handle_chat_completions
   解析请求体 → 确定 session_id（客户端 "user" 字段，缺省用 uuid4）
        │
        ▼
④ 账号选择 pool.pick(session_id)
   优先：session_id 已绑定的账号（会话粘性）
   次选：current_account_id（WebUI 手动切换）
   兜底：加权随机选号（偏快过期、优先未进入熔断、请求数少）
        │
        ▼
⑤ 构造上游请求
   chat_upstream_url()          → https://copilot.tencent.com/api/chat
   chat_upstream_headers()      → 注入该账号的 accessToken + User-Agent + Cookie
   模型兜底：不在 REAL_MODELS 白名单 → 改写到 deepseek-v4-flash
   强制 stream=true              （上游只接受流式，非流式实测返回 400 code=11101）
        │
        ▼
⑥ 向上游发起 urlopen (timeout=120s)
        │
        ├── stream=true ──▶ 边收边发：Content-Type: text/event-stream
        │                   while True: wfile.write(chunk); wfile.flush()
        │                   失败/超时 → 停止发送，客户端看到不完整流
        │
        └── stream=false ──▶ aggregate_sse_completion(resp, model)
                             把上游 SSE 帧解析为 content/reasoning_content
                             聚合为单个 OpenAI JSON 响应后一次性返回
        │
        ▼
⑦ 收尾
   成功 → pool.record_success(account.id)
          request_count++ / success_count++ / last_used=now()
   HTTPError(429) → pool.record_failure(acc.id, is_rate_limit=True)
                    进入 600s 起指数退避冷却，封顶 soft_rate_max
   HTTPError(404) → 60s 短冷却
   HTTPError(402) → 硬冷却至次日 04:00（额度耗尽）
   连续失败达 breaker_threshold(3) → breaker_until = now + breaker_cooldown(30m)
   Exception      → record_failure，headers_sent 时不再回传错误
        │
        ▼
⑧ 一行日志：uid / TTFB / token 速率 / 上游耗时 / 客户端 IP·UA
   落盘 data/request-logs/YYYY-MM-DD.log
```

**为什么强制上游 `stream=true`**：CodeBuddy 上游实测对非流式请求直接返回 `{"code":11101}`（业务错误，非 HTTP 状态码）。网关在客户端侧「拆流式再聚合」比「让上游开非流式」更稳定——聚合逻辑就在 `aggregate_sse_completion()`。

---

## 关键机制原理

### 1. 账号池调度（`AccountPool.pick`）

`Account` 是 dataclass，每个账号维护：

```python
id, name, token, phone, domain, credits, energy, active,
last_used,            # 最近一次被选中的时间戳
request_count,        # 累计请求数
success_count,        # 成功数
fail_count,           # 失败数
breaker_until,        # 熔断到期时间（epoch 秒）
cooldown_until,       # 冷却到期时间（epoch 秒）
balance_checked_at    # 上次刷新余额的时间戳
```

`pick(session_id)` 的决策顺序：

1. **会话粘性**：`session_id` 在 `sticky_sessions` 中且 TTL 未过期 → 返回该账号（多轮上下文不跳号）；
2. **手动切换**：`current_account_id` 非空 → 返回该账号（WebUI 里用户手动指定）；
3. **加权随机**：从可用账号中按权重抽选
   - 权重 = `f(credits 快过期程度, fail_count 倒数, request_count 倒数)`
   - `prefer_expiring=True` 时，快过期的账号权重更高（用得掉就用掉，别囤着）

选不出账号（全部熔断/冷却/余额耗尽）→ 503 `No available accounts`。

### 2. 熔断与冷却（`record_failure`）

上游返回不同错误时采取不同策略：

| 上游响应 | 含义 | 网关动作 |
|---|---|---|
| HTTP 429 | 请求过快 | 软冷却 600s 起，指数退避，封顶 `soft_rate_max` |
| HTTP 404 | 会话/模型路径错误 | 60s 短冷却 |
| HTTP 402 | 额度耗尽 | 硬冷却至次日 04:00 |
| 连续失败 ≥ `breaker_threshold`（默认 3） | 疑似坏号 | 熔断 `breaker_cooldown`（默认 30m） |
| HTTP 5xx / 超时 | 上游抖动 | 计入失败但不熔断，下个请求可能选中该号 |

`_is_available(acc)` 检查 `acc.active AND now() >= breaker_until AND now() >= cooldown_until`。

**`max_in_flight`（默认 3）**：单个账号同时在途的请求数上限，防止单号被打爆。

### 3. 会话粘性（`sticky_sessions`）

多轮对话如果跳号，上下文断裂。因此 `session_id` 会绑定账号：

- 首次请求：`session_id` 缺省时用 `uuid.uuid4()`；
- 每次成功请求后，`session_ttl[session_id] = now + TTL`；
- TTL 滚动续期；超时后自动解绑，下次请求重新选号；
- 失败请求立即解绑（这个号不适合当前会话）；
- 可选镜像到 Redis（`STATE_REDIS_URL` 环境变量），重启不丢。

### 4. Token 刷新（`refresh_auth_tokens` + `decode_jwt_payload`）

CodeBuddy accessToken 有效期较短（分钟级）。网关在每次 `pick()` 之后、请求上游之前，检查 token 是否接近过期：

- `decode_jwt_payload(token)` 解出 payload（`exp` / `uid` / `phone`）；
- 若 `exp - now < 阈值`，调用 `refresh_auth_tokens(force=True)`：
  - 用本地保存的 refreshToken 向 `https://www.codebuddy.cn` 申请新的 accessToken；
  - 新 token 写回 `auths/workbuddy-<uid>.json`；
  - 更新内存中的 `Account.token`；
- 刷新失败 → 该账号标记 `active=False`，走熔断流程。

### 5. SSE 帧透传与非流式聚合

- **流式客户端**：网关原封不动把上游的 `text/event-stream` 字节流写给客户端（`Content-Type: text/event-stream` + `Cache-Control: no-cache` + `Connection: close`）；
- **非流式客户端**：上游永远返回流，网关用 `aggregate_sse_completion(resp, model)` 逐帧解析：
  - 提取 `choices[0].delta.content` → 拼接为完整 `content`；
  - 提取 `choices[0].delta.reasoning_content` → 拼接为 `reasoning_content`；
  - 遇到 `choices[0].finish_reason` → 结束；
  - 组装为单个 OpenAI JSON 响应，`choices[0].message.content` 一次性返回。

**SSE 帧白名单重建**：某些客户端（老版 SDK）对非规范帧报错，网关会按白名单字段重建每一帧，只保留 `id / object / created / model / choices / usage`。

### 6. 保活与定时任务

`schedule` 配置驱动独立定时器，任务在 `wb_tasks.py` 里定义：

| 定时任务 | 默认时间 | 说明 |
|---|---|---|
| 签到（checkin） | 09:00 / 21:00 | 每天两次，末尾自动跑「连登管家」：兑换已解锁档位 + 抽完抽奖次数 |
| 活跃上报 | 10:00 | 点亮连登、解锁领养、streak 自检 |
| 猫猫旅行 | 09:00 / 21:00 | 独立排程，走 `RichMeow_Chat` 接口 |
| Token 保活 | 22:00 | 对每个账号强制刷新一次 token，防止长时间闲置后失效 |
| 余额刷新 | 每 5 分钟 | 从 billing API 拉最新积分，写入 `data/usage.json` |

任务引擎全部**幂等**：`wb_tasks.py` 头部声明「全部函数保持幂等：已完成自动跳过，重复执行不会产生重复奖励」——因为积分系统是「已完成返回 200 但不再加积分」的，脚本重复跑安全。

### 7. 成长任务一键完成

官方 18 个成长任务里，**17 个走公开 API 就能完成**（`first_buddy` / `create_canvas` / `chat_5` / `Model_chat_GLM5.2` / `RichMeow_Chat` / `Buddy_App` / `Buddy_App_QQ` / `automation_1` / `Library_read` / `template_5` / `playbook_prompt` / `expert_5` / `Expert_team_use_3` …）。

任务引擎对每个任务做：

1. **扫描**：调 `chat/tasks` 接口拿账号当前完成状态；
2. **accept**：若任务未开始，先 `accept`；
3. **report**：调对应业务接口产生「完成证据」；
4. **claim**：等异步计分落定后 `claim` 领取积分；
5. **落盘**：写入 `wb_data/tasks_log.jsonl`，便于审计。

**为什么从零重写而不是搬运上游 Go**：上游 Go 版把任务定义、扫描、领取混在 `task_common.py` 结构里，可读性差。这里改成扁平的 TASKS 目录 + 独立函数，全部幂等，方便在 WebUI 里点一下「一键完成」就自动推进。

---

## 客户端指纹白名单

CodeBuddy 上游有一个隐蔽的防御机制：**请求体里如果缺少桌面客户端指纹字段，服务端会返回 HTTP 200 但静默丢弃**（不报错，但增长积分不涨）。这是从上游 Go 版 `panel-main/internal/upstream/desktop.go` 逆向对齐的字段集合。

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

同理，网关出站时对客户端请求体的**指纹字段做黑名单清洗**（`chat_upstream_headers` 里可关闭），避免把用户本地工具（Claude / Cursor / Cline）的真实环境信息透给上游——这是**指纹脱敏**：两层叠加（提示词替换 + 指纹清洗）一起把「这是哪台机器、哪个人在跑」的信息剥离掉。

---

## 与上游 Go 版的差异

本项目是 [Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) 的 fork 分支，上游现已删除。核心改造点：

| 维度 | 上游 Go 版 | 本项目 |
|---|---|---|
| 语言 | Go 1.22.5 | Python 3.10+ |
| 部署形态 | 单二进制 / Docker | 双击 `一键启动.bat` / `python main.py` |
| 任务引擎 | 混在 `task_common.py` | **从零重写**，扁平 TASKS 目录，全部幂等 |
| 可视化 | 无（CLI） | WebUI 面板（账号池 / 用量 / 任务 / 配置 / 日志 / 积分） |
| 自检 | 无 | `selfcheck.py`（py_compile + JS 转义）+ `browser_check.py`（Edge 实测） |
| 依赖 | Go 标准库 | Python 标准库 + Playwright（仅积分采集器用） |
| 状态存储 | 本地 JSON | 本地 JSON + 可选 Redis 镜像 |
| 指纹处理 | 桌面指纹透传 | 桌面指纹白名单 + 客户端指纹黑名单清洗 |

**保留的设计**：账号池调度算法、错误分类策略（429/404/402 分级）、模型白名单兜底、OpenAI 兼容协议、会话粘性思路。

**放弃的东西**：Docker 部署、Redis 强依赖、上游 Go 版任务定义结构。

---

## 安全与合规

### 安全边界

1. **凭证安全**：`auths/*.json` 保存**明文** accessToken / refreshToken / uid——**这就是为什么 `.gitignore` 强制排除该目录**。任何「收费版」「卡密版」如果让你上传凭证到别人的服务器，都是把账号交给陌生人。
2. **数据边界**：网关、任务引擎、采集器**全部本地运行**，不经过任何第三方服务器；唯一外部依赖是上游 `copilot.tencent.com`。
3. **网络边界**：网关默认监听 `127.0.0.1:7863`（仅本机），面板监听 `127.0.0.1:8789`。若需要对外，务必自行加反代与鉴权。
4. **来源审计**：每条请求日志记录**调用来源 IP · User-Agent**，便于事后追溯「哪台机器用了我的账号」。
5. **脱敏展示**：`mask_phone()` 把手机号中段掩码（`188****1421`）；`decode_jwt_payload()` 解出的 uid / phone 仅在面板和内部使用，不透给上游。

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

1. `main.py`（155 行）—— 看清楚系统怎么起来的；
2. `wb_gateway.py` L1–L180 —— `Account` / `AccountPool` 数据模型和调度入口；
3. `wb_gateway.py` L1244–L1345 —— `_handle_chat_completions`，请求生命周期的核心；
4. `wb_auth.py` —— OAuth 三步登录，理解凭证怎么落盘的；
5. `wb_tasks.py` 头部常量区 —— 桌面指纹白名单；
6. `wb_webui.py` L1–L100 —— 面板骨架；
7. `wb_credits_fetcher.py` 头部 —— Playwright 采集器与报告生成。

有任何问题，欢迎提 Issue；如果理解了代码，欢迎提 PR。
