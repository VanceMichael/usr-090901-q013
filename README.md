# 具身智能实验资源排程服务

具身智能教学实验室多小组并发预约机器人、实验区与指导员时段的纯后台排程服务。
Python 3.12 + 标准库 `http.server` + JSON Schema（Draft 2020-12），规则只从
`fixtures/rules.json` 读取，不连接真实机器人或校园系统。

## 能力

- 单个 / 批量预约草案排程，批量结果严格保持输入顺序，坏数据隔离不影响其他合法预约。
- 校验：`Asia/Shanghai` 时间戳（必须显式 `+08:00`；Zulu、其他偏移、裸时间均拒绝）、
  结束严格晚于开始、禁止跨午夜、设备与区域兼容、指导员对设备授权。
- 分配顺序固定来自 fixtures：`priority_desc → starts_at_asc → request_id_asc`。
- 资源：设备（独占）、区域（容量计数）、指导员（独占）；维护窗口不可打断；
  相邻预约之间强制 `max(turnaround_minutes, charging_minutes_after)` 准备/充电间隔。
- 决策：`allocated` / `deferred`（被已排预约或间隔阻塞）/ `conflict`（维护窗口）/ `error`（校验失败），
  返回具体冲突资源、阻塞预约与三类资源同时空闲的下一可用时段。
- 查询接口：规则（脱敏 `internal_notes`）、某资源时间线、单预约冲突解释。
- 全部错误为稳定结构化 JSON：`{"error": {"code", "message", "details"}}`。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 探活 |
| GET | `/rules` | 当前规则（仅 fixtures 内容，内部备注不外泄） |
| POST | `/schedules` | 单个排程，请求体符合 `contracts/request.schema.json` |
| POST | `/schedules/batch` | 批量排程，请求体 `{"schedules": [...]}`（1..50） |
| GET | `/schedules/{schedule_id}` | 取排程结果 |
| GET | `/schedules/{id}/timeline?resource_type=device\|zone\|operator&resource_id=...` | 资源时间线 |
| GET | `/schedules/{id}/requests/{request_id}` | 单预约冲突解释 |

冲突码：`DEVICE_OVERLAP`、`ZONE_CAPACITY`、`OPERATOR_OVERLAP`、`MAINTENANCE_WINDOW`、`TURNAROUND_REQUIRED`。
错误码还包括 `UNKNOWN_DEVICE`、`UNKNOWN_ZONE`、`UNKNOWN_OPERATOR`、`DEVICE_ZONE_INCOMPATIBLE`、
`OPERATOR_NOT_AUTHORIZED`、`INVALID_TIMEZONE`、`INVALID_TIMESTAMP`、`INVALID_INTERVAL`、
`CROSSES_MIDNIGHT`、`SCHEMA_VIOLATION`、`INVALID_BODY` 等。

## 本地运行与测试

```sh
pip install -r requirements-dev.txt
python -m pytest -q          # 36 个自动化测试
python -m app --port 8080    # 启动服务
```

## 从镜像重建开始的容器自测

```sh
# 1) 干净重建镜像（多阶段构建中会先跑完全部 pytest，测试失败则镜像构建失败）
docker compose build --no-cache api selftest

# 2) 启动并等待 /healthz 健康
docker compose up -d api
docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q api)"   # -> healthy

# 3) 容器内 HTTP 黑盒自测（仅标准库，覆盖下列六类场景），退出码即结论
docker compose run --rm selftest http://api:8080

# 一键：健康条件满足后自动运行自测
docker compose up --build \
  && docker compose run --rm selftest http://api:8080

# 4) 基线夹具契约自检（scaffold 保留）
docker compose run --rm --no-deps scaffold-check
```

`tests/selftest.py` 的 24 项断言覆盖：资源容量、维护抢占、准备/充电间隔、
优先级三级平局、跨午夜排程、混合批错误隔离，以及时间线、冲突解释、规则脱敏与非法时区。

## 布局

```
app/            排程服务（timeutil / rules / engine / service / __main__）
contracts/      请求 JSON Schema 契约（保持不变）
fixtures/       规则与示例（规则唯一数据来源；internal_notes 不对外）
scaffold/       基线夹具校验（保持可用）
tests/          pytest 套件 + 容器黑盒 selftest.py
Dockerfile      多阶段：test 阶段跑 pytest，runtime 阶段非 root 运行、自带 HEALTHCHECK
Dockerfile.selftest  黑盒自测镜像
compose.yaml    api（探活）、selftest（依赖 healthy）、scaffold-check
```

## 语义约定

- 所有区间按半开 `[start, end)` 处理，首尾相接不算重叠，因此前约缓冲恰好在后约开始时
  结束是合法的；缓冲侵入维护窗口则判 `conflict`。
- 维护重叠的预约判 `conflict`（设备固有不可用，协调员必须改期）；
  仅被其他预约/容量/间隔阻塞的判 `deferred`（可稍后再试）。
- 排程状态保存在内存中，服务重启即清空；本服务不做持久化与外部系统对接。
