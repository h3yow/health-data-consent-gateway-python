"""访问判定服务：同意匹配、幂等判定、审计留痕与按时间点回溯复核。"""
from __future__ import annotations

from datetime import datetime

from .consent import ConsentService
from .models import (
    AccessRequest,
    ConsentStatement,
    Decision,
    DecisionOutcome,
    DenyReason,
    ReplayEvidence,
    ReplayResult,
)
from .reconstruction import ReconstructedConsent, reconstruct_at
from .registry import RegistryService
from .storage import GatewayStore


class DecisionService:
    def __init__(
        self,
        store: GatewayStore,
        consents: ConsentService,
        registry: RegistryService | None = None,
    ) -> None:
        self._store = store
        self._consents = consents
        self._registry = registry if registry is not None else RegistryService(store)

    def evaluate(self, request: AccessRequest) -> Decision:
        """判定访问请求。

        - 引用校验：患者 / 接收机构未登记时在提交那一刻即给出可区分的
          拒绝结论（patient_not_found / recipient_not_found），
          不会当成功存下来。
        - 幂等：同一 request_id 重复或并发提交，返回首次判定，不重复计数、
          不产生重复审计。
        - 稳定：判定在存储锁内基于「提交时刻的同意历史快照」完成，
          处理期间声明被并发变更不影响本次结论；判定依据（声明 ID 与版本）
          随审计留痕，可解释、可追溯。
        - 判定时刻取请求的 submitted_at，先按历史还原那一刻的声明版本再匹配：
          与按时间点回溯复核共用同一套还原与判定逻辑，结论可对账。
        """
        decided_at = request.submitted_at

        def decide(_snapshot: list[ConsentStatement]) -> Decision:
            # 提交那一刻先挡未登记主体（在幂等判定与审计落库之前）
            if self._store.get_patient(request.patient_id) is None:
                return self._deny(
                    request, decided_at, (DenyReason.PATIENT_NOT_FOUND,), ()
                )
            if self._store.get_institution(request.recipient_id) is None:
                return self._deny(
                    request, decided_at, (DenyReason.RECIPIENT_NOT_FOUND,), ()
                )

            # RLock 可重入：在临界区内取该患者的一致性历史快照并还原
            def use_history(
                history_map: dict[str, list],
            ) -> tuple[Decision, tuple[ReplayEvidence, ...]]:
                return self._decide_from_history(request, history_map, decided_at)

            decision, _evidence = self._store.patient_history_atomically(
                request.patient_id, use_history
            )
            return decision

        decision, _ = self._store.decide_atomically(request, decide)
        return decision

    def _decide_from_history(
        self,
        request: AccessRequest,
        history_map: dict[str, list],
        as_of: datetime,
    ) -> tuple[Decision, tuple[ReplayEvidence, ...]]:
        """共用核心：按历史还原 ``as_of`` 时刻的声明版本后判定，附版本依据。

        实时判定与时间点回溯都走这里，保证两者口径一致。
        """
        reconstructed: list[ReconstructedConsent] = [
            reconstruct_at(entries, as_of) for entries in history_map.values()
        ]

        def covers(c: ConsentStatement) -> bool:
            return (
                request.purpose in c.purposes
                and request.categories <= c.categories
                and request.recipient_id in c.recipients
            )

        evidence: list[ReplayEvidence] = []
        matching: list[ConsentStatement] = []
        for rc in reconstructed:
            stmt = rc.statement
            if stmt is None:
                evidence.append(
                    ReplayEvidence(
                        consent_id=rc.consent_id,
                        version=0,
                        status=None,
                        considered=False,
                        reason=rc.state,
                    )
                )
                continue
            is_match = covers(stmt)
            considered = is_match and rc.state == "used"
            if considered:
                matching.append(stmt)
            if is_match:
                reason = "used" if rc.state == "used" else rc.state
            else:
                reason = "used_not_matching" if rc.state == "used" else rc.state
            evidence.append(
                ReplayEvidence(
                    consent_id=stmt.consent_id,
                    version=stmt.version,
                    status=stmt.status,
                    considered=considered,
                    reason=reason,
                )
            )

        versions = tuple((c.consent_id, c.version) for c in matching)
        if matching:
            basis = "命中有效声明: " + ", ".join(
                f"{c.consent_id}@v{c.version}" for c in matching
            )
            decision = Decision(
                request_id=request.request_id,
                outcome=DecisionOutcome.ALLOW,
                reasons=(),
                basis=basis,
                decided_at=as_of,
                consent_versions=versions,
            )
            return decision, tuple(evidence)

        reasons = self._deny_reasons(request, reconstructed)
        decision = self._deny(request, as_of, reasons, self._matching_versions(request, reconstructed))
        return decision, tuple(evidence)

    @staticmethod
    def _matching_versions(
        request: AccessRequest,
        reconstructed: list[ReconstructedConsent],
    ) -> tuple[tuple[str, int], ...]:
        versions = []
        for rc in reconstructed:
            stmt = rc.statement
            if stmt is None:
                continue
            if (
                request.purpose in stmt.purposes
                and request.categories <= stmt.categories
                and request.recipient_id in stmt.recipients
            ):
                versions.append((stmt.consent_id, stmt.version))
        return tuple(versions)

    @staticmethod
    def _deny_reasons(
        request: AccessRequest,
        reconstructed: list[ReconstructedConsent],
    ) -> tuple[DenyReason, ...]:
        existing = [rc for rc in reconstructed if rc.statement is not None]
        if not existing:
            return (DenyReason.NO_CONSENT,)

        def covers(c: ConsentStatement) -> bool:
            return (
                request.purpose in c.purposes
                and request.categories <= c.categories
                and request.recipient_id in c.recipients
            )

        matched = [rc for rc in existing if covers(rc.statement)]
        if not matched:
            reasons: list[DenyReason] = []
            if not any(request.purpose in rc.statement.purposes for rc in existing):
                reasons.append(DenyReason.PURPOSE_NOT_COVERED)
            if not any(request.categories <= rc.statement.categories for rc in existing):
                reasons.append(DenyReason.CATEGORY_NOT_COVERED)
            if not any(request.recipient_id in rc.statement.recipients for rc in existing):
                reasons.append(DenyReason.RECIPIENT_NOT_COVERED)
            return tuple(reasons)

        states = {rc.state for rc in matched}
        reasons = []
        if "withdrawn" in states:
            reasons.append(DenyReason.CONSENT_WITHDRAWN)
        if states & {"expired", "not_yet_effective"}:
            reasons.append(DenyReason.CONSENT_EXPIRED)
        return tuple(reasons) or (DenyReason.NO_CONSENT,)

    # ---- 按时间点回溯复核 ----------------------------------------

    def replay(self, request: AccessRequest, as_of: datetime) -> ReplayResult:
        """按历史还原 ``as_of`` 时刻的同意状态并重判该访问请求。

        只依据 ``changed_at <= as_of`` 的历史版本：已撤回、当时还没生效、
        已经过期、或当时尚未建立的声明都不会被当作有效依据。
        结论附带逐条声明的版本依据，并与该请求当时真实判定的结论对账
        （同一 request_id 的首次判定结论应一致）。
        """
        def use_history(
            history_map: dict[str, list],
        ) -> tuple[Decision, tuple[ReplayEvidence, ...]]:
            return self._decide_from_history(request, history_map, as_of)

        decision, evidence = self._store.patient_history_atomically(
            request.patient_id, use_history
        )

        original = self._store.get_decision(request.request_id)
        matches = original is None or original.outcome == decision.outcome
        return ReplayResult(
            request=request,
            decision=decision,
            as_of=as_of,
            evidence=evidence,
            matches_original=matches,
        )

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
