"""领域模型：机构、患者、同意声明、访问请求、判定与审计、发布结果。"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime


class Purpose(enum.StrEnum):
    """数据使用用途。"""

    TREATMENT = "treatment"
    RESEARCH = "research"
    PUBLIC_HEALTH = "public_health"
    BILLING = "billing"


class DataCategory(enum.StrEnum):
    """健康数据类别。"""

    DEMOGRAPHICS = "demographics"
    DIAGNOSIS = "diagnosis"
    LAB_RESULT = "lab_result"
    MEDICATION = "medication"
    GENOMIC = "genomic"


class ConsentStatus(enum.StrEnum):
    """同意声明状态。"""

    ACTIVE = "active"
    WITHDRAWN = "withdrawn"


class ConsentHistoryAction(enum.StrEnum):
    """同意声明历史动作。"""

    CREATED = "created"
    AMENDED = "amended"
    WITHDRAWN = "withdrawn"


class DecisionOutcome(enum.StrEnum):
    """访问判定结论。"""

    ALLOW = "allow"
    DENY = "deny"


class DenyReason(enum.StrEnum):
    """可区分的拒绝原因。"""

    NO_CONSENT = "no_consent"
    CONSENT_EXPIRED = "consent_expired"
    CONSENT_WITHDRAWN = "consent_withdrawn"
    PURPOSE_NOT_COVERED = "purpose_not_covered"
    CATEGORY_NOT_COVERED = "category_not_covered"
    RECIPIENT_NOT_COVERED = "recipient_not_covered"
    PATIENT_NOT_FOUND = "patient_not_found"
    RECIPIENT_NOT_FOUND = "recipient_not_found"


@dataclass(frozen=True)
class Institution:
    institution_id: str
    name: str


@dataclass(frozen=True)
class Patient:
    patient_id: str
    name: str
    birth_year: int
    diagnosis: str = ""
    lab_group: str = ""


@dataclass
class ConsentStatement:
    """患者同意声明：覆盖用途、数据类别、接收方与有效期。"""

    consent_id: str
    patient_id: str
    purposes: frozenset[Purpose]
    categories: frozenset[DataCategory]
    recipients: frozenset[str]  # 允许接收数据的机构 ID
    valid_from: datetime
    valid_until: datetime
    status: ConsentStatus = ConsentStatus.ACTIVE
    version: int = 1
    updated_at: datetime | None = None


@dataclass(frozen=True)
class AccessRequest:
    """访问请求。request_id 由调用方提供，用于幂等去重。"""

    request_id: str
    patient_id: str
    recipient_id: str
    purpose: Purpose
    categories: frozenset[DataCategory]
    submitted_at: datetime


@dataclass(frozen=True)
class Decision:
    """判定结果：结论 + 可区分原因 + 判定依据快照。"""

    request_id: str
    outcome: DecisionOutcome
    reasons: tuple[DenyReason, ...] = ()
    basis: str = ""
    decided_at: datetime | None = None
    consent_versions: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True)
class AuditRecord:
    """审计留痕：放行与拒绝都记录可追溯的判定依据。"""

    audit_id: str
    request: AccessRequest
    decision: Decision


@dataclass(frozen=True)
class PublicationRow:
    """发布结果中的一行聚合数据。"""

    group_key: str
    pseudonym_ids: tuple[str, ...]
    count: int


@dataclass(frozen=True)
class ConsentHistoryEntry:
    """同意声明的一条不可变历史：建立 / 变更 / 撤回各留一条。

    statement 为该动作完成后的声明完整快照（含版本号与状态）。
    """

    consent_id: str
    version: int
    action: ConsentHistoryAction
    statement: ConsentStatement
    changed_at: datetime


@dataclass(frozen=True)
class ReplayEvidence:
    """回溯判定中一条声明的还原依据：该时刻生效的是哪个版本、为何用/不用。"""

    consent_id: str
    version: int
    status: ConsentStatus | None
    considered: bool  # 是否被当作有效放行依据
    reason: str  # used / used_not_matching / withdrawn / not_yet_effective / expired / not_created


@dataclass(frozen=True)
class ReplayResult:
    """按某个时间点还原同意状态后的复核结论。

    - decision：还原那一刻的同意状态后重判得到的结论。
    - as_of：回溯时间点。
    - evidence：逐条声明的版本还原依据。
    - matches_original：与该请求当时真实判定的结论是否一致。
    """

    request: AccessRequest
    decision: Decision
    as_of: datetime
    evidence: tuple[ReplayEvidence, ...]
    matches_original: bool
