"""线程安全的本地存储：机构、患者、同意声明、判定与审计。"""
from __future__ import annotations

import threading

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

    # ---- 以下为空实现，后续逐步填充 ----

    def add_institution(self, institution: Institution) -> None:
        raise NotImplementedError

    def add_patient(self, patient: Patient) -> None:
        raise NotImplementedError

    def get_patient(self, patient_id: str) -> Patient | None:
        raise NotImplementedError

    def save_consent(self, consent: ConsentStatement) -> None:
        raise NotImplementedError

    def get_consent(self, consent_id: str) -> ConsentStatement | None:
        raise NotImplementedError

    def consents_for_patient(self, patient_id: str) -> list[ConsentStatement]:
        raise NotImplementedError

    def get_decision(self, request_id: str) -> Decision | None:
        raise NotImplementedError

    def record_decision_if_absent(
        self, request: AccessRequest, decision: Decision
    ) -> tuple[Decision, bool]:
        """原子地记录判定与审计；若 request_id 已存在则返回已有判定。"""
        raise NotImplementedError

    def list_audits(self) -> list[AuditRecord]:
        raise NotImplementedError
