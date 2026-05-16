# ZongziLedger

## 使用流程（精简版）

本项目当前只保留这条链路：

`wetrace -> Dify Workflow -> 本地记账落盘`

不再使用 UIA / WinAPI 抓取流程。

## 1. 准备 wetrace

先按 wetrace 项目要求完成部署与启动，确保可访问其 HTTP API。

在本项目 `.env` 中至少配置：

- `WETRACE_BASE_URL`（例如 `http://127.0.0.1:9099`）
- `WETRACE_TALKER_ID` 或 `WETRACE_TALKER_KEYWORD`（二选一，推荐先用 keyword 找群）

## 2. 配置群聊强绑定（重点）

群绑定参数是：

- `WETRACE_TALKER_ID`：直接绑定目标群 ID（最稳）
- `WETRACE_TALKER_KEYWORD`：按群名关键词解析目标群

说明：这两个参数用于“选哪个群”。

另外，`WETRACE_KEYWORD` 是“消息内容过滤关键词”，不是群绑定参数。

### `WETRACE_TALKER_ID` 推荐获取方式（按我们当前流程）

1. 先在 `.env` 里填写群关键词，不先写死 ID：
   - `WETRACE_TALKER_KEYWORD=你的群名关键词`
   - `WETRACE_TALKER_ID=`（留空）
2. 运行一次：

```bat
run_ingest_wetrace.bat
```

3. 打开本次运行日志（控制台最后会打印 `[LOG] ...` 路径），查这行：
   - `[WETRACE] talker resolve keyword=..., chosen_id=..., chosen_name=...`
4. 把 `chosen_id` 回填到 `.env` 的 `WETRACE_TALKER_ID`，后续固定用 ID 绑定（最稳）。

补充：
- 现在已兼容群聊 ID 常见格式差异（例如有无 `@chatroom` 后缀），不会因为这种格式差异被误判 `talker_mismatch`。
- `WETRACE_TALKER_KEYWORD` 适合“找群”，`WETRACE_TALKER_ID` 适合“长期稳定绑定群”。

## 3. 补全业务字典

按你的业务维护这三个本地字典：

- `dictionaries/known_stores.local.json`：店名与别名
- `dictionaries/products.local.json`：品名与别名
- `dictionaries/specs.local.json`：规格归一化（可选但推荐）

字典强绑定说明：

- 当前账单消息主要是“店名 + 内容”格式，所以本项目使用字典强绑定来做稳定识别（优先依赖店名字典，再结合品名/规格字典）。
- 这是一套确定性规则，不依赖临场猜测，目的是减少误识别。
- 如果后续你有新的识别规则需求（例如新的消息模板、额外字段、不同结算口径），目前这部分还没实现，需要后续再扩展。

硬性要求（必须先做）：

- 在运行任何 BAT 前，必须先维护好本地字典（至少先维护 `known_stores.local.json` 和 `products.local.json`）。
- 如果字典没维护或内容不完整，流程会出现“无法命中店名/品名、无可入账结果、被跳过”等情况，表现为看起来“程序在跑但不入账”。
- 建议先用少量真实样本把字典补齐，再跑日常流程。

## 4. 部署 Dify Workflow 并配置 .env

把你的工作流部署到 Dify 后，在 `.env` 填入：

- `DIFY_REMOTE_API_URL`
- `DIFY_REMOTE_API_KEY`

并确认：

- `WETRACE_DISPATCH_MODE=dify`
- `DIFY_REMOTE_ENABLED=true`
- `DIFY_REMOTE_PRIORITY=true`
- `DIFY_REMOTE_FALLBACK_LOCAL=false`（远端失败即失败，避免本地兜底导致口径漂移）

## 5. 运行入口

推荐直接运行：

```bat
run_wetrace_daily_once.bat
```

首次运行说明（重要）：

- 第一次运行属于初始化阶段，只会建立/同步 wetrace 游标状态，不会记录任何账单数据到 `runtime_local/ledger.local.csv`。
- 从第二次运行开始，才会基于增量消息正常入账。

该脚本会：

1. 启动本地 `dify_bridge_server.py`
2. 执行一次 `ingest_wetrace_local.py`
3. 结束后自动关闭 bridge 进程

## 常用脚本

- `run_wetrace_daily_once.bat`：一键单次跑完整链路（推荐）
- `run_dify_bridge.bat`：仅启动 bridge
- `run_ingest_wetrace.bat`：仅跑 wetrace ingest（要求 bridge 已可用）
- `run_ingest_images.bat`：本地图像 OCR 入账入口（当前仅保留预留能力）

## 当前未做完与预留项

- 图片识别链路你这版暂未正式启用/验收；仓库里保留了相关预留代码，便于后续继续接：
- `ingest_images_local.py`
- `core/vision_ingest.py`
- `core/vision_ocr.py`
- `run_ingest_images.bat`

## 目录结构说明

```text
.
├─ core/                     # 核心处理逻辑（解析、字典匹配、wetrace 客户端、输出）
├─ dictionaries/             # 业务字典（店名/品名/规格）；example 为模板，local 为本地实际配置
├─ runtime_local/            # 本地运行态输出与状态（验收账单、去重状态、wetrace 游标）
├─ data/                     # 审计与示例数据等辅助数据文件
├─ .env.example              # 环境变量模板
├─ config.yaml               # 默认配置（可被 .env 覆盖）
├─ ingest_wetrace_local.py   # wetrace 消息拉取 + 投递流程入口
├─ dify_bridge_server.py     # 本地 bridge 服务（承接 ingest 并转处理）
├─ dify_receiver.py          # Dify 返回结果接收与处理
├─ main.py                   # 本地记账主处理逻辑
├─ run_wetrace_daily_once.bat# 推荐一键入口（bridge + ingest）
├─ run_dify_bridge.bat       # 仅启动 bridge
├─ run_ingest_wetrace.bat    # 仅跑 wetrace ingest
└─ run_ingest_images.bat     # 图片识别预留入口
```

## 最小自检清单

运行后重点看：

- 账单验收以 CSV 为准：`runtime_local/ledger.local.csv`
- 验收目录：`runtime_local/`
- `runtime_local/ledger.local.csv` 是否有新增
- `runtime_local/message_hash_state.local.json` 是否更新
- `runtime_local/wetrace_state.*.local.json` 是否推进游标
- `data/dify_ingest_audit.local.jsonl`（或你配置的审计路径）是否有调用记录

## 常见问题（FAQ）

1. 为什么不进账？

- 先看字典有没有维护：`dictionaries/known_stores.local.json`、`dictionaries/products.local.json`。
- 这两个字典是前置条件，未维护或内容不全时，最常见表现就是“不进账 / 被跳过”。
- 再看控制台最后的 `[LOG] ...` 路径，确认是“识别不到”还是“流程异常”。

2. 为什么第一次运行没有任何账单写入？

- 这是正常行为。第一次运行是初始化游标，只同步 `wetrace_state`，不写 `runtime_local/ledger.local.csv`。
- 从第二次运行开始才按增量消息入账。

3. 为什么提示群不匹配或一直抓不到目标群消息？

- 优先用 `WETRACE_TALKER_KEYWORD` 先跑一次，拿到日志里的 `chosen_id`。
- 把 `chosen_id` 回填到 `WETRACE_TALKER_ID` 后固定使用 ID 绑定。
- `WETRACE_KEYWORD` 是消息内容过滤，不是群绑定参数，别混用。

4. 为什么 daily-once 提示 bridge 健康检查失败？

- `run_wetrace_daily_once.bat` 依赖本地 bridge 健康检查地址：`http://127.0.0.1:8787/api/health`。
- 先单独运行 `run_dify_bridge.bat`，确认 bridge 进程已启动，再重试 daily-once。
- 若仍失败，重点检查 `.env` 中 Dify 相关配置和本机端口占用。

5. 为什么显示“成功”但账单文件没新增？

- 先看验收口径文件：`runtime_local/ledger.local.csv`（以它为准）。
- 再看 `runtime_local/message_hash_state.local.json` 是否更新，排除被去重跳过。
- 同时查看 `data/dify_ingest_audit.local.jsonl` 是否有调用记录，确认请求是否真正走到了 ingest。

6. 报错 502 是什么问题？

- 当前链路里，`502` 通常是 Dify 侧返回异常或返回体不符合预期导致，不是本地 CSV 写盘本身的问题。
- 先检查 Dify 工作流是否可正常运行、API Key/URL 是否有效、工作流输出字段是否符合你当前接入格式。
