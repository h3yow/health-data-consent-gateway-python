# 跨机构健康数据访问网关

基于 FastAPI 的本地服务，用内存存储模拟机构、患者、同意声明与访问请求，
提供患者同意管理、访问判定与审计、去标识化发布三类能力。

## 同意模型

`ConsentStatement`（`gateway/models.py`）覆盖四个授权维度：

| 维度 | 字段 | 说明 |
| --- | --- | --- |
| 用途 | `purposes` | treatment / research / public_health / billing |
| 数据类别 | `categories` | demographics / diagnosis / lab_result / medication / genomic |
| 接收方 | `recipients` | 允许接收数据的机构 ID 集合 |
| 有效期 | `valid_from` / `valid_until` | 区间外一律无效 |

- 同一患者可同时存在多条部分重叠的声明；任一**有效**声明完整覆盖请求即可放行。
- 声明支持建立（`POST /consents`）、变更（`PATCH /consents/{id}`，版本号递增）、
  撤回（`POST /consents/{id}/withdraw`）。已撤回声明不可再变更。
- 过期或已撤回的声明不得作为放行依据。

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

## 发布规则

`PublicationService`（`gateway/publication.py`）：

- **去标识化**：患者以 HMAC-SHA256（盐值来自环境变量）假名出现，
  同一患者跨发布稳定，假名不可逆推身份。
- **数据最小化**：发布结果只含分组键、计数与假名列表。
- **小样本抑制**：计数低于阈值（默认 5，可用 `GATEWAY_MIN_GROUP_SIZE` 调整）
  的分组整体省略——不返回 0 计数行、错误信息为通用文案，
  阈值或策略调整后无法通过计数、错误信息或响应差异反推被抑制个体。

## 本地验证

```bash
uv sync                 # 安装依赖
uv run fastapi dev main.py   # 启动服务（或 uv run uvicorn main:app）
uv run pytest -q        # 运行全部测试
uv run pytest -q -s     # 打印请求上下文与判定依据日志
```

测试覆盖：同意撤回与并发判定、越权访问（各拒绝原因可区分）、
小样本阈值边界、重复/并发提交幂等、声明处理期变更的结论稳定性。

环境变量：`GATEWAY_PSEUDONYM_SALT`（假名盐值，默认 dev-only）、
`GATEWAY_MIN_GROUP_SIZE`（抑制阈值，默认 5）。
