# DSH 接入说明

> 本文说明「DSH（DeepSeek Harness）怎么接到本网关」，包括装了什么、怎么验证、出问题怎么对照、怎么回滚。
> 配套片段在 [`docs/dsh/cordis.workbuddy.patch.yml`](dsh/cordis.workbuddy.patch.yml)。

---

## 一、原理

DSH 通过 OpenAI 兼容协议访问网关：

```
DSH  ──(OpenAI /chat/completions, Bearer key)──▶  core/wb_gateway.py :7863  ──▶  WorkBuddy 上游
```

网关对上游永远发 `stream: true`，再按客户端请求决定是否聚合成非流式响应；所以 DSH 端用流式或非流式都能工作。协议细节见 [README 「流式行为细节」](../README.md#流式行为细节)。

## 二、接管了什么

两处改动（都在本机 DSH 的用户配置里，**包内文件未动**）：

| # | 文件 | 改动 |
|---|---|---|
| 1 | `C:\Users\<用户名>\.dsh\.credentials.yaml` | `refs:` 段新增 `WORKBUDDY_API_KEY: test_key` —— 这是 `apiKeyEnv` 的解析源（每请求解析一次） |
| 2 | `C:\Users\<用户名>\.dsh\profiles\desktop\cordis.patch.yml` | `llm-pi-ai` → `config` → `providers` 下新增 `workbuddy-manager` 路由 |

改动前的两份备份（同目录同名 + `.bak-pre-workbuddy` 后缀）：

- `C:\Users\<用户名>\.dsh\.credentials.yaml.bak-pre-workbuddy`
- `C:\Users\<用户名>\.dsh\profiles\desktop\cordis.patch.yml.bak-pre-workbuddy`

### 路由长什么样

仓库里的片段文件就是这一段（可以直接复制粘贴）：

```yaml
workbuddy-manager:
  displayName: WorkBuddy 统一管理器
  apiKeyEnv: WORKBUDDY_API_KEY
  api: openai-completions
  baseURL: http://127.0.0.1:7863/v1
  defaultContextWindow: 262144
  defaultMaxTokens: 32768
  models:            # 必须非空
    - id: deepseek-v4-flash      # 网关默认兜底模型
    - id: deepseek-v4.1-flash
    - id: deepseek-v4.1-flash-sg # 仅国际版账号
    - id: deepseek-v4-pro
    - id: deepseek-v3
    - id: deepseek-v3.2
    - id: deepseek-r1            # reasoningEfforts: low / high
    - id: glm-5.3                # 支持图片 + low/high/max
    - id: glm-5.3-flash          # 支持图片 + low/high/max
    - id: glm-5.2                # 支持图片
    - id: glm-5.1                # 纯文本
    - id: kimi-k3
    - id: minimax-m3
    - id: hy3                    # 仅国内版，被限流时网关自动退到 hy3-x
    - id: hy4-preview            # 限时免费
```

声明支持图片的是 6 个：`deepseek-v4-flash`、`deepseek-v4.1-flash`、`deepseek-v4-pro`、`glm-5.3`、`glm-5.3-flash`、`glm-5.2`。实际可用的模型以网关 `GET /v1/models` 返回为准（会随账号区域变化）。

### 为什么是这种写法（DSH 的约束）

1. **手工声明的路由不能走 OAuth 登录**（写凭据记录会报 `UNSTORABLE_PROVIDER_ID`），只能用 `apiKeyEnv`；这也是改 `.credentials.yaml` 的 `refs` 而不用 `setx` 的原因（同样效果，不必开新终端）。
2. **`models` 是「整表替换」语义**：自建路由不给 `models` 就一个模型都用不了，所以必须列出来。
3. **`api: openai-completions` + `baseURL` 到 `/v1`**：pi-ai 自己会拼 `/chat/completions`，所以 baseURL 不要带 `/chat/completions`。
4. 路由键必须是小写连字符形式（`workbuddy-manager`）。

## 三、使用前提：先把网关起来

```bat
:: 仓库根目录双击
一键启动.bat

:: 或命令行（本仓库目录结构：入口在 scripts/ 下）
python scripts/main.py
```

它会拉起网关（`http://127.0.0.1:7863`）并打开统一面板 `http://127.0.0.1:7863/panel/`。网关关掉时，DSH 里选这个 provider 会报连接失败（ECONNREFUSED）。

> 网关与面板全用标准库，**不需要装任何第三方包**就能跑；只有积分采集和浏览器自检要 `playwright`（见 README「环境要求」）。

## 四、怎么验证

```bash
# 1) 鉴权 + 模型列表
curl -s http://127.0.0.1:7863/v1/models -H "Authorization: Bearer test_key"

# 2) 聊天通不通
curl -s http://127.0.0.1:7863/v1/chat/completions \
  -H "Authorization: Bearer test_key" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}]}'
```

3) 在 DSH 里把模型选成 **WorkBuddy 统一管理器 → DeepSeek-V4-Flash** 发一句话。成功后网关窗口会打印访问日志与模型行；面板「Token 统计」页也会出现这条用量。

4) 仓库自带的整套自检（18 个模块语法 + 4 段 JS + 16 个 HTTP 端点，需服务已在运行）：

```bash
python scripts/selfcheck.py --online
```

## 五、报错对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| DSH 报 `MISSING_CREDENTIAL` | `apiKeyEnv` 解析不到值 | 检查 `.credentials.yaml` 的 `refs.WORKBUDDY_API_KEY` 是否还在；或改用 `setx WORKBUDDY_API_KEY test_key` 并重开 DSH |
| 网关返回 401 `authentication_error` | DSH 发的 key 与 `config.json` 的 `api_key` 不一致 | 两边改成同一个值（`config.json` 改完要重启网关） |
| `ECONNREFUSED 127.0.0.1:7863` | 网关没启动 | 先双击 `一键启动.bat` 或 `python scripts/main.py` |
| `Upstream rejected the request (HTTP 400 code=…): …` | 请求本身有问题（模型名不存在、`messages` 为空、参数被拒），**不是账号坏了** | 按冒号后的上游原话改请求；不会影响账号健康。旧版本这时只会显示 `Upstream error: 400` |
| 短时间连续报 400 后一路 503 | 旧版本会把客户端坏请求算成账号失败并熔断 | 已修；若跑的是旧代码，重启网关（熔断状态只存在内存里） |
| `Upstream error 400: … 请求被拦截…`（上游 code 11128） | 上游反探测拦截 | 网关会自动原地重发（最多 2 次）再换号；连续出现说明整池都被拦，稍等再试 |
| 503 `No available accounts: …` | 账号池为空 / 全部熔断冷却中 | 面板「账号管理」页扫码登录；或等冷却结束（提示里带剩余时间） |
| DSH 里模型名和请求的不一样 | 网关的 `resolve_model` 兜底（未知 / 跨区 / 被隐藏的模型会换成可用模型，日志有「模型 X -> Y」） | 正常行为；想用某模型请在面板「模型」页启用它 |
| 改了路由但没生效 | `providers` 是**装载期**配置 | 完全退出 DSH 再重开（不是刷新页面） |

错误响应体的统一形状：

```json
{"error":{"message":"Upstream error <code>: <上游原文>","type":"api_error","code":<code>}}
```

`type` 按状态码推断：≥500 → `api_error`、401/403 → `authentication_error`、404 → `not_found_error`、其余 → `invalid_request_error`。

## 六、改密钥 / 换网关地址

- 换密钥：改 `.credentials.yaml` 的 `refs.WORKBUDDY_API_KEY`（或 `setx`），并保证与 `config.json` 的 `api_key` 相同。
- 换地址：改 `cordis.patch.yml` 里 `workbuddy-manager.baseURL`（比如网关部署在另一台机器或换了端口）。
- 两处都是装载期读取，改完**完全退出并重开 DSH**。

## 七、回滚

删掉 `cordis.patch.yml` 里的 `workbuddy-manager:` 整块与 `.credentials.yaml` 里 `WORKBUDDY_API_KEY` 那一行即可；也可以直接用两份 `.bak-pre-workbuddy` 备份覆盖回去，然后重开 DSH。

## 八、可选：把它设成默认模型

[`docs/dsh/cordis.workbuddy.patch.yml`](dsh/cordis.workbuddy.patch.yml) 里附了一段被注释的 `agent-default-model` 写法，把 provider 改成 `workbuddy-manager`、model 改成 `deepseek-v4-flash` 即可。不改也没关系。

## 九、诚实声明

路由片段经过 YAML 解析器回读校验（顶层条目、`llm-pi-ai.providers` 键、`workbuddy-manager` 的模型数）。网关侧的协议行为由 `python scripts/selfcheck.py --online`（16 个 HTTP 端点）与真实的 `/v1/chat/completions` 请求验证过；但**DSH 客户端自身的端到端**只能在你本机按第四节第 3 步实测确认。
