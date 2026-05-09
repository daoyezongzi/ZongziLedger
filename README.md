# ZongziLedger

ZongziLedger 是一个本地 Windows 记账脚本：从微信窗口抓取记账消息，解析后写入 CSV，并同步输出 JSON 账单明细。

当前默认抓取后端：`win32_memory`（进程内存扫描）。

## 新增内容速览（2026-05-10）

- 强制业务格式：`#记账 + yymmdd + 两位序号 + 明细 + 结束`（无合法单号会被过滤）。
- 默认日结模式：首次运行只建基线不入账；后续只处理新增消息。
- 新增 `dify_bridge_server.py`：支持“远端 Dify 优先 + 本地回退 + 审计落盘”。
- 新增 `capture_to_dify.py` / `run_capture_to_dify.bat`：抓取后自动推送到本地 Dify bridge。
- 新增 `dify_receiver.py`：纯本地接收并落账，支持失败数据写入复核队列。
- 新增已知店铺直查（`core/store_lookup.py` + `known_stores.*.json`），可在解析后补充店铺标注。

## 使用方法（重点）

### 1) 先按固定格式发消息（必须）

```text
#记账26010101
红茶3
绿茶2
结束
```

规则：

- 首行必须是 `#记账` + `yymmdd` + 两位序号（例如 `#记账26010101`）。
- `yymmdd` 必须是合法日期（如 `260230` 会被判定为非法日期并忽略）。
- 最后一行必须是 `结束`。
- 中间每行必须是 `项目 + 数量`（例如 `青梅绿茶1`、`红茶 x 3`）。

### 2) 推荐一键运行

```bat
run_zongziledger.bat
```

脚本会自动：

- 检测 Python（优先 3.12）；
- 创建/复用虚拟环境（`.venv312` 或 `.venv`）；
- 安装基础依赖（`uiautomation`、`pyyaml`）；
- 执行 `main.py`。

### 3) 理解默认“日结模式”行为

当前配置 `daily_settlement_mode: true`：

- 第一次运行：只初始化当日基线（把当前可见历史消息标记为已见），本次不会写入 CSV/JSON。
- 后续运行：只写入当日新增消息，且同一条消息不会重复入账。

### 4) 每天操作建议

1. 在微信按规范发送记账块（带新单号）。
2. 手动执行一次 `run_zongziledger.bat`。
3. 查看账本/明细/日志（默认 `data/ledger.csv`、`data/ledger_details.json`、`logs/capture_YYYYMMDD_HHMMSS.log`，若用 `.env` 则以覆盖路径为准）。

### 5) 需要重置日结基线时

在 `config.yaml` 暂时改为：

```yaml
daily_force_reinitialize: true
```

运行一次后会重新初始化（本次不入账），然后改回：

```yaml
daily_force_reinitialize: false
```

### 6) 如果你想改成“实时去重模式”（非日结）

把 `config.yaml` 调整为：

```yaml
daily_settlement_mode: false
capture_scope: "today_new"
message_hash_dedupe_enabled: true
message_hash_window_seconds: 120
```

说明：

- 关闭日结后，不再有“首次初始化不入账”行为。
- 同一条消息会按“当天已见 + hash 时间窗”规则去重。
- 适合一天内多次执行、希望更即时入账的场景。

### 7) 运行成功时你会看到什么

- 首次日结初始化：控制台提示“已建立历史基线，本次不写入账单”。
- 正常入账：控制台会显示“本次成功写入 N 条记录”，并输出 JSON 写入条数。
- 无新增消息：控制台提示“本次未发现可入账的新消息”。

## Dify 接口接入（本地）

建议先选模式，再按对应步骤启动：

- `dify_bridge_server.py`：本地接收 + 可选远端 Dify API（推荐默认模式）。
- `dify_receiver.py`：纯本地接收落账，不主动请求远端 Dify（用于只收上游结构化输出）。

### 模式 A：Bridge（推荐）

`dify_bridge_server.py` 保留本地 HTTP 接口，并支持“优先调用 Dify API，失败回退本地解析”：

- 优先调用 Dify API（可配置）
- 统一格式校验（含单号头校验）
- 去重（按配置：hash 时间窗 / 可选 capture_scope / 可选日结）
- 写入 CSV 与 JSON（与 `main.py` 共用同一套落账逻辑）
- 审计记录（保存请求与入账摘要，便于回放核对）

### 启动服务

```powershell
python dify_bridge_server.py
```

或一键启动：

```bat
run_dify_bridge.bat
```

默认监听：

- 接收：`POST http://127.0.0.1:8787/api/dify/ingest`
- 健康检查：`GET http://127.0.0.1:8787/health`

### 微信抓取后自动推送到 Dify

新增串联脚本：`capture_to_dify.py`（以及 `run_capture_to_dify.bat`）。

它会执行：

1. 自动抓取微信消息（复用 `main.py` 的抓取后端配置）
2. `POST` 到本地 bridge：`/api/dify/ingest`
3. 由 bridge 决定“远端 Dify 优先/本地回退”，最终落账到 CSV/JSON

运行命令：

```powershell
python capture_to_dify.py
```

或一键运行：

```bat
run_capture_to_dify.bat
```

推荐调试顺序：

1. 打开微信并停留在目标聊天窗口
2. 启动 `run_dify_bridge.bat`
3. 再运行 `run_capture_to_dify.bat`

可选配置：

- `dify_ingest_url`：完整地址覆盖（例如 `http://127.0.0.1:8787/api/dify/ingest`）
- `dify_ingest_timeout_seconds`：推送超时秒数（默认 30）
- Dify Prompt/Schema 参考模板：`dify_prompt_and_schema.md`

### Dify 输出接入格式（推荐）

```json
{
  "messages": [
    {
      "message": "#记账26050901\n汤呈工地小卖部\n红茶3\n绿茶2\n结束",
      "name": "汤呈工地小卖部",
      "timestamp": "2026-05-09 10:23:45",
      "source_id": "wechat-group-a"
    }
  ]
}
```

也兼容单条消息：

```json
{
  "message": "#记账26050902\n青梅绿茶1\n结束"
}
```

说明：

- 核心字段是 `message`（或 `normalized_block`）；
- 可选字段 `name`：若消息中未注明名字，传空字符串 `""`；
- 可选字段：`timestamp`、`source_id`、`message_hash`、`message_captured_at`；
- 也支持单条模式：`{"message":"..."}`；
- 默认策略是 `dify_remote_priority=true`（优先 Dify API）；
- 当远端失败且 `dify_remote_fallback_local=true` 时，会自动回退本地解析链路；
- 默认审计文件：`data/dify_ingest_audit.jsonl`。

### TradeEye 同款推送模式（推荐）

该模式与 `TradeEye` 一致：本地服务主动 `POST` 到 Dify `workflows/run`，拿到 `data.outputs` 后再落本地账本。

最小配置：

```yaml
dify_remote_enabled: true
dify_remote_priority: true
dify_remote_fallback_local: true
dify_remote_api_url: "https://api.dify.ai/v1/workflows/run"
dify_remote_api_key: "app-xxxx"
dify_remote_response_mode: "blocking"
dify_remote_user: "zongziledger-local"
```

说明：

- 认证方式：`Authorization: Bearer <dify_remote_api_key>`；
- 入参会自动包含 `inputs.query`（首条消息文本）；
- 同时会带 `payload_json` 与 `messages_json`（键名可由 `dify_remote_input_*` 配置覆盖）；
- 若远端结果不可解析且开启回退，会自动走本地解析链路。

### 模式 B：纯本地接收（可选）

启动：

```powershell
python dify_receiver.py
```

默认监听：

- 接收：`POST http://127.0.0.1:18888/api/dify/ingest`
- 健康检查：`GET http://127.0.0.1:18888/api/health`

本地自测请求（PowerShell）：

```powershell
$payload = @{
  messages = @(
    @{
      message   = "#记账26051001`n红茶3`n结束"
      source_id = "manual-test"
      timestamp = "2026-05-10 12:00:00"
    }
  )
} | ConvertTo-Json -Depth 6

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:18888/api/dify/ingest" `
  -ContentType "application/json; charset=utf-8" `
  -Body $payload
```

适用场景：

- 你已经有上游系统（含 Dify）产出消息 JSON，只需要本地接收并落账。
- 你希望把格式不合法或解析失败的数据写入复核队列，后续人工回放。

关键配置：

- `dify_listen_host` / `dify_listen_port` / `dify_ingest_path` / `dify_health_path`
- `dify_max_payload_bytes`：请求体大小限制
- `review_queue_enabled` / `review_queue_path`：复核队列开关与路径

## 运行方式（命令行）

```powershell
python main.py
```

或先补依赖再运行：

```powershell
python setup.py
python main.py
```

## `.env` 本地测试说明

程序会自动读取项目根目录 `.env`，并覆盖 `config.yaml` 中同名配置（规则：`ENV_KEY -> env_key`）。

示例：

- `MAX_MESSAGES=30` -> `max_messages`
- `DATA_PATH=data/ledger.local.csv` -> `data_path`
- `DAILY_SETTLEMENT_MODE=true` -> `daily_settlement_mode`
- `WECHAT_WINDOW_NAME=微信` -> `wechat_window_name`
- `MESSAGE_PANE_CLASS_NAME=MMUIRenderSubWindowHW` -> `message_pane_class_name`

建议：

- 上传仓库时保留 `config.yaml` 与 `.env.example`；
- 本地机器使用 `.env`；
- `.env` 已加入 `.gitignore`，避免误提交。
- 机器相关参数（窗口类名、窗口标题、本地输出路径）优先放 `.env`，不要写死到仓库配置。

## 关键配置（config.yaml）

```yaml
prefix: "#记账"
record_start_marker: "记账"
record_end_marker: "结束"

capture_mode: "auto"
capture_backend: "win32_memory"

capture_scope: "all"
capture_state_dir: "data"
deduplicate_within_run: false
show_item_totals: true
item_totals_top_n: 20

daily_settlement_mode: true
daily_recent_message_limit: 3
daily_seen_retention_days: 30
daily_force_reinitialize: false

order_id_required: true
order_id_digits: 8
order_id_require_hash: true

known_store_lookup_enabled: false
known_store_lookup_path: "data/known_stores.local.json"
known_store_lookup_max_matches: 3

message_hash_dedupe_enabled: true
message_hash_window_seconds: 120
message_hash_state_retention_seconds: 604800

json_output_enabled: true
json_output_append_history: true

max_messages: 30

win32_memory_overlap_bytes: 4096
win32_memory_max_chunk_bytes: 2097152
win32_memory_recent_blocks: 6
win32_memory_max_block_chars: 2000
win32_memory_min_payload_lines: 2
win32_memory_max_payload_lines: 40
win32_memory_min_valid_line_ratio: 0.8
win32_memory_require_today_token: false

dify_api_host: "127.0.0.1"
dify_api_port: 8787
dify_ingest_url: ""
dify_ingest_timeout_seconds: 30
dify_apply_capture_scope: false
dify_use_daily_settlement: false
dify_use_hash_time_window: true
dify_audit_enabled: true
dify_audit_path: "data/dify_ingest_audit.jsonl"
dify_response_preview_limit: 20
dify_remote_enabled: true
dify_remote_priority: true
dify_remote_fallback_local: true
dify_remote_api_url: ""
dify_remote_api_key: ""
dify_remote_timeout_seconds: 30
dify_remote_response_mode: "blocking"
dify_remote_user: "zongziledger-local"
dify_remote_user_agent: "python-requests/2.32.3"
dify_remote_input_payload_key: "payload_json"
dify_remote_input_messages_key: "messages_json"
dify_remote_inputs_as_json_text: true

dify_listen_host: "127.0.0.1"
dify_listen_port: 18888
dify_ingest_path: "/api/dify/ingest"
dify_health_path: "/api/health"
dify_max_payload_bytes: 2097152
dify_source_id: "dify"

review_queue_enabled: true
review_queue_path: "data/review_queue.jsonl"
```

配置说明：

- `capture_mode`：`auto` 自动抓取；`manual` 手动粘贴记账块（输入 `END` 结束）。
- `capture_backend`：`win32_memory` / `win32_clipboard` / `uia`。
- `capture_scope=all`：不过滤当天已见消息。
- `capture_scope=today_new`：同一天内同一原始消息只处理一次（状态文件：`data/seen_messages_today.json`）。
- `daily_recent_message_limit`：日结时仅处理最近 N 条候选消息，减少历史残留干扰。
- `message_hash_dedupe_enabled`：仅在非日结模式下按 hash+时间窗去重。
- `daily_settlement_mode=true` 时，`daily_recent_message_limit` 会优先生效，通常不再依赖 `message_hash_dedupe_enabled`。
- `known_store_lookup_enabled`：开启后会从 `known_store_lookup_path` 加载“已知店铺名录”并做直查，不依赖 LLM 推理。
- Bridge 模式默认建议：`dify_use_daily_settlement=false`，避免首次调用触发“仅初始化不入账”。
- Bridge 远端优先建议：设置 `dify_remote_api_url` + `dify_remote_api_key`，并保留 `dify_remote_fallback_local=true`。
- 抓取推送可选 `dify_ingest_url` 覆盖目标地址；未设置时默认拼接 `dify_api_host + dify_api_port + /api/dify/ingest`。
- 纯本地接收端口使用 `dify_listen_host` + `dify_listen_port`，与 bridge 端口独立（默认 `18888`）。
- `review_queue_enabled=true` 时，格式不合法/解析失败项会写入 `review_queue_path` 便于人工复核。

## 已知店铺直查接口（预留）

- 这是预留接口，默认关闭；开启后仅做“名录匹配”，不改你现有的 LLM 流程设计。
- 名录路径默认是 `data/known_stores.local.json`（本地文件，默认不会被上传）。
- 匹配命中后，记录里会补充 `known_store` 与 `known_store_candidates` 字段（CSV 原字段不变）。

名录格式示例（也可直接参考 `data/known_stores.example.json`）：

```json
{
  "stores": [
    {
      "name": "某某小卖部",
      "aliases": ["某某小卖部", "某某小卖部旗舰店", "某某店"]
    }
  ]
}
```

## 输出文件

### 1) CSV 账本：`data/ledger.csv`（可由 `data_path` 覆盖）

字段：

- `timestamp`
- `order_id`
- `item`
- `amount`
- `raw_message`
- `message_hash`
- `message_captured_at`
- `recorded_at`

### 2) JSON 账单明细：`data/ledger_details.json`（可由 `json_output_path` 覆盖）

按账单聚合，包含：

- `order_id`
- `name`（若未提供名字则为空字符串）
- `message_hash`
- `message_captured_at`
- `recorded_at`
- `source_id`
- `raw_message`
- `items`
- `item_count`
- `total_amount`

### 3) 日结状态：`data/daily_settlement_state.json`

记录是否已初始化、每天已处理消息 ID 等状态信息。

### 4) 抓取日志：`logs/capture_YYYYMMDD_HHMMSS.log`

用于排查抓取、过滤、解析过程与拒绝原因统计。

### 5) 复核队列：`data/review_queue.jsonl`

按 JSONL 逐行记录接入失败项（如空消息、格式不合法、解析失败、异常等），便于后续人工复核或回放。

### 6) Dify 审计：`data/dify_ingest_audit.jsonl`

由 `dify_bridge_server.py` 写入请求/路由/结果摘要；当前实现是 JSON 对象（含 `records` 数组）并持续累积。

## 排障

### 微信窗口/控件排查

```powershell
python debug_wechat_window.py
```

作用：列出微信候选窗口类名、消息列表控件名称，并给出建议配置。

### 抓取失败原因分析

```powershell
python analyze_wechat_failure.py
```

作用：分析“找到微信但抓不到可读消息区”的常见根因（窗口最小化、权限不一致、控件树变化等）。

## 常见问题

1. 第一次运行没写入，是不是失败了？
- 不是。日结模式下首次运行是初始化基线，属于预期行为。

2. 为什么日志里出现 `start_not_standalone`？
- 说明命中块首行不是独立的 `#记账+yymmdd+两位序号`，通常是格式不完整或消息拼接污染。

3. 为什么有消息没记上？
- 未按 `#记账+yymmdd+两位序号 ... 结束` 发送；
- 消息不在本次抓取的最近候选范围；
- 已被当日去重规则判定为已处理。

## 目录

```text
ZongziLedger/
├─ main.py
├─ capture_to_dify.py
├─ dify_bridge_server.py
├─ dify_receiver.py
├─ dify_prompt_and_schema.md
├─ config.yaml
├─ .env.example
├─ setup.py
├─ run_zongziledger.bat
├─ run_dify_bridge.bat
├─ run_capture_to_dify.bat
├─ debug_wechat_window.py
├─ analyze_wechat_failure.py
├─ README.md
├─ core/
│  ├─ constants.py
│  ├─ parser.py
│  ├─ store_lookup.py
│  ├─ monitor.py
│  ├─ monitor_win32.py
│  └─ monitor_win32_memory.py
├─ data/
│  ├─ known_stores.example.json
│  ├─ ledger*.csv
│  ├─ ledger_details*.json
│  ├─ message_hash_state*.json
│  ├─ daily_settlement_state*.json
│  ├─ review_queue*.jsonl
│  └─ dify_ingest_audit*.jsonl
└─ logs/
```
