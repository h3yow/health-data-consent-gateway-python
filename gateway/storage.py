"""线程安全的本地存储：机构、患者、同意声明、判定与审计。"""
from __future__ import annotations

import threading
from collections.abc import Callable

from .models import (
    AccessRequest,
    AuditRecord,
    ConsentStatement,
    Decision,
    Institution,
    Patient,
)


class GatewayStore:
    """内存存储，所有写操作在锁内完成以保证并发一致性。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.institutions: dict[str, Institution] = {}
        self.patients: dict[str, Patient] = {}
        self.consents: dict[str, ConsentStatement] = {}
        self.decisions: dict[str, Decision] = {}  # request_id -> Decision（幂等）
        self.audits: list[AuditRecord] = []
        self._audit_seq = 0

    def add_institution(self, institution: Institution) -> None:
        with self._lock:
            self.institutions[institution.institution_id] = institution

    def add_patient(self, patient: Patient) -> None:
        with self._lock:
            self.patients[patient.patient_id] = patient

    def get_patient(self, patient_id: str) -> Patient | None:
        with self._lock:
            return self.patients.get(patient_id)

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
