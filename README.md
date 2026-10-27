# Context Lens

把本机各个 AI Agent 的会话日志汇总成一个看板：**一个会话的上下文被什么吃掉了、花了多少 token、调了哪些工具**。

跨宿主、可筛选、能实时刷新。纯本地运行，只读日志，不联网、不上传。

```
┌──────────────────────────────────────────────────────────┐
│ 会话 764   计费 token 2.9B   成本 ¥220   缓存命中 95.6%   │
├──────────────────────────────────────────────────────────┤
│ 宿主: [全部] [WorkBuddy] [WorkBuddy AI] [Qwen Code] ...  │
│ 工作区 ▾   时间 ▾   搜索…   排序 ▾                       │
├───────────────────────────┬──────────────────────────────┤
│  每日用量（按宿主堆叠）    │  宿主分布                    │
├───────────────────────────┴──────────────────────────────┤
│  会话清单（点一行进入上下文透视）                         │
└──────────────────────────────────────────────────────────┘
```

## 支持的宿主

| Agent | 数据来源 | token 口径 |
|---|---|---|
| **WorkBuddy** 5.x | `~/.workbuddy/projects/*/*.jsonl` + `workbuddy.db` | 实报 |
| **WorkBuddy AI** 5.8 | `~/.workbuddy-ai/projects/*/*.jsonl` + `workbuddy.db` | 实报 |
| **CodeBuddy CLI** | `~/.codebuddy/projects/*/*.jsonl` | 实报 |
| **Qwen Code** | `~/.qwen/projects/*/chats/*.jsonl` | 实报 |
| **千问办公** | `AppData/Roaming/QwenWorkCN/data/agents.db`（SQLite） | 估算 |
| **豆包工作** | `.doubaowork/.../.sessions/*/agents/*/system/trajectory.jsonl` | 估算 + 上下文构成实报 |
| **Claude Code** | `~/.claude/projects/*/*.jsonl` | 实报 |
| **Codex CLI** | `~/.codex/sessions/**/rollout-*.jsonl` | 实报 |

> 「实报」= 日志里有 `usage`（input / output / cache_read）；「估算」= 宿主不上报，按字符密度推算（CJK ≈ 1 token/字，其它 ≈ 3.5 字符/token），并以实报 input 归一。

## 快速开始

```bash
python serve.py        # 实时模式，自动打开 http://127.0.0.1:8765
python collect.py      # 只生成静态快照 data.js，用浏览器直接打开 index.html 也能看
```

零依赖，Python 3.8+ 即可（`leveldb.py` 是自己实现的，不需要编译）。

首次扫描 700+ 会话约 30 秒，之后走增量缓存，只重新解析变过的文件。

## 三个页签

**总览** — 跨宿主汇总。6 个 KPI、每日用量（按宿主堆叠）、宿主分布、会话清单。
筛选：宿主多选 / 工作区 / 时间范围 / 关键词 / 排序，四处联动。点任意一行进入该会话。

**上下文** — 单个会话的透视：

- 输入输出、缓存命中、成本、输出速度
- **上下文构成**：宿主注入（系统提示词 + 工具定义）/ 用户消息 / 助手消息 / 模型思考 / 工具参数 / 工具结果
- 逐次请求的上下文趋势（累计 / 增量两种视图）
- 上下文浏览器：哪些消息、哪个工具结果最占地方
- 耗时拆解：模型耗时 / 工具耗时 / 空档

**编队** — 同一工作区的会话关系（本机日志里没有子 Agent 谱系事件，所以按工作区聚合，UI 里已标注）。

## 它是怎么算的

一次「步」= 一次模型调用（日志里的 `function_call`）。累计上下文 = 截至该次请求所有消息 + 工具定义的总和。

分项估算后按供应商实报的 `input_tokens` 归一，差额归给「宿主注入」——所以各分项之和永远等于实报值，不会出现比例对不上的情况。

有官方 token-meter 时优先用官方的：

- WorkBuddy 系列读 `workbuddy.db` 的 `sessions.context_window` + `session_usage.used`，窗口是**真实值**（192k / 256k / 300k / 1M 各不相同），不是统一假设。
- 豆包工作从 IndexedDB 里挖 `context_window_usage`，直接拿到官方四桶：系统提示词 / 对话消息 / 技能 / 工具定义。

## leveldb.py

纯 Python 的 LevelDB 读取器，用来读 Chrome / Electron 的 IndexedDB：

```python
import leveldb
recs = leveldb.read_db(r"...\chrome_doubaowork-chat_0.indexeddb.leveldb")
```

实现了 Snappy 块解压、SSTable（footer → index block → data block）、WAL（32KB 分块 + FULL/FIRST/MIDDLE/LAST 拼装）、多文件按 sequence 合并。不校验 CRC。约 200 行，无第三方依赖。

## 隐私

**仓库里是脱敏后的演示数据**（`make_public.py` 生成）：标题、路径、项目名、上下文预览全部替换或清空，只保留 token 量级和工具名。

真实数据在 `data.js`，已被 `.gitignore` 排除。自己跑 `python collect.py` 会重新生成。

本工具只读本地日志，不发起任何网络请求。

## 已知限制

- 每个会话最多保留 600 个请求点，超长会话的按日统计会略低（KPI 的 token 总数是全量累加，不受影响）。
- 成本是按 `collect.py` 顶部 `PRICE` 估算的，不是账单。
- 上下文窗口按各会话真实值；拿不到时退回 `CONTEXT_WINDOW` 常量。
- 千问办公的 `agent_trajectory_log.payload` 是 AES 加密的，拿不到真实用量，只能估算。
- 豆包工作的 `context_window_usage` 只在请求进行中出现，LevelDB 压实后就没了，所以要「边跑边抓」：让 `serve.py` 常驻，用一次抓一次，结果存在 `doubao_ctx_cache.json`。

## 目录

```
collect.py       采集 + 解析（宿主适配器 + 归一化 Builder）
leveldb.py       纯 Python LevelDB 读取器
serve.py         实时服务（增量解析 + gzip + 3 秒轮询）
index.html       单页看板（原生 JS + SVG，无框架无依赖）
make_public.py   生成脱敏的公开数据
```

加一个新宿主只需在 `collect.py` 的 `HOSTS` 里加一条：id / name / color / glob / parse 函数。
