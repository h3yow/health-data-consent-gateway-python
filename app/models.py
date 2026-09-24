"""Core domain types for the health data consent gateway.

Only type definitions live here; no business logic.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Purpose(str, Enum):
    TREATMENT = "treatment"
    RESEARCH = "research"
    PUBLIC_HEALTH = "public_health"
    BILLING = "billing"


class DataCategory(str, Enum):
    DEMOGRAPHICS = "demographics"
    DIAGNOSES = "diagnoses"
    LAB_RESULTS = "lab_results"
    MEDICATIONS = "medications"
    GENOMICS = "genomics"


class ConsentStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


@dataclass
class ConsentStatement:
    """A patient's consent statement covering purpose/category/recipient/validity."""

    consent_id: str
    patient_id: str
    purposes: frozenset[Purpose]
    data_categories: frozenset[DataCategory]
    recipients: frozenset[str]
    valid_from: datetime
    valid_until: datetime | None  # None = no expiry
    status: ConsentStatus
    version: int
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None


class DecisionOutcome(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


class DenyReason(str, Enum):
    PATIENT_NOT_FOUND = "patient_not_found"
    NO_CONSENT = "no_consent"
    CONSENT_REVOKED = "consent_revoked"
    CONSENT_EXPIRED = "consent_expired"
    PURPOSE_NOT_COVERED = "purpose_not_covered"
    CATEGORY_NOT_COVERED = "category_not_covered"
    RECIPIENT_NOT_COVERED = "recipient_not_covered"
    SCOPE_NOT_COVERED = "scope_not_covered"


@dataclass(frozen=True)
class AccessRequest:
    """An inbound data access request. request_id doubles as idempotency key."""

    request_id: str
    patient_id: str
    requester_id: str
    purpose: Purpose
    data_categories: tuple[DataCategory, ...]
    submitted_at: datetime


@dataclass(frozen=True)
class Decision:
    """Immutable outcome of evaluating one access request."""

    request_id: str
    outcome: DecisionOutcome
    deny_reasons: tuple[DenyReason, ...]
    matched_consent_ids: tuple[str, ...]
    matched_consent_versions: tuple[int, ...]
    basis: str
    decided_at: datetime


@dataclass(frozen=True)
class AuditRecord:
    """Traceable audit trail entry for every decision (allow or deny)."""

    audit_id: str
    request_id: str
    patient_id: str
    requester_id: str
    outcome: DecisionOutcome
    deny_reasons: tuple[DenyReason, ...]
    matched_consent_ids: tuple[str, ...]
    matched_consent_versions: tuple[int, ...]
    basis: str
    decided_at: datetime


@dataclass(frozen=True)
class PatientRecord:
    """A patient row held by the gateway (simulated local storage)."""

    patient_id: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PublishPolicy:
    """De-identification policy: k-anonymity style threshold + pseudonym salt."""

    min_group_size: int = 5
    salt: str = "local-dev-salt"
    pseudonym_length: int = 16


@dataclass(frozen=True)
class PublishedGroup:
    """One released aggregate group (meets the size threshold)."""

    group_key: tuple[tuple[str, str], ...]
    count: int
    pseudonyms: tuple[str, ...]


@dataclass(frozen=True)
class SuppressedGroup:
    """A group below threshold. Count and membership are never exposed."""

    group_key: tuple[tuple[str, str], ...]
    suppressed: bool = True


@dataclass(frozen=True)
class PublishReport:
    released: tuple[PublishedGroup, ...]
    suppressed: tuple[SuppressedGroup, ...]
    policy: PublishPolicy
    published_at: datetime
