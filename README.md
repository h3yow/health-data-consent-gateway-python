# 跨机构健康数据访问网关

基于 FastAPI 的本地服务，用内存存储模拟机构、患者、同意声明与访问请求，
提供主体登记、患者同意管理（含历史与回溯判定）、访问判定与审计、去标识化发布四类能力。

## 主体登记

服务启动后只用接口即可登记机构与患者，登记完即可查询：

| 接口 | 说明 |
| --- | --- |
| `POST /institutions` | 登记机构 `{institution_id, name}` |
| `GET /institutions` / `GET /institutions/{id}` | 列表 / 按 ID 查询 |
| `POST /patients` | 登记患者 `{patient_id, name, birth_year, diagnosis?, lab_group?}` |
| `GET /patients` / `GET /patients/{id}` | 列表 / 按 ID 查询 |

- 患者的 `diagnosis` / `lab_group` / `birth_year` 是发布统计用到的分组属性。
- 同一 ID 重复登记返回 **409**（`institution_already_exists` / `patient_already_exists`），
  原记录不被覆盖，不会出现两条互相打架的记录；并发登记同一 ID 恰好一个成功。
- 查询不存在的患者 / 机构返回 **404**（`patient_not_found` / `institution_not_found`）。

**写入时校验**：声明与访问请求引用未登记的主体时，在写入 / 提交那一刻就明确失败，
不会先当成功存下来：

- `POST /consents` 引用未登记患者 → **422** `patient_not_found`；
  引用未登记接收方机构 → **422** `recipient_not_found`；`consent_id` 重复 → **409**。
- `POST /access-requests` 引用未登记患者 / 机构 → **422** `patient_not_found` /
  `recipient_not_found`，且不产生判定与审计记录。

## 同意模型

`ConsentStatement`（`gateway/models.py`）覆盖四个授权维度：

| 维度 | 字段 | 说明 |
| --- | --- | --- |
| 用途 | `purposes` | treatment / research / public_health / billing |
| 数据类别 | `categories` | demographics / diagnosis / lab_result / medication / genomic |
| 接收方 | `recipients` | 允许接收数据的机构 ID 集合（须已登记） |
| 有效期 | `valid_from` / `valid_until` | 区间外一律无效 |

- 同一患者可同时存在多条部分重叠的声明；任一**有效**声明完整覆盖请求即可放行。
- 声明支持建立（`POST /consents`）、变更（`PATCH /consents/{id}`，版本号递增）、
  撤回（`POST /consents/{id}/withdraw`）。已撤回声明不可再变更。
- 过期或已撤回的声明不得作为放行依据。

## 同意历史与回溯判定

每次建立、变更、撤回都会留下一条历史（`ConsentHistoryEntry`），
包含变更类型、版本、状态、声明内容快照与变更时间：

- `GET /consents/{id}/history`：按版本顺序返回完整历史，
  看得出什么时候、改成了什么、版本和状态怎么走。
- `GET /access-requests/{request_id}/review`：事后复核。按该请求**提交时刻**
  还原当时的同意状态重判一遍，返回 `original`（当时的真实判定）、
  `retrospective`（回溯重判）与 `consistent`（两者是否一致）。

回溯规则：

- 依据声明变更历史还原 `submitted_at` 时刻每条声明的版本与状态；
  当时未建立、已撤回、尚未生效或已过期的声明都不会被当作有效依据。
- 回溯判定与实时判定走同一套逻辑，因此结论与当时真实判定对得上，
  并能从 `basis` / `consent_versions` 讲清是靠哪几条声明、哪个版本放行或拒绝的。
- 回溯是只读操作：不写判定、不落审计；之后声明再被变更也不影响回溯结论。

## 访问判定与审计

`DecisionService.evaluate`（`gateway/decision.py`）：

- **可区分拒绝原因**：`no_consent`、`consent_expired`、`consent_withdrawn`、
  `purpose_not_covered`、`category_not_covered`、`recipient_not_covered`、
  `patient_not_found`、`recipient_not_found`。
- **幂等**：`request_id` 由调用方提供。同一 `request_id` 重复或并发提交，
  返回首次判定，不重复放行、不重复计数、不产生重复审计。
- **稳定性**：判定在存储锁内完成「幂等检查 → 同意快照 → 判定 → 落审计」，
  声明在处理期间被并发变更不影响本次结论；判定依据（命中声明的 ID 与版本）
  记录在 `Decision.basis` / `consent_versions` 中，放行与拒绝均留痕可解释。
- **并发一致**：同一声明的并发变更 / 撤回在存储锁内串行化，
  版本号严格递增、历史完整不丢失；并发登记同一主体恰好一个成功。

## 发布规则

`PublicationService`（`gateway/publication.py`）：

- **去标识化**：患者以 HMAC-SHA256（盐值来自环境变量）假名出现，
  同一患者跨发布稳定，假名不可逆推身份。
- **数据最小化**：发布结果只含分组键、计数与假名列表。
- **小样本抑制**：计数低于阈值（默认 5，可用 `GATEWAY_MIN_GROUP_SIZE` 调整）
  的分组整体省略——不返回 0 计数行、错误信息为通用文案，
  阈值或策略调整后无法通过计数、错误信息或响应差异反推被抑制个体。
- 分组维度：`diagnosis` / `lab_group` / `birth_decade`，来自患者登记时的分组属性。

## 兼容范围

本版在上一版基础上新增主体登记、同意历史与回溯判定，原有能力保持兼容：

- 同意建立 / 变更 / 撤回的接口路径、字段与返回结构不变；
  判定结论、拒绝原因取值、幂等语义、审计结构、发布行为与返回语义不变。
- 服务层 `DecisionService.evaluate` 对未登记主体仍返回带
  `patient_not_found` / `recipient_not_found` 原因的 DENY 判定（供既有调用方使用）；
  HTTP 层则在提交时以 422 快速失败，不落判定与审计。
- 新增的行为差异仅限「脏数据拦截」：引用未登记主体的声明 / 请求，
  上一版会先存下来，本版在写入 / 提交时明确失败。

## 本地验证

```bash
uv sync                          # 安装依赖
uv run pytest -q                 # 运行全部测试
uv run pytest -q -s              # 打印请求上下文、判定依据、历史与审计日志
uv run uvicorn main:app &        # 启动服务（或 uv run fastapi dev main.py）
```

起服务后手工验证核心链路：

```bash
# 登记机构与患者
curl -s -X POST localhost:8000/institutions -H 'content-type: application/json' \
  -d '{"institution_id":"hosp-a","name":"医院A"}'
curl -s -X POST localhost:8000/patients -H 'content-type: application/json' \
  -d '{"patient_id":"p1","name":"张三","birth_year":1980,"diagnosis":"flu"}'

# 未登记患者建声明：写入时即失败
curl -s -X POST localhost:8000/consents -H 'content-type: application/json' \
  -d '{"consent_id":"cx","patient_id":"ghost","purposes":["treatment"],
       "categories":["diagnosis"],"recipients":["hosp-a"],
       "valid_from":"2026-01-01T00:00:00Z","valid_until":"2027-01-01T00:00:00Z"}'
# -> {"detail":"patient_not_found"}

# 正常建声明 -> 提交访问请求 -> 撤回 -> 查历史 -> 按提交时刻回溯复核
curl -s -X POST localhost:8000/consents -H 'content-type: application/json' \
  -d '{"consent_id":"c1","patient_id":"p1","purposes":["treatment"],
       "categories":["diagnosis"],"recipients":["hosp-a"],
       "valid_from":"2026-01-01T00:00:00Z","valid_until":"2027-01-01T00:00:00Z"}'
curl -s -X POST localhost:8000/access-requests -H 'content-type: application/json' \
  -d '{"request_id":"r1","patient_id":"p1","recipient_id":"hosp-a",
       "purpose":"treatment","categories":["diagnosis"]}'
curl -s -X POST localhost:8000/consents/c1/withdraw
curl -s localhost:8000/consents/c1/history
curl -s localhost:8000/access-requests/r1/review   # consistent: true
```

测试覆盖：主体登记与重复冲突、写入时校验、同意历史留痕、按时间点回溯判定、
并发登记 / 变更 / 撤回 / 提交的一致性、同意撤回与并发判定、越权访问
（各拒绝原因可区分）、小样本阈值边界、重复 / 并发提交幂等、
声明处理期变更的结论稳定性。

环境变量：`GATEWAY_PSEUDONYM_SALT`（假名盐值，默认 dev-only）、
`GATEWAY_MIN_GROUP_SIZE`（抑制阈值，默认 5）。
