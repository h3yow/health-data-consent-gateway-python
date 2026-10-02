"""同意声明服务：建立、变更、撤回、历史留痕与有效性判断。"""
from __future__ import annotations

from datetime import datetime

from .models import (
    ChangeType,
    ConsentHistoryEntry,
    ConsentStatement,
    ConsentStatus,
    DataCategory,
    Purpose,
)
from .storage import (
    ConsentMissingError,
    DuplicateConsentError,
    GatewayStore,
)


class ConsentNotFoundError(Exception):
    pass


class ConsentAlreadyExistsError(Exception):
    """同一 consent_id 重复建立。"""


class PatientNotRegisteredError(Exception):
    """声明引用的患者未登记。"""

    def __init__(self, patient_id: str) -> None:
        self.patient_id = patient_id
        super().__init__(f"患者未登记: {patient_id}")


class RecipientNotRegisteredError(Exception):
    """声明引用的接收方机构未登记。"""

    def __init__(self, institution_id: str) -> None:
        self.institution_id = institution_id
        super().__init__(f"机构未登记: {institution_id}")


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
        """建立声明。写入前校验：患者与接收方机构必须已登记，
        consent_id 不得重复——任一不满足都在写入那一刻失败，不落脏数据。"""
        if valid_until <= valid_from:
            raise ValueError("valid_until 必须晚于 valid_from")
        if self._store.get_patient(patient_id) is None:
            raise PatientNotRegisteredError(patient_id)
        for recipient in sorted(recipients):
            if self._store.get_institution(recipient) is None:
                raise RecipientNotRegisteredError(recipient)
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
        entry = ConsentHistoryEntry.from_statement(consent, ChangeType.CREATED)
        try:
            self._store.create_consent(consent, entry)
        except DuplicateConsentError as exc:
            raise ConsentAlreadyExistsError(consent_id) from exc
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
        """变更声明，版本号递增并留历史。已撤回的声明不可再变更。

        读取-修改-写入在存储锁内原子完成：并发变更不会丢版本或分叉历史。
        变更接收方时同样校验机构已登记。
        """
        if recipients is not None:
            for recipient in sorted(recipients):
                if self._store.get_institution(recipient) is None:
                    raise RecipientNotRegisteredError(recipient)

        def apply(current: ConsentStatement) -> tuple[ConsentStatement, ConsentHistoryEntry]:
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
            return amended, ConsentHistoryEntry.from_statement(amended, ChangeType.AMENDED)

        try:
            return self._store.update_consent(consent_id, apply)
        except ConsentMissingError as exc:
            raise ConsentNotFoundError(consent_id) from exc

    def withdraw(self, consent_id: str) -> ConsentStatement:
        """撤回声明，版本号递增并留历史。重复撤回是幂等空操作之外的
        显式变更：状态保持 WITHDRAWN，版本继续递增，历史完整可查。"""

        def apply(current: ConsentStatement) -> tuple[ConsentStatement, ConsentHistoryEntry]:
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
            return withdrawn, ConsentHistoryEntry.from_statement(withdrawn, ChangeType.WITHDRAWN)

        try:
            return self._store.update_consent(consent_id, apply)
        except ConsentMissingError as exc:
            raise ConsentNotFoundError(consent_id) from exc

    def history(self, consent_id: str) -> list[ConsentHistoryEntry]:
        """声明的完整变更历史（按版本顺序）；声明不存在时抛 ConsentNotFoundError。"""
        entries = self._store.get_consent_history(consent_id)
        if not entries:
            raise ConsentNotFoundError(consent_id)
        return entries

    def is_effective(self, consent: ConsentStatement, at: datetime) -> bool:
        """声明在给定时刻是否可作为放行依据：未撤回且在有效期内。"""
        return (
            consent.status == ConsentStatus.ACTIVE
            and consent.valid_from <= at <= consent.valid_until
        )
