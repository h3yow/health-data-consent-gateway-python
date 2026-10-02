"""线程安全的本地存储：机构、患者、同意声明、判定与审计。"""
from __future__ import annotations

import threading
from collections.abc import Callable

from .models import (
    AccessRequest,
    AuditRecord,
    ConsentHistoryAction,
    ConsentHistoryEntry,
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
        self.consent_history: dict[str, list[ConsentHistoryEntry]] = {}
        self.decisions: dict[str, Decision] = {}  # request_id -> Decision（幂等）
        self.audits: list[AuditRecord] = []
        self._audit_seq = 0

    # ---- 主体登记 -------------------------------------------------

    def register_institution(self, institution: Institution) -> tuple[Institution, bool]:
        """原子登记机构。返回 (最终记录, 是否本次新建)。

        同一 institution_id 并发登记只会写入一条；重复登记返回已有记录，
        不会出现两条互相打架的记录。
        """
        with self._lock:
            existing = self.institutions.get(institution.institution_id)
            if existing is not None:
                return existing, False
            self.institutions[institution.institution_id] = institution
            return institution, True

    def register_patient(self, patient: Patient) -> tuple[Patient, bool]:
        """原子登记患者。返回 (最终记录, 是否本次新建)。同一 ID 不覆盖。"""
        with self._lock:
            existing = self.patients.get(patient.patient_id)
            if existing is not None:
                return existing, False
            self.patients[patient.patient_id] = patient
            return patient, True

    def add_institution(self, institution: Institution) -> None:
        with self._lock:
            self.institutions[institution.institution_id] = institution

    def add_patient(self, patient: Patient) -> None:
        with self._lock:
            self.patients[patient.patient_id] = patient

    def get_patient(self, patient_id: str) -> Patient | None:
        with self._lock:
            return self.patients.get(patient_id)

    def get_institution(self, institution_id: str) -> Institution | None:
        with self._lock:
            return self.institutions.get(institution_id)

    def list_institutions(self) -> list[Institution]:
        with self._lock:
            return list(self.institutions.values())

    def list_patients(self) -> list[Patient]:
        with self._lock:
            return list(self.patients.values())

    # ---- 同意声明与历史 -------------------------------------------

    def create_consent_atomically(
        self, consent: ConsentStatement
    ) -> tuple[ConsentStatement, bool]:
        """原子建立声明（版本 1）并追加 created 历史。

        同一 consent_id 并发建立只有一条生效：返回 (最终声明, 是否本次新建)。
        """
        with self._lock:
            existing = self.consents.get(consent.consent_id)
            if existing is not None:
                return existing, False
            self.consents[consent.consent_id] = consent
            self.consent_history.setdefault(consent.consent_id, []).append(
                ConsentHistoryEntry(
                    consent_id=consent.consent_id,
                    version=consent.version,
                    action=ConsentHistoryAction.CREATED,
                    statement=consent,
                    changed_at=consent.updated_at,
                )
            )
            return consent, True

    def mutate_consent_atomically(
        self,
        consent_id: str,
        mutate_fn: Callable[[ConsentStatement], ConsentStatement],
    ) -> tuple[ConsentStatement, ConsentHistoryEntry]:
        """在同一临界区内读取当前声明 -> 计算下一版本 -> 覆盖并追加历史。

        mutate_fn 收到当前版本，返回下一版本（版本号必须正好 +1）。
        mutate_fn 抛出的异常原样传播，且不会产生任何写入或历史。
        并发变更 / 撤回在此串行化，不会重复记录、不会版本错乱或丢历史。
        """
        with self._lock:
            current = self.consents.get(consent_id)
            if current is None:
                raise KeyError(consent_id)
            next_statement = mutate_fn(current)
            if next_statement.consent_id != consent_id:
                raise ValueError("变更结果的 consent_id 不可改变")
            if next_statement.version != current.version + 1:
                raise ValueError("变更结果的版本号必须为当前版本 + 1")
            self.consents[consent_id] = next_statement
            action = (
                ConsentHistoryAction.WITHDRAWN
                if next_statement.status.value == "withdrawn"
                and current.status.value != "withdrawn"
                else ConsentHistoryAction.AMENDED
            )
            entry = ConsentHistoryEntry(
                consent_id=consent_id,
                version=next_statement.version,
                action=action,
                statement=next_statement,
                changed_at=next_statement.updated_at,
            )
            self.consent_history.setdefault(consent_id, []).append(entry)
            return next_statement, entry

    def get_consent(self, consent_id: str) -> ConsentStatement | None:
        with self._lock:
            return self.consents.get(consent_id)

    def consent_history_snapshot(
        self, patient_id: str | None = None
    ) -> dict[str, list[ConsentHistoryEntry]]:
        """返回历史的一致性复制快照；可按患者过滤。"""
        with self._lock:
            if patient_id is None:
                return {
                    cid: list(entries) for cid, entries in self.consent_history.items()
                }
            result: dict[str, list[ConsentHistoryEntry]] = {}
            for cid, entries in self.consent_history.items():
                current = self.consents.get(cid)
                if current is not None and current.patient_id == patient_id:
                    result[cid] = list(entries)
            return result

    def consents_for_patient(self, patient_id: str) -> list[ConsentStatement]:
        """返回该患者声明的一致性快照（同一锁内复制）。"""
        with self._lock:
            return [c for c in self.consents.values() if c.patient_id == patient_id]

    def patient_history_atomically(
        self,
        patient_id: str,
        action: "Callable[[dict[str, list[ConsentHistoryEntry]]], object]",
    ) -> object:
        """在存储临界区内对该患者的同意历史做一致性读取并执行 action。

        保证实时判定/回溯读取历史期间，并发的建立、变更、撤回不会让
        同一次判定看到跨越两个版本的半截视图。
        """
        with self._lock:
            snapshot = {
                cid: list(entries)
                for cid, entries in self.consent_history.items()
                if cid in self.consents
                and self.consents[cid].patient_id == patient_id
            }
            return action(snapshot)

    def patient_consent_history(
        self, patient_id: str
    ) -> list[tuple[str, list[ConsentHistoryEntry]]]:
        """返回某患者全部声明的历史，按 (consent_id, 版本升序条目) 排列。"""
        with self._lock:
            result = []
            for cid, entries in self.consent_history.items():
                current = self.consents.get(cid)
                if current is not None and current.patient_id == patient_id:
                    result.append((cid, list(entries)))
            result.sort(key=lambda item: item[0])
            return result

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

    def get_audit_by_request(self, request_id: str) -> AuditRecord | None:
        """按 request_id 取回当时的审计记录（含原始请求要素）。"""
        with self._lock:
            return next(
                (a for a in self.audits if a.request.request_id == request_id), None
            )
