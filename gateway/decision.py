"""访问判定服务：同意匹配、幂等判定与审计留痕。"""
from __future__ import annotations

from datetime import datetime

from .consent import ConsentService
from .models import (
    AccessRequest,
    AuditRecord,
    ConsentStatement,
    Decision,
    DecisionOutcome,
    DenyReason,
)
from .storage import GatewayStore


class DecisionService:
    def __init__(self, store: GatewayStore, consents: ConsentService) -> None:
        self._store = store
        self._consents = consents

    def evaluate(self, request: AccessRequest) -> Decision:
        """判定访问请求。

        - 幂等：同一 request_id 重复或并发提交，返回首次判定，不重复计数、
          不产生重复审计。
        - 稳定：判定在存储锁内基于同意快照完成；处理期间声明被变更不影响
          本次结论，判定依据（声明 ID 与版本）随审计留痕，可解释、可追溯。
        """
        now = datetime.now().astimezone()

        def decide(snapshot: list[ConsentStatement]) -> Decision:
            return self._evaluate_fresh(request, snapshot, now)

        decision, _ = self._store.decide_atomically(request, decide)
        return decision

    def evaluate_at(self, request: AccessRequest, at: datetime) -> Decision:
        """按时间点回溯判定：把 at 时刻的同意状态还原出来重判一遍。

        - 只读：不写判定、不落审计，用于事后复核。
        - 依据声明变更历史还原 at 时刻每条声明的版本与状态；
          当时未建立、已撤回、未生效或已过期的声明都不会被当作有效依据。
        - 判定逻辑与实时判定完全一致（同一 _evaluate_fresh），
          因此 at 取请求提交时刻时，结论与当时真实判定对得上。
        """
        snapshot = self._store.consents_at(request.patient_id, at)
        return self._evaluate_fresh(request, snapshot, at)

    def review(self, request_id: str) -> tuple[AuditRecord, Decision] | None:
        """事后复核：返回 (原始审计记录, 按提交时刻回溯的重判结果)。

        请求不存在时返回 None。重判按原始请求的 submitted_at 还原同意状态，
        此后声明再被变更/撤回不影响回溯结论。
        """
        record = self._store.find_audit(request_id)
        if record is None:
            return None
        retrospective = self.evaluate_at(record.request, record.request.submitted_at)
        return record, retrospective

    def _evaluate_fresh(
        self,
        request: AccessRequest,
        snapshot: list[ConsentStatement],
        now: datetime,
    ) -> Decision:
        """基于给定同意快照做出判定（含可区分的拒绝原因与判定依据）。"""
        if self._store.get_patient(request.patient_id) is None:
            return self._deny(request, now, (DenyReason.PATIENT_NOT_FOUND,), ())
        if self._store.get_institution(request.recipient_id) is None:
            return self._deny(request, now, (DenyReason.RECIPIENT_NOT_FOUND,), ())
        if not snapshot:
            return self._deny(request, now, (DenyReason.NO_CONSENT,), ())

        def covers(c: ConsentStatement) -> bool:
            return (
                request.purpose in c.purposes
                and request.categories <= c.categories
                and request.recipient_id in c.recipients
            )

        matching = [c for c in snapshot if covers(c)]
        versions = tuple((c.consent_id, c.version) for c in matching)

        if not matching:
            reasons: list[DenyReason] = []
            if not any(request.purpose in c.purposes for c in snapshot):
                reasons.append(DenyReason.PURPOSE_NOT_COVERED)
            if not any(request.categories <= c.categories for c in snapshot):
                reasons.append(DenyReason.CATEGORY_NOT_COVERED)
            if not any(request.recipient_id in c.recipients for c in snapshot):
                reasons.append(DenyReason.RECIPIENT_NOT_COVERED)
            return self._deny(request, now, tuple(reasons), versions)

        effective = [c for c in matching if self._consents.is_effective(c, now)]
        if effective:
            basis = "命中有效声明: " + ", ".join(
                f"{c.consent_id}@v{c.version}" for c in effective
            )
            return Decision(
                request_id=request.request_id,
                outcome=DecisionOutcome.ALLOW,
                reasons=(),
                basis=basis,
                decided_at=now,
                consent_versions=tuple((c.consent_id, c.version) for c in effective),
            )

        withdrawn = [c for c in matching if c.status.value == "withdrawn"]
        if withdrawn and len(withdrawn) == len(matching):
            reasons = (DenyReason.CONSENT_WITHDRAWN,)
        elif not withdrawn:
            reasons = (DenyReason.CONSENT_EXPIRED,)
        else:
            reasons = (DenyReason.CONSENT_WITHDRAWN, DenyReason.CONSENT_EXPIRED)
        return self._deny(request, now, reasons, versions)

    @staticmethod
    def _deny(
        request: AccessRequest,
        now: datetime,
        reasons: tuple[DenyReason, ...],
        versions: tuple[tuple[str, int], ...],
    ) -> Decision:
        basis = "拒绝原因: " + ", ".join(r.value for r in reasons)
        if versions:
            basis += "; 相关声明: " + ", ".join(f"{cid}@v{v}" for cid, v in versions)
        return Decision(
            request_id=request.request_id,
            outcome=DecisionOutcome.DENY,
            reasons=reasons,
            basis=basis,
            decided_at=now,
            consent_versions=versions,
        )
