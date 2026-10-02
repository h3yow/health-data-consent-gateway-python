"""线程安全的本地存储：机构、患者、同意声明（含历史）、判定与审计。"""
from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime

from .models import (
    AccessRequest,
    AuditRecord,
    ConsentHistoryEntry,
    ConsentStatement,
    Decision,
    Institution,
    Patient,
)


class DuplicateSubjectError(Exception):
    """同一 ID 的主体重复登记。"""

    def __init__(self, kind: str, subject_id: str) -> None:
        self.kind = kind  # "institution" 或 "patient"
        self.subject_id = subject_id
        super().__init__(f"{kind} 已存在: {subject_id}")


class DuplicateConsentError(Exception):
    """同一 consent_id 重复建立。"""


class ConsentMissingError(Exception):
    """存储层：声明不存在（服务层转换为领域错误）。"""


class GatewayStore:
    """内存存储，所有写操作在锁内完成以保证并发一致性。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.institutions: dict[str, Institution] = {}
        self.patients: dict[str, Patient] = {}
        self.consents: dict[str, ConsentStatement] = {}
        self.consent_history: dict[str, list[ConsentHistoryEntry]] = {}
        self.decisions: dict[str, Decision] = {}  # request_id -> Decision（幂等）
        self.audits: list[AuditRecord] = []
        self._audit_seq = 0

    def add_institution(self, institution: Institution) -> None:
        with self._lock:
            self.institutions[institution.institution_id] = institution

    def add_patient(self, patient: Patient) -> None:
        with self._lock:
            self.patients[patient.patient_id] = patient

    def register_institution(self, institution: Institution) -> None:
        """原子登记机构；ID 已存在时抛 DuplicateSubjectError，不产生第二条记录。"""
        with self._lock:
            if institution.institution_id in self.institutions:
                raise DuplicateSubjectError("institution", institution.institution_id)
            self.institutions[institution.institution_id] = institution

    def register_patient(self, patient: Patient) -> None:
        """原子登记患者；ID 已存在时抛 DuplicateSubjectError，不产生第二条记录。"""
        with self._lock:
            if patient.patient_id in self.patients:
                raise DuplicateSubjectError("patient", patient.patient_id)
            self.patients[patient.patient_id] = patient

    def get_institution(self, institution_id: str) -> Institution | None:
        with self._lock:
            return self.institutions.get(institution_id)

    def get_patient(self, patient_id: str) -> Patient | None:
        with self._lock:
            return self.patients.get(patient_id)

    def list_institutions(self) -> list[Institution]:
        with self._lock:
            return list(self.institutions.values())

    def list_patients(self) -> list[Patient]:
        with self._lock:
            return list(self.patients.values())

    def save_consent(self, consent: ConsentStatement) -> None:
        with self._lock:
            self.consents[consent.consent_id] = consent

    def get_consent(self, consent_id: str) -> ConsentStatement | None:
        with self._lock:
            return self.consents.get(consent_id)

    def consents_for_patient(self, patient_id: str) -> list[ConsentStatement]:
        """返回该患者声明的一致性快照（同一锁内复制）。"""
        with self._lock:
            return [c for c in self.consents.values() if c.patient_id == patient_id]

    def create_consent(
        self, consent: ConsentStatement, entry: ConsentHistoryEntry
    ) -> None:
        """原子建立声明并写入首条历史；consent_id 已存在时抛 DuplicateConsentError。"""
        with self._lock:
            if consent.consent_id in self.consents:
                raise DuplicateConsentError(consent.consent_id)
            self.consents[consent.consent_id] = consent
            self.consent_history.setdefault(consent.consent_id, []).append(entry)

    def update_consent(
        self,
        consent_id: str,
        update_fn: "Callable[[ConsentStatement], tuple[ConsentStatement, ConsentHistoryEntry]]",
    ) -> ConsentStatement:
        """在同一临界区内完成「读取当前版本 -> 计算新版本 -> 落库 -> 追加历史」。

        并发变更/撤回同一声明时，版本号严格递增、历史不丢失、不产生分叉。
        """
        with self._lock:
            current = self.consents.get(consent_id)
            if current is None:
                raise ConsentMissingError(consent_id)
            updated, entry = update_fn(current)
            self.consents[consent_id] = updated
            self.consent_history.setdefault(consent_id, []).append(entry)
            return updated

    def get_consent_history(self, consent_id: str) -> list[ConsentHistoryEntry]:
        """按版本顺序返回声明的完整变更历史。"""
        with self._lock:
            return list(self.consent_history.get(consent_id, []))

    def consents_at(self, patient_id: str, at: datetime) -> list[ConsentStatement]:
        """按时间点回溯：还原 at 时刻该患者各声明的状态。

        取每条声明在 at 之前（含）最后一次变更对应的历史版本；
        at 之后才建立的声明不出现在快照中。
        """
        with self._lock:
            snapshot: list[ConsentStatement] = []
            for entries in self.consent_history.values():
                effective: ConsentHistoryEntry | None = None
                for entry in entries:
                    if entry.changed_at <= at:
                        effective = entry
                    else:
                        break
                if effective is not None and effective.patient_id == patient_id:
                    snapshot.append(effective.to_statement())
            return snapshot

    def find_audit(self, request_id: str) -> AuditRecord | None:
        with self._lock:
            for record in self.audits:
                if record.request.request_id == request_id:
                    return record
            return None

    def get_decision(self, request_id: str) -> Decision | None:
        with self._lock:
            return self.decisions.get(request_id)

    def decide_atomically(
        self, request: AccessRequest, decide_fn: "Callable[[list[ConsentStatement]], Decision]"
    ) -> tuple[Decision, bool]:
        """在同一临界区内完成：幂等检查 -> 同意快照 -> 判定 -> 落审计。

        保证声明在请求处理过程中被并发变更时，判定基于一致快照，
        且同一 request_id 只会产生一条判定与一条审计记录。
        """
        with self._lock:
            existing = self.decisions.get(request.request_id)
            if existing is not None:
                return existing, False
            snapshot = [
                c for c in self.consents.values() if c.patient_id == request.patient_id
            ]
            decision = decide_fn(snapshot)
            self.decisions[request.request_id] = decision
            self._audit_seq += 1
            self.audits.append(
                AuditRecord(
                    audit_id=f"audit-{self._audit_seq:06d}",
                    request=request,
                    decision=decision,
                )
            )
            return decision, True

    def record_decision_if_absent(
        self, request: AccessRequest, decision: Decision
    ) -> tuple[Decision, bool]:
        """原子地记录判定与审计；若 request_id 已存在则返回已有判定。

        返回 (最终判定, 是否为本次新写入)。重复/并发提交不会产生
        重复审计记录，也不会覆盖首次结论。
        """
        with self._lock:
            existing = self.decisions.get(request.request_id)
            if existing is not None:
                return existing, False
            self.decisions[request.request_id] = decision
            self._audit_seq += 1
            self.audits.append(
                AuditRecord(
                    audit_id=f"audit-{self._audit_seq:06d}",
                    request=request,
                    decision=decision,
                )
            )
            return decision, True

    def list_audits(self) -> list[AuditRecord]:
        with self._lock:
            return list(self.audits)
