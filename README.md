# Health Data Consent Gateway

跨机构健康数据访问网关。使用本地 JSON 存储模拟机构、患者与访问请求，基于 FastAPI + uv 管理依赖，本地即可完整验证。

## 模块结构

```
app/models.py    领域类型：同意声明、访问请求、判定、审计、发布报告
app/storage.py   JsonStore：线程安全的本地 JSON 持久化存储
app/consent.py   ConsentService：同意声明的建立 / 变更 / 撤回
app/gateway.py   Gateway：访问请求判定 + 审计留痕（幂等、并发安全）
app/publish.py   PublishService：数据最小化、去标识化与小样本抑制
app/api.py       FastAPI HTTP 层
main.py          应用入口
tests/           pytest 测试（判定日志打印请求上下文与判定依据）
```

## 同意模型

一条同意声明（`ConsentStatement`）覆盖四个维度：

- **用途** `purposes`：treatment / research / public_health / billing
- **数据类别** `data_categories`：demographics / diagnoses / lab_results / medications / genomics
- **接收方** `recipients`：被授权的机构标识集合
- **有效期** `valid_from` / `valid_until`（`null` 表示不限期）

同一患者可同时存在多条部分重叠的声明。声明支持：

- **建立** `POST /consents`（校验患者存在、维度非空、有效期合法）
- **变更** `POST /consents/{id}/amend`（版本号 +1，已撤回的声明不可变更）
- **撤回** `POST /consents/{id}/revoke`（幂等；记录 `revoked_at`）

过期或已撤回的声明不再作为放行依据。

## 访问判定

`POST /access-requests` 提交请求，`request_id` 即幂等键。判定规则：

- 放行：存在**单条** ACTIVE 声明，同时覆盖请求的用途、全部数据类别、接收方，且当前时间在有效期内。
- 拒绝：返回可区分的 `deny_reasons`：
  - `patient_not_found` / `no_consent`
  - `consent_revoked`（本可匹配但已撤回）
  - `consent_expired`（本可匹配但已过期/未生效）
  - `purpose_not_covered` / `category_not_covered` / `recipient_not_covered`
  - `scope_not_covered`（各维度分别被不同声明覆盖，但无单条声明覆盖整个请求范围）

**审计留痕**：放行与拒绝都会写入一条 `AuditRecord`（`GET /audits` 查询），包含请求上下文、判定结论、拒绝原因、命中的声明 id 与版本号、可读的判定依据 `basis`。

**幂等与并发**：

- 同一 `request_id` 重复或并发提交，只产生一次判定与一条审计记录，后续提交返回首次判定结果（不重复放行、不重复计数）。
- 判定时对声明做一次快照，结论只依赖该快照；即使声明在请求处理过程中被变更/撤回，最终结论仍然稳定，且可通过审计中记录的声明版本号解释。

## 发布规则

`POST /publish` 对患者属性做分组发布：

- **数据最小化**：只投影请求中指定的准标识符属性。
- **小样本抑制**：组内人数 `< min_group_size` 的组被抑制；被抑制组只返回组键与 `suppressed: true`，**不返回计数与成员**，因此调整阈值或发布策略后，无法通过计数、错误信息或响应差异反推被抑制个体。
- **假名化**：放行组中的患者以 `HMAC-SHA256(salt, patient_id)` 截断作为假名，同一患者（同一 salt）跨多次发布保持稳定，且无法由假名反推患者标识。

## 本地验证

```bash
uv sync                 # 安装依赖（fastapi、pytest）
uv run pytest -q        # 运行全部测试（-s 可查看判定日志：请求上下文 + 判定依据）
uv run uvicorn main:app --reload   # 启动服务，默认 http://127.0.0.1:8000
```

测试覆盖：同意撤回与并发判定、越权访问（可区分拒绝原因）、样本量边界（k-1 抑制 / k 放行）、重复与并发提交（单次判定、审计不丢失）、声明处理中被变更时结论稳定可解释。

持久化：`JsonStore(path)` 传入文件路径即可将患者、声明、判定与审计落盘为单个 JSON 文件；测试使用纯内存实例。
