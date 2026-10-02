# 跨机构健康数据访问网关

基于 FastAPI 的本地服务，用内存存储模拟机构、患者、同意声明与访问请求，
提供主体登记、患者同意管理与历史、访问判定与审计、按时间点回溯复核、
去标识化发布等能力。

## 主体登记

起服务后只靠接口即可把基础数据建起来，链路无需预置数据：

| 操作 | 接口 |
| --- | --- |
| 登记机构 | `POST /institutions` `{institution_id, name}` |
| 查询机构 | `GET /institutions`、`GET /institutions/{id}` |
| 登记患者 | `POST /patients` `{patient_id, name, birth_year, diagnosis?, lab_group?}` |
| 查询患者 | `GET /patients`、`GET /patients/{id}` |

- 患者可携带发布统计用的分组属性：`diagnosis`、`lab_group`（出生年代由
  `birth_year` 推导），登记完即可查到。
- 同一 ID **重复登记内容相同**：幂等成功（201，响应 `created=false`），
  不会产生两条互相打架的记录；**内容冲突**：`409 *_id_conflict`，旧记录保持不变。
- **写入即校验引用**：建立同意声明时引用了没登记的患者或机构，请求当场失败
  （不落任何脏数据），并能分清是哪边不存在：
  - `422 {"detail": "patient_not_found"}`
  - `422 {"detail": "institution_not_found"}`
- 提交访问请求时主体仍未登记，沿用既有可区分的拒绝结论
  （`patient_not_found` / `recipient_not_found`，见下）。

```bash
curl -s -X POST localhost:8000/institutions -H 'Content-Type: application/json' \
  -d '{"institution_id":"hosp-a","name":"医院A"}'
curl -s -X POST localhost:8000/patients -H 'Content-Type: application/json' \
  -d '{"patient_id":"p1","name":"张三","birth_year":1980,"diagnosis":"flu","lab_group":"cohort-a"}'
```

## 同意模型与历史

`ConsentStatement`（`gateway/models.py`）覆盖四个授权维度：

| 维度 | 字段 | 说明 |
| --- | --- | --- |
| 用途 | `purposes` | treatment / research / public_health / billing |
| 数据类别 | `categories` | demographics / diagnosis / lab_result / medication / genomic |
| 接收方 | `recipients` | 允许接收数据的机构 ID 集合 |
| 有效期 | `valid_from` / `valid_until` | 区间外一律无效 |

- 同一患者可同时存在多条部分重叠的声明；任一**有效**声明完整覆盖请求即可放行。
- 声明支持建立（`POST /consents`，201；同一 `consent_id` 重复建立返回
  `409 consent_id_conflict`）、变更（`PATCH /consents/{id}`，版本号递增）、
  撤回（`POST /consents/{id}/withdraw`）。已撤回声明不可再变更；
  重复/并发撤回是幂等的，只留下一条 `withdrawn` 历史。
- 过期或已撤回的声明不得作为放行依据。

### 历史与版本

每次建立、变更、撤回都会追加一条**不可变历史**（版本、动作、改后完整快照、
变更时间），可通过 `GET /consents/{id}/history` 翻出来，看得出
“什么时候、改成了什么、版本和状态怎么走”：

```bash
curl -s localhost:8000/consents/c1/history
# [{"version":1,"action":"created","changed_at":"...","statement":{...}},
#  {"version":2,"action":"amended", ...},
#  {"version":3,"action":"withdrawn", ...}]
```

所有读-改-写都在同一把锁内完成，同一主体同一条声明并发登记、变更、撤回时
不会出现重复记录、版本错乱或历史丢失。

## 访问判定、审计与按时间点回溯

`DecisionService.evaluate`（`gateway/decision.py`）：

- **可区分拒绝原因**：`no_consent`、`consent_expired`、`consent_withdrawn`、
  `purpose_not_covered`、`category_not_covered`、`recipient_not_covered`、
  `patient_not_found`、`recipient_not_found`。
- **幂等**：`request_id` 由调用方提供。同一 `request_id` 重复或并发提交，
  返回首次判定，不重复放行、不重复计数、不产生重复审计。
- **稳定性**：判定在存储锁内完成「幂等检查 → 历史快照 → 按提交时刻还原版本 →
  判定 → 落审计」，声明在处理期间被并发变更不影响本次结论；判定依据
  （命中声明的 ID 与版本）记录在 `Decision.basis` / `consent_versions` 中，
  放行与拒绝均留痕可解释。
- **判定时刻**：以请求的 `submitted_at` 为准，先按同意历史还原那一刻的声明
  版本再匹配，因此与下面的时间点回溯共用同一套口径，事后复核能对得上账。

### 按时间点回溯判定（事后复核）

`DecisionService.replay` / `POST /access-requests/{request_id}/replay`
（body：`{"as_of": "<ISO 时间>"}`）拿一条**已经提交过**的访问请求，
按指定时间点从同意历史还原当时的声明版本与状态，再重判一遍：

- 只依据 `changed_at <= as_of` 的历史版本：**已经撤回、当时还没生效、
  已经过期、当时尚未建立**的声明都不会被当作有效放行依据。
- 返回重判结论、命中的声明版本（`consent_versions`）、逐条声明的还原依据
  （`evidence[].reason` 取值 `used / used_not_matching / withdrawn /
  not_yet_effective / expired / not_created`），并给出 `matches_original`
  表示与该请求当时真实判定的结论是否一致。
- 复核的是同一条历史请求（要素取自原审计记录）；哪怕声明之后又被改了，
  固定时间点的回溯结论始终稳定。请求不存在返回 `404 request_not_found`。

```bash
# as_of 取该请求“当时提交的时间点”（可从 GET /audits 的 submitted_at 取回）
curl -s -X POST localhost:8000/access-requests/r1/replay \
  -H 'Content-Type: application/json' \
  -d '{"as_of":"2026-09-24T12:00:00+00:00"}'
# {"outcome":"allow","consent_versions":[{"consent_id":"c1","version":1}],
#  "matches_original":true,"evidence":[{"consent_id":"c1","version":1,
#  "status":"active","considered":true,"reason":"used"}]}
```

## 发布规则

`PublicationService`（`gateway/publication.py`）：

- **去标识化**：患者以 HMAC-SHA256（盐值来自环境变量）假名出现，
  同一患者跨发布稳定，假名不可逆推身份。
- **数据最小化**：发布结果只含分组键、计数与假名列表。
- **小样本抑制**：计数低于阈值（默认 5，可用 `GATEWAY_MIN_GROUP_SIZE` 调整）
  的分组整体省略——不返回 0 计数行、错误信息为通用文案，
  阈值或策略调整后无法通过计数、错误信息或响应差异反推被抑制个体。

## 兼容范围

本次新增能力不改变原有同意管理、判定、审计与发布的既有行为与返回语义：

- `POST /consents`、`PATCH`、`withdraw`、`POST /access-requests`、`GET /audits`、
  `POST /publications/group-counts` 的路径、成功响应字段与判定原因枚举保持不变。
- 访问请求遇到未登记主体，仍返回**可区分的拒绝结论**（不是异常）：
  `patient_not_found` / `recipient_not_found`，且审计照常留痕。
- 新增的强约束只作用在**声明写入**上：建声明引用未登记的患者/机构时
  由“先存成功、判定时才发现”改为写入当场 `422` 明确失败（新拦截，不产生脏数据）。
- 内部判定口径的变化仅为：判定时刻统一取请求的 `submitted_at` 并按历史还原
  版本；对实时提交（提交即判定）的结论与旧行为一致。

## 本地验证

```bash
uv sync                          # 安装依赖
uv run python -m pytest -q       # 运行全部测试（48 个）
uv run python -m pytest -q -s    # 打印登记/写入拦截/历史/并发/回溯的业务日志
uv run fastapi dev main.py       # 启动服务（或 uv run uvicorn main:app）
```

> 注：本机若安装了控制台脚本版 `pytest`，其可能误收集上级目录的同名工程
> conftest，请使用 `uv run python -m pytest` 入口（本仓库测试按此验证）。

起服务后，下面这条脚本把「登记 → 建声明（含脏数据拦截）→ 判定 →
历史 → 变更/撤回 → 按时间点回溯」完整走一遍，照着跑应全部通过
（时间点由脚本按当前时间动态生成，无需手工对齐）：

```bash
BASE=http://127.0.0.1:8000
jqf() { python3 -c "import sys,json;d=json.load(sys.stdin);print($1)"; }
# ISO 时间戳助手：now-iso / iso-at <±天数>
now_iso() { python3 -c "from datetime import datetime,timezone;print(datetime.now(timezone.utc).isoformat())"; }
iso_at()  { python3 -c "from datetime import datetime,timedelta,timezone;print((datetime.now(timezone.utc)+timedelta(days=$1)).isoformat())"; }

# 1) 主体登记（仅接口即可建基础数据）
curl -s -X POST $BASE/institutions -H 'Content-Type: application/json' \
  -d '{"institution_id":"hosp-a","name":"医院A"}' | jqf 'd["created"]'        # true
curl -s -X POST $BASE/patients -H 'Content-Type: application/json' \
  -d '{"patient_id":"p1","name":"张三","birth_year":1980,"diagnosis":"flu","lab_group":"cohort-a"}' \
  | jqf 'd["created"]'                                                        # true
# 同内容重复登记幂等
curl -s -X POST $BASE/institutions -H 'Content-Type: application/json' \
  -d '{"institution_id":"hosp-a","name":"医院A"}' | jqf 'd["created"]'        # false

# 2) 脏引用在写入那一刻被挡住，并能分清患者还是机构
curl -s -o /dev/null -w '%{http_code}\n' -X POST $BASE/consents \
  -H 'Content-Type: application/json' \
  -d "{\"consent_id\":\"cx\",\"patient_id\":\"ghost\",\"purposes\":[\"treatment\"],
    \"categories\":[\"diagnosis\"],\"recipients\":[\"hosp-a\"],
    \"valid_from\":\"$(iso_at -30)\",\"valid_until\":\"$(iso_at 30)\"}"        # 422
curl -s -X POST $BASE/consents -H 'Content-Type: application/json' \
  -d "{\"consent_id\":\"cx\",\"patient_id\":\"ghost\",\"purposes\":[\"treatment\"],
    \"categories\":[\"diagnosis\"],\"recipients\":[\"hosp-a\"],
    \"valid_from\":\"$(iso_at -30)\",\"valid_until\":\"$(iso_at 30)\"}" \
  | jqf 'd["detail"]'                                                         # patient_not_found

# 3) 建合法声明 -> 提交请求放行
curl -s -X POST $BASE/consents -H 'Content-Type: application/json' \
  -d "{\"consent_id\":\"c1\",\"patient_id\":\"p1\",\"purposes\":[\"treatment\"],
    \"categories\":[\"diagnosis\"],\"recipients\":[\"hosp-a\"],
    \"valid_from\":\"$(iso_at -30)\",\"valid_until\":\"$(iso_at 30)\"}" >/dev/null
curl -s -X POST $BASE/access-requests -H 'Content-Type: application/json' -d '{
    "request_id":"r1","patient_id":"p1","recipient_id":"hosp-a",
    "purpose":"treatment","categories":["diagnosis"]}' | jqf 'd["allowed"]'    # true

# 4) 历史可见：建立 / 变更 / 撤回
curl -s -X PATCH $BASE/consents/c1 -H 'Content-Type: application/json' \
  -d '{"categories":["diagnosis","lab_result"]}' >/dev/null
curl -s -X POST $BASE/consents/c1/withdraw >/dev/null
curl -s $BASE/consents/c1/history | jqf '[ (h["version"],h["action"]) for h in d]'
# [(1, 'created'), (2, 'amended'), (3, 'withdrawn')]

# 5) 按请求“当时提交的时间点”回溯（从审计取回 submitted_at）：
#    即使声明事后已撤回，该时刻仍还原为 v1 放行，与原判定一致
SUB=$(curl -s $BASE/audits \
  | jqf '[a for a in d if a["request_id"]=="r1"][0]["submitted_at"]')
curl -s -X POST $BASE/access-requests/r1/replay -H 'Content-Type: application/json' \
  -d "{\"as_of\":\"$SUB\"}" \
  | jqf '(d["outcome"], d["consent_versions"], d["matches_original"])'
# ('allow', [{'consent_id': 'c1', 'version': 1}], True)
```

测试覆盖：主体登记与冲突、写入时患者/机构引用校验、同意历史建立/变更/撤回、
按时间点回溯（撤回/未生效/过期/未建立均被排除、与原判定对账、之后再改结论稳定）、
并发登记/变更/撤回与判定交错（无重复、版本连续、历史与审计不丢失）、
同意撤回与并发判定、越权访问（各拒绝原因可区分）、小样本阈值边界、
重复/并发提交幂等、声明处理期变更的结论稳定性。

环境变量：`GATEWAY_PSEUDONYM_SALT`（假名盐值，默认 dev-only）、
`GATEWAY_MIN_GROUP_SIZE`（抑制阈值，默认 5）。
