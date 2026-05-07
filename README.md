# ZongziLedger

ZongziLedger 是一个本地 Windows 记账脚本：从微信窗口抓取记账消息，解析后写入 CSV，并同步输出 JSON 账单明细。

当前默认抓取后端：`win32_memory`（进程内存扫描）。

## 新增内容速览（2026-05-07）

- 强制业务格式升级为 `#记账 + yymmdd + 两位序号 + 明细 + 结束`。
- 默认启用日结模式：首次运行仅初始化基线，不入账；后续运行只入账当日新增。
- 新增按消息块质量过滤（如 `start_not_standalone`、`too_long` 等），降低误抓。
- 新增 JSON 账单明细输出（`data/ledger_details.json`）。
- 增加一键启动脚本：`run_zongziledger.bat`（自动建虚拟环境、补依赖并运行）。
- 增加排障脚本：`debug_wechat_window.py`、`analyze_wechat_failure.py`。

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
3. 查看 `data/ledger.csv`、`data/ledger_details.json`、`logs/capture_YYYYMMDD_HHMMSS.log`。

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

建议：

- 上传仓库时保留 `config.yaml` 与 `.env.example`；
- 本地机器使用 `.env`；
- `.env` 已加入 `.gitignore`，避免误提交。

## 关键配置（config.yaml）

```yaml
prefix: "#记账"
record_start_marker: "记账"
record_end_marker: "结束"

capture_mode: "auto"
capture_backend: "win32_memory"

capture_scope: "all"
deduplicate_within_run: false

daily_settlement_mode: true
daily_settlement_state_path: "data/daily_settlement_state.json"
daily_recent_message_limit: 3
daily_seen_retention_days: 30
daily_force_reinitialize: false

order_id_required: true
order_id_digits: 8
order_id_require_hash: true

message_hash_dedupe_enabled: true
message_hash_window_seconds: 120
message_hash_state_path: "data/message_hash_state.json"

json_output_enabled: true
json_output_path: "data/ledger_details.json"
json_output_append_history: true

data_path: "data/ledger.csv"
log_dir: "logs"
max_messages: 30
```

配置说明：

- `capture_mode`：`auto` 自动抓取；`manual` 手动粘贴记账块（输入 `END` 结束）。
- `capture_backend`：`win32_memory` / `win32_clipboard` / `uia`。
- `capture_scope`：
- `all`：不过滤当天已见消息。
- `today_new`：同一天内同一原始消息只处理一次（状态文件：`data/seen_messages_today.json`）。
- `daily_recent_message_limit`：日结时仅处理最近 N 条候选消息，减少历史残留干扰。
- `message_hash_dedupe_enabled`：仅在非日结模式下按 hash+时间窗去重。
- `daily_settlement_mode=true` 时，`daily_recent_message_limit` 会优先生效，通常不再依赖 `message_hash_dedupe_enabled`。

## 输出文件

### 1) CSV 账本：`data/ledger.csv`

字段：

- `timestamp`
- `order_id`
- `item`
- `amount`
- `raw_message`
- `message_hash`
- `message_captured_at`
- `recorded_at`

### 2) JSON 账单明细：`data/ledger_details.json`

按账单聚合，包含：

- `order_id`
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
zheyizhangdan/
├─ main.py
├─ config.yaml
├─ setup.py
├─ run_zongziledger.bat
├─ debug_wechat_window.py
├─ analyze_wechat_failure.py
├─ README.md
├─ core/
│  ├─ parser.py
│  ├─ monitor.py
│  ├─ monitor_win32.py
│  └─ monitor_win32_memory.py
├─ data/
│  ├─ ledger.csv
│  ├─ ledger_details.json
│  ├─ message_hash_state.json
│  └─ daily_settlement_state.json
└─ logs/
```
