# DSH 接入说明

> **状态：已经替你装好了**（2026-10-17）。你不需要再手动粘贴任何片段；本文说明「装了什么 / 怎么验证 / 怎么回滚」，以及出问题时的对照表。

## 一、装了什么

两处改动（都在本机 DSH 的用户配置里，包内文件未动）：

| # | 文件 | 改动 |
|---|---|---|
| 1 | `C:\Users\<你的用户名>\.dsh\.credentials.yaml` | 在 `refs:` 段新增一行 `WORKBUDDY_API_KEY: test_key` —— 这是 `apiKeyEnv` 的解析源（每请求解析一次） |
| 2 | `C:\Users\<你的用户名>\.dsh\profiles\desktop\cordis.patch.yml` | 在 `llm-pi-ai` → `config` → `providers` 下新增 `workbuddy-manager` 路由（第 45-119 行，位于 `sensenova` 之前） |

改动前的两份备份（同目录同名 + `.bak-pre-workbuddy` 后缀）：

- `C:\Users\<你的用户名>\.dsh\.credentials.yaml.bak-pre-workbuddy`
- `C:\Users\<你的用户名>\.dsh\profiles\desktop\cordis.patch.yml.bak-pre-workbuddy`

### 路由长什么样

```yaml
workbuddy-manager:
  displayName: WorkBuddy 统一管理器
  apiKeyEnv: WORKBUDDY_API_KEY
  api: openai-completions
  baseURL: http://127.0.0.1:7863/v1
  defaultContextWindow: 262144
  defaultMaxTokens: 32768
  models:            # 15 个，必须非空
    - id: deepseek-v4-flash      # 网关默认兜底模型
    - id: deepseek-v4.1-flash
    - id: deepseek-v4.1-flash-sg # 仅国际版
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

声明支持图片的是 6 个：`deepseek-v4-flash`、`deepseek-v4.1-flash`、`deepseek-v4-pro`、`glm-5.3`、`glm-5.3-flash`、`glm-5.2`。

### 为什么是这种写法（DSH 的约束）

1. **手工声明的路由不能走 OAuth 登录**（写凭据记录会报 `UNSTORABLE_PROVIDER_ID`），只能用 `apiKeyEnv`；这也是改 `.credentials.yaml` 的 `refs` 而不用 `setx` 的原因（同样效果，不必开新终端）。
2. **`models` 是「整表替换」语义**：自建路由不给 `models` 就一个模型都用不了，所以必须列出来。
3. **`api: openai-completions` + `baseURL` 到 `/v1`**：pi-ai 自己会拼 `/chat/completions`，所以 baseURL 不要带 `/chat/completions`。
4. 路由键必须是小写连字符形式（`workbuddy-manager`）。

## 二、使用前提

网关必须先起来，DSH 才连得上：

```bat
双击  D:\多账号管理器\一键启动.bat
:: 或者，仓库克隆到任意位置后，直接双击仓库根目录里的 一键启动.bat
```

它会拉起网关（`http://127.0.0.1:7863`）并打开统一面板 `http://127.0.0.1:7863/panel/`。网关关掉时，DSH 里选这个 provider 会报连接失败（ECONNREFUSED）。

## 三、怎么验证

```bash
# 1) 网关自己通不通（模型列表）
curl -s http://127.0.0.1:7863/v1/models -H "Authorization: Bearer test_key"

# 2) 聊天通不通
curl -s http://127.0.0.1:7863/v1/chat/completions \
  -H "Authorization: Bearer test_key" -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}]}'
```

3) 在 DSH 里把模型选成 **WorkBuddy 统一管理器 → DeepSeek-V4-Flash** 发一句话。成功后网关窗口会打印访问日志与模型行；面板「Token 统计」页也会出现这条用量。

## 四、报错对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| DSH 报 `MISSING_CREDENTIAL` | `apiKeyEnv` 解析不到值 | 检查 `.credentials.yaml` 的 `refs.WORKBUDDY_API_KEY` 是否还在；或改用 `setx WORKBUDDY_API_KEY test_key` 并重开 DSH |
| 网关返回 401 `authentication_error` | DSH 发的 key 与 `config.json` 的 `api_key` 不一致 | 两边改成同一个值（`config.json` 改完要重启网关） |
| `ECONNREFUSED 127.0.0.1:7863` | 网关没启动 | 先双击 `一键启动.bat` |
| `Upstream rejected the request (HTTP 400 code=…): …` | 请求本身有问题（模型名不存在、`messages` 为空、参数被拒），**不是账号坏了** | 按冒号后的上游原话改请求；这次不会影响账号健康。旧版本这时只会显示 `Upstream error: 400` |
| 短时间连续报 400 后一路 503 | 旧版本会把客户端坏请求算成账号失败并熔断 | 已修；若跑的是旧代码，重启网关（熔断状态只存在内存里） |
| 503 `No available accounts: …` | 账号池为空 / 全部不可用 | 面板「账号管理」页扫码登录；或等冷却结束 |
| DSH 里模型名和请求的不一样 | 网关的 `resolve_model` 兜底（未知 / 跨区 / 被隐藏的模型会换成可用模型，日志有「模型 X -> Y」） | 正常行为；想用某模型请在面板「模型」页启用它 |
| 改了路由但没生效 | `providers` 是**装载期**配置 | 完全退出 DSH 再重开（不是刷新页面） |

## 五、改密钥 / 换网关地址

- 换密钥：改 `.credentials.yaml` 的 `refs.WORKBUDDY_API_KEY`（或 `setx`），并保证与 `config.json` 的 `api_key` 相同。
- 换地址：改 `cordis.patch.yml` 里 `workbuddy-manager.baseURL`（比如网关部署在另一台机器）。
- 两处都是装载期读取，改完**完全退出并重开 DSH**。

## 六、回滚

删掉 `cordis.patch.yml` 里 `workbuddy-manager:` 整块（第 45-119 行）与 `.credentials.yaml` 里 `WORKBUDDY_API_KEY` 那一行即可；也可以直接用两份 `.bak-pre-workbuddy` 备份覆盖回去，然后重开 DSH。

## 七、可选：把它设成默认模型

`WorkBuddy_统一管理器\dsh\cordis.workbuddy.patch.yml` 里附了一段被注释的 `agent-default-model` 写法，把 provider 改成 `workbuddy-manager`、model 改成 `deepseek-v4-flash` 即可。默认值目前是 `nvidia-vision / deepseek-ai/deepseek-v4.1-flash`，不改也没关系。

## 八、诚实声明

本次改动是**静态校验**通过的：用 YAML 解析器回读了 `cordis.patch.yml`（顶层 12 个条目、`llm-pi-ai.providers` 键为 `workbuddy-manager, sensenova, nvidia`、`workbuddy-manager` 含 15 个模型）。本机无法启动子进程（`0xC0000142`），所以**没有做过 DSH 真机端到端**——请按第三节跑一次，确认后再长期使用。
