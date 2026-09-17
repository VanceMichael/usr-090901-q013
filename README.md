# 具身智能实验资源排程服务

纯后台的实验资源（机器人设备 / 实验区域 / 指导员时段）排程服务。多组同时申请时，
协调员在不打断维护窗口、不违反设备授权与准备/充电间隔的前提下，得到可执行排程。

- **运行时零第三方依赖**：仅使用 Python 3.12 标准库（`http.server`、`zoneinfo`、`json`）
- **规则唯一来源**：`fixtures/rules.json`，启动时加载；不连接数据库、真实机器人或校园系统
- **时区固定**：`Asia/Shanghai`（+08:00），预约不得跨上海日历日午夜
- **确定性分配顺序**（来自夹具 `allocation_order`）：优先级降序 → 开始时间升序 → 请求编号升序

## 目录结构

```
app/                     服务源码（stdlib only）
  timeutil.py            Asia/Shanghai 时间戳解析、跨午夜校验
  rules.py               规则夹具加载（public_view 剔除 internal_notes）
  validator.py           信封与单条草案校验（镜像 JSON Schema 契约）
  engine.py              贪心分配引擎、决策、时间线快照
  server.py / __main__.py  HTTP API 与入口
contracts/               请求 JSON Schema 契约（保持原样）
fixtures/                规则夹具（rules.json 保持原样；rules-extended.json 供容量自测）
scripts/http_selftest.py 容器内黑盒 HTTP 自测（33 项断言）
tests/                   72 个 pytest 自动化测试
scaffold/validate.py     原始脚手架校验器（保持原样）
Dockerfile compose.yaml  镜像、健康检查与容器自测编排
```

## 本地运行

需要 Python 3.12（容器内自带完整 tzdata）。

```sh
python3 -m app                                  # 默认 :8080，fixtures/rules.json
PORT=8080 RULES_PATH=fixtures/rules.json python3 -m app
```

### 自动化测试

```sh
pip install -r requirements-dev.txt            # pytest + jsonschema（仅测试用）
python3 -m pytest -q
```

## 从镜像重建的容器 HTTP 自测

```sh
# 1) 干净重建镜像并启动服务（含 /healthz 健康检查）
docker compose build --no-cache scheduler scheduler-extended
docker compose up -d scheduler scheduler-extended
docker compose ps                              # 等待 scheduler 状态为 healthy

# 2) 在容器网络内跑黑盒 HTTP 自测（结束自动退出）
docker compose --profile selftest up \
  --abort-on-container-exit --exit-code-from self-test self-test
# 期望结尾：checks: 33, failures: 0 / ALL HTTP SELF-TESTS PASSED

# 3) 容器内运行 pytest 全套
docker compose --profile test run --rm tests

# 4) 原始脚手架契约自检（保持可用）
docker compose run --rm --no-deps scaffold-check
```

`self-test` 服务通过 `depends_on: condition: service_healthy` 等两个排程服务
探活通过后才启动，因此无需任何手工 sleep。全部交互均为容器内网 HTTP，不触达
真实硬件或外部系统。

## API

所有请求/响应均为 JSON；时间字段必须是显式偏移量的 RFC 3339，且偏移量为 `+08:00`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 存活探活 |
| GET | `/v1/rules` | 当前规则（**绝不包含** `internal_notes`） |
| POST | `/v1/schedules` | 单个/批量预约草案的分配 |
| GET | `/v1/schedules/{schedule_id}/timeline` | 该批次全部资源时间线（可加 `resource_type`/`resource_id` 过滤） |
| GET | `/v1/resources/{device\|zone\|operator}/{id}/timeline?schedule_id=..` | 单资源时间线 |
| POST | `/v1/requests/explain` | 单预约冲突解释（可用已存批次或内联 context） |

### 决策语义

| decision | 含义 |
| --- | --- |
| `allocated` | 设备、区域空位、指导员全部锁定 |
| `conflict` | 请求时刻硬冲突：`MAINTENANCE_WINDOW` / `DEVICE_OVERLAP` / `ZONE_CAPACITY` / `OPERATOR_OVERLAP` |
| `deferred` | 活动本身不重叠，但准备/充电间隔 `TURNAROUND_REQUIRED` 无法满足；按 `next_available_at` 或更晚重试即可 |
| `rejected` | 草案结构性非法（未知设备、跨午夜、非法时区等），不影响同批其他草案 |

每条非分配结果都带：`conflict_code`、`conflicting_resources`（形如 `device:ARM-07`）、
`blocking_request_ids`、逐资源 `blocked_by` 详情与 `next_available_at`。
批量响应的 `results` 严格保持输入顺序（含 `index`），并附 `summary` 计数。

### 时间模型

- 活动占用半开区间 `[starts_at, ends_at)`；首尾相接（上一场结束 == 下一场开始）不算冲突。
- 设备/指导员在活动结束后还被尾部区间占用：
  `ends_at + max(turnaround_minutes, charging_minutes_after)`。
- 区域空位仅按活动占用；设备尾部通过设备/指导员约束自然阻止下一组进入。
- 维护窗口全程占用设备；活动尾部同样不得侵入维护窗口。

### 结构化错误

信封级错误返回 HTTP 4xx，体形如 `{"error": {"code": ..., "message": ..., "field": ...}}`，
错误码包括 `BAD_JSON`、`BAD_TIMEZONE`、`BAD_TIMESTAMP`、`TIMEZONE_MISMATCH`、
`END_NOT_AFTER_START`、`CROSSES_MIDNIGHT`、`UNKNOWN_DEVICE`、`UNKNOWN_ZONE`、
`UNKNOWN_OPERATOR`、`DEVICE_ZONE_INCOMPATIBLE`、`OPERATOR_NOT_AUTHORIZED` 等。
批量中单条草案的错误在 HTTP 200 响应内以 `decision: rejected` 逐项报告。

### 示例

```sh
curl -s localhost:8080/v1/schedules -H 'Content-Type: application/json' -d '{
  "schedule_id": "lab-schedule-demo0001",
  "timezone": "Asia/Shanghai",
  "requests": [{
    "request_id": "REQ-DEMO-01",
    "device_id": "ARM-07", "zone_id": "ZONE-C", "operator_id": "OP-101",
    "starts_at": "2026-09-09T09:00:00+08:00",
    "ends_at":   "2026-09-09T10:00:00+08:00",
    "priority": 4, "charging_minutes_after": 20
  }]
}'
```

## 已验证场景

容量并发与饱和、维护抢占（含尾部侵入）、准备/充电间隔与下一可用时刻、
优先级三级平局裁决、跨午夜拒绝与次日零点开始、混合批错误隔离，
以及规则不泄露内部备注 —— 由 `tests/`（72 项）与容器黑盒自测（33 项）共同覆盖。
