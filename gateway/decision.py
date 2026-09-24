"""访问判定服务：同意匹配、幂等判定与审计留痕。"""
from __future__ import annotations

from datetime import datetime

from .consent import ConsentService
from .models import (
    AccessRequest,
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

    def _evaluate_fresh(
        self,
        request: AccessRequest,
        snapshot: list[ConsentStatement],
        now: datetime,
    ) -> Decision:
        """基于给定同意快照做出判定（含可区分的拒绝原因与判定依据）。"""
        if self._store.get_patient(request.patient_id) is None:
            return self._deny(request, now, (DenyReason.PATIENT_NOT_FOUND,), ())
        if request.recipient_id not in self._store.institutions:
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
