"""Consent lifecycle service: create, amend, revoke."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from .models import ConsentStatement, ConsentStatus, DataCategory, Purpose
from .storage import JsonStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConsentService:
    def __init__(self, store: JsonStore) -> None:
        self._store = store

    def create_consent(
        self,
        patient_id: str,
        purposes: set[Purpose],
        data_categories: set[DataCategory],
        recipients: set[str],
        valid_from: datetime,
        valid_until: datetime | None = None,
    ) -> ConsentStatement:
        if self._store.get_patient(patient_id) is None:
            raise ValueError(f"unknown patient: {patient_id}")
        if not purposes or not data_categories or not recipients:
            raise ValueError("purposes, data_categories and recipients must be non-empty")
        if valid_until is not None and valid_until <= valid_from:
            raise ValueError("valid_until must be after valid_from")
        now = _now()
        consent = ConsentStatement(
            consent_id=uuid.uuid4().hex,
            patient_id=patient_id,
            purposes=frozenset(purposes),
            data_categories=frozenset(data_categories),
            recipients=frozenset(recipients),
            valid_from=valid_from,
            valid_until=valid_until,
            status=ConsentStatus.ACTIVE,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self._store.put_consent(consent)
        return consent

    def amend_consent(
        self,
        consent_id: str,
        *,
        purposes: set[Purpose] | None = None,
        data_categories: set[DataCategory] | None = None,
        recipients: set[str] | None = None,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
    ) -> ConsentStatement:
        current = self._store.get_consent(consent_id)
        if current is None:
            raise ValueError(f"unknown consent: {consent_id}")
        if current.status is ConsentStatus.REVOKED:
            raise ValueError("cannot amend a revoked consent")
        new_valid_from = valid_from if valid_from is not None else current.valid_from
        new_valid_until = valid_until if valid_until is not None else current.valid_until
        if new_valid_until is not None and new_valid_until <= new_valid_from:
            raise ValueError("valid_until must be after valid_from")
        amended = ConsentStatement(
            consent_id=current.consent_id,
            patient_id=current.patient_id,
            purposes=frozenset(purposes) if purposes is not None else current.purposes,
            data_categories=(
                frozenset(data_categories) if data_categories is not None else current.data_categories
            ),
            recipients=frozenset(recipients) if recipients is not None else current.recipients,
            valid_from=new_valid_from,
            valid_until=new_valid_until,
            status=current.status,
            version=current.version + 1,
            created_at=current.created_at,
            updated_at=_now(),
            revoked_at=current.revoked_at,
        )
        self._store.put_consent(amended)
        return amended

    def revoke_consent(self, consent_id: str) -> ConsentStatement:
        current = self._store.get_consent(consent_id)
        if current is None:
            raise ValueError(f"unknown consent: {consent_id}")
        if current.status is ConsentStatus.REVOKED:
            return current  # idempotent revoke
        revoked = ConsentStatement(
            consent_id=current.consent_id,
            patient_id=current.patient_id,
            purposes=current.purposes,
            data_categories=current.data_categories,
            recipients=current.recipients,
            valid_from=current.valid_from,
            valid_until=current.valid_until,
            status=ConsentStatus.REVOKED,
            version=current.version + 1,
            created_at=current.created_at,
            updated_at=_now(),
            revoked_at=_now(),
        )
        self._store.put_consent(revoked)
        return revoked

    def consents_for_patient(self, patient_id: str) -> list[ConsentStatement]:
        return self._store.consents_for_patient(patient_id)
