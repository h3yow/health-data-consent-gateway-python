"""同意声明服务：建立、变更、撤回与有效性判断。"""
from __future__ import annotations

from datetime import datetime

from .models import (
    ConsentStatement,
    ConsentStatus,
    DataCategory,
    Purpose,
)
from .storage import GatewayStore


class ConsentNotFoundError(Exception):
    pass


class ConsentService:
    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def create(
        self,
        consent_id: str,
        patient_id: str,
        purposes: set[Purpose],
        categories: set[DataCategory],
        recipients: set[str],
        valid_from: datetime,
        valid_until: datetime,
    ) -> ConsentStatement:
        if valid_until <= valid_from:
            raise ValueError("valid_until 必须晚于 valid_from")
        now = datetime.now().astimezone()
        consent = ConsentStatement(
            consent_id=consent_id,
            patient_id=patient_id,
            purposes=frozenset(purposes),
            categories=frozenset(categories),
            recipients=frozenset(recipients),
            valid_from=valid_from,
            valid_until=valid_until,
            status=ConsentStatus.ACTIVE,
            version=1,
            updated_at=now,
        )
        self._store.save_consent(consent)
        return consent

    def amend(
        self,
        consent_id: str,
        *,
        purposes: set[Purpose] | None = None,
        categories: set[DataCategory] | None = None,
        recipients: set[str] | None = None,
        valid_until: datetime | None = None,
    ) -> ConsentStatement:
        """变更声明，版本号递增。已撤回的声明不可再变更。"""
        current = self._store.get_consent(consent_id)
        if current is None:
            raise ConsentNotFoundError(consent_id)
        if current.status == ConsentStatus.WITHDRAWN:
            raise ValueError("已撤回的声明不可变更")
        amended = ConsentStatement(
            consent_id=current.consent_id,
            patient_id=current.patient_id,
            purposes=frozenset(purposes) if purposes is not None else current.purposes,
            categories=(
                frozenset(categories) if categories is not None else current.categories
            ),
            recipients=(
                frozenset(recipients) if recipients is not None else current.recipients
            ),
            valid_from=current.valid_from,
            valid_until=valid_until if valid_until is not None else current.valid_until,
            status=current.status,
            version=current.version + 1,
            updated_at=datetime.now().astimezone(),
        )
        self._store.save_consent(amended)
        return amended

    def withdraw(self, consent_id: str) -> ConsentStatement:
        current = self._store.get_consent(consent_id)
        if current is None:
            raise ConsentNotFoundError(consent_id)
        withdrawn = ConsentStatement(
            consent_id=current.consent_id,
            patient_id=current.patient_id,
            purposes=current.purposes,
            categories=current.categories,
            recipients=current.recipients,
            valid_from=current.valid_from,
            valid_until=current.valid_until,
            status=ConsentStatus.WITHDRAWN,
            version=current.version + 1,
            updated_at=datetime.now().astimezone(),
        )
        self._store.save_consent(withdrawn)
        return withdrawn

    def is_effective(self, consent: ConsentStatement, at: datetime) -> bool:
        """声明在给定时刻是否可作为放行依据：未撤回且在有效期内。"""
        return (
            consent.status == ConsentStatus.ACTIVE
            and consent.valid_from <= at <= consent.valid_until
        )
