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


class ChangeType(enum.StrEnum):
    """同意声明变更类型。"""

    CREATED = "created"
    AMENDED = "amended"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True)
class ConsentHistoryEntry:
    """同意声明的一次变更留痕：某一版本在何时成为当前状态。"""

    consent_id: str
    patient_id: str
    version: int
    change_type: ChangeType
    status: ConsentStatus
    purposes: frozenset[Purpose]
    categories: frozenset[DataCategory]
    recipients: frozenset[str]
    valid_from: datetime
    valid_until: datetime
    changed_at: datetime  # 该版本生效（写入）的真实时间

    @classmethod
    def from_statement(
        cls, consent: ConsentStatement, change_type: ChangeType
    ) -> ConsentHistoryEntry:
        return cls(
            consent_id=consent.consent_id,
            patient_id=consent.patient_id,
            version=consent.version,
            change_type=change_type,
            status=consent.status,
            purposes=consent.purposes,
            categories=consent.categories,
            recipients=consent.recipients,
            valid_from=consent.valid_from,
            valid_until=consent.valid_until,
            changed_at=consent.updated_at,
        )

    def to_statement(self) -> ConsentStatement:
        """还原该历史版本对应的声明状态（用于按时间点回溯判定）。"""
        return ConsentStatement(
            consent_id=self.consent_id,
            patient_id=self.patient_id,
            purposes=self.purposes,
            categories=self.categories,
            recipients=self.recipients,
            valid_from=self.valid_from,
            valid_until=self.valid_until,
            status=self.status,
            version=self.version,
            updated_at=self.changed_at,
        )


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
