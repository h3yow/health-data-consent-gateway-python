"""同意声明服务：建立、变更、撤回、历史与有效性判断。"""
from __future__ import annotations

from datetime import datetime

from .models import (
    ConsentHistoryEntry,
    ConsentStatement,
    ConsentStatus,
    DataCategory,
    Purpose,
)
from .registry import RegistryService
from .storage import GatewayStore


def current_time() -> datetime:
    """网关统一时钟入口（测试可替换 consent 模块的 datetime 来固定时间）。"""
    return datetime.now().astimezone()


class ConsentNotFoundError(Exception):
    pass


class _AlreadyWithdrawn(Exception):
    """内部信号：声明已处于撤回态，撤回操作应幂等返回。"""

    def __init__(self, statement: ConsentStatement) -> None:
        super().__init__("already withdrawn")
        self.statement = statement


class DuplicateConsentError(Exception):
    """同一 consent_id 被重复建立。"""

    def __init__(self, consent_id: str) -> None:
        super().__init__(f"consent_already_exists:{consent_id}")
        self.consent_id = consent_id


class ConsentService:
    def __init__(self, store: GatewayStore, registry: RegistryService | None = None) -> None:
        self._store = store
        self._registry = registry if registry is not None else RegistryService(store)

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
        # 写入那一刻就挡住脏引用：患者不存在 / 机构不存在分别明确报错
        self._registry.require_patient(patient_id)
        for recipient_id in recipients:
            self._registry.require_institution(recipient_id)
        now = current_time()
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
        _, created = self._store.create_consent_atomically(consent)
        if not created:
            raise DuplicateConsentError(consent_id)
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
        """变更声明，版本号递增。已撤回的声明不可再变更。

        变更在存储临界区内完成读-改-写，并发变更/撤回串行化，
        不会重复记录、版本错乱或丢历史。
        """
        now = current_time()

        def mutate(current: ConsentStatement) -> ConsentStatement:
            if current.status == ConsentStatus.WITHDRAWN:
                raise ValueError("已撤回的声明不可变更")
            new_recipients = (
                frozenset(recipients) if recipients is not None else current.recipients
            )
            # 新增的接收方机构必须已登记
            for recipient_id in new_recipients - current.recipients:
                self._registry.require_institution(recipient_id)
            new_until = valid_until if valid_until is not None else current.valid_until
            if new_until <= current.valid_from:
                raise ValueError("valid_until 必须晚于 valid_from")
            return ConsentStatement(
                consent_id=current.consent_id,
                patient_id=current.patient_id,
                purposes=frozenset(purposes) if purposes is not None else current.purposes,
                categories=(
                    frozenset(categories) if categories is not None else current.categories
                ),
                recipients=new_recipients,
                valid_from=current.valid_from,
                valid_until=new_until,
                status=current.status,
                version=current.version + 1,
                updated_at=now,
            )

        try:
            amended, _ = self._store.mutate_consent_atomically(consent_id, mutate)
        except KeyError:
            raise ConsentNotFoundError(consent_id) from None
        return amended

    def withdraw(self, consent_id: str) -> ConsentStatement:
        """撤回声明。对已撤回声明重复/并发撤回是幂等的：

        返回当前（已撤回）版本，不再追加历史，保证并发撤回只留下
        一条 withdrawn 记录、最终版本唯一。
        """
        now = current_time()

        def mutate(current: ConsentStatement) -> ConsentStatement:
            if current.status == ConsentStatus.WITHDRAWN:
                # 幂等撤回：信号，外层据此跳过写入
                raise _AlreadyWithdrawn(current)
            return ConsentStatement(
                consent_id=current.consent_id,
                patient_id=current.patient_id,
                purposes=current.purposes,
                categories=current.categories,
                recipients=current.recipients,
                valid_from=current.valid_from,
                valid_until=current.valid_until,
                status=ConsentStatus.WITHDRAWN,
                version=current.version + 1,
                updated_at=now,
            )

        try:
            withdrawn, _ = self._store.mutate_consent_atomically(consent_id, mutate)
        except KeyError:
            raise ConsentNotFoundError(consent_id) from None
        except _AlreadyWithdrawn as exc:
            return exc.statement
        return withdrawn

    def history(self, consent_id: str) -> list[ConsentHistoryEntry]:
        """返回一条声明的完整历史（建立/变更/撤回），按版本升序。"""
        snapshot = self._store.consent_history_snapshot()
        entries = snapshot.get(consent_id)
        if not entries:
            raise ConsentNotFoundError(consent_id)
        return list(entries)

    def is_effective(self, consent: ConsentStatement, at: datetime) -> bool:
        """声明在给定时刻是否可作为放行依据：未撤回且在有效期内。"""
        return (
            consent.status == ConsentStatus.ACTIVE
            and consent.valid_from <= at <= consent.valid_until
        )
