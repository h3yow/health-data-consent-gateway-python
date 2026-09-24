"""Access request evaluation: decision + audit, idempotent and concurrency-safe."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from .models import (
    AccessRequest,
    AuditRecord,
    ConsentStatement,
    ConsentStatus,
    Decision,
    DecisionOutcome,
    DenyReason,
)
from .storage import JsonStore

logger = logging.getLogger("gateway")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_time_valid(consent: ConsentStatement, now: datetime) -> bool:
    if consent.valid_from > now:
        return False
    if consent.valid_until is not None and consent.valid_until <= now:
        return False
    return True


class Gateway:
    def __init__(self, store: JsonStore) -> None:
        self._store = store

    # -- evaluation against a frozen snapshot ------------------------------
    @staticmethod
    def _covers(consent: ConsentStatement, request: AccessRequest, now: datetime) -> bool:
        return (
            consent.status is ConsentStatus.ACTIVE
            and _is_time_valid(consent, now)
            and request.purpose in consent.purposes
            and set(request.data_categories) <= consent.data_categories
            and request.requester_id in consent.recipients
        )

    def _evaluate_snapshot(
        self, request: AccessRequest, consents: list[ConsentStatement], now: datetime
    ) -> Decision:
        decided_at = _now()
        if self._store.get_patient(request.patient_id) is None:
            return Decision(
                request_id=request.request_id,
                outcome=DecisionOutcome.DENY,
                deny_reasons=(DenyReason.PATIENT_NOT_FOUND,),
                matched_consent_ids=(),
                matched_consent_versions=(),
                basis=f"patient {request.patient_id} not found",
                decided_at=decided_at,
            )
        if not consents:
            return Decision(
                request_id=request.request_id,
                outcome=DecisionOutcome.DENY,
                deny_reasons=(DenyReason.NO_CONSENT,),
                matched_consent_ids=(),
                matched_consent_versions=(),
                basis="patient has no consent statements",
                decided_at=decided_at,
            )

        covering = [c for c in consents if self._covers(c, request, now)]
        if covering:
            basis = "; ".join(
                f"consent {c.consent_id} v{c.version} covers purpose={request.purpose.value}, "
                f"categories={[c_.value for c_ in request.data_categories]}, "
                f"recipient={request.requester_id}"
                for c in covering
            )
            return Decision(
                request_id=request.request_id,
                outcome=DecisionOutcome.ALLOW,
                deny_reasons=(),
                matched_consent_ids=tuple(c.consent_id for c in covering),
                matched_consent_versions=tuple(c.version for c in covering),
                basis=basis,
                decided_at=decided_at,
            )

        # Deny: derive distinguishable reasons from the snapshot.
        reasons: list[DenyReason] = []
        explanations: list[str] = []

        def covers_except(consent: ConsentStatement, dim: str) -> bool:
            if dim != "status" and consent.status is not ConsentStatus.ACTIVE:
                return False
            if dim != "time" and not _is_time_valid(consent, now):
                return False
            if dim != "purpose" and request.purpose not in consent.purposes:
                return False
            if dim != "category" and not set(request.data_categories) <= consent.data_categories:
                return False
            if dim != "recipient" and request.requester_id not in consent.recipients:
                return False
            return True

        revoked_near = [c for c in consents if c.status is ConsentStatus.REVOKED and covers_except(c, "status")]
        if revoked_near:
            reasons.append(DenyReason.CONSENT_REVOKED)
            explanations.append(
                "revoked consents otherwise matching: "
                + ", ".join(f"{c.consent_id} v{c.version}" for c in revoked_near)
            )
        expired_near = [c for c in consents if not _is_time_valid(c, now) and covers_except(c, "time")]
        if expired_near:
            reasons.append(DenyReason.CONSENT_EXPIRED)
            explanations.append(
                "expired/not-yet-valid consents otherwise matching: "
                + ", ".join(f"{c.consent_id} v{c.version}" for c in expired_near)
            )
        if not any(request.purpose in c.purposes for c in consents):
            reasons.append(DenyReason.PURPOSE_NOT_COVERED)
            explanations.append(f"no consent covers purpose={request.purpose.value}")
        if not any(set(request.data_categories) <= c.data_categories for c in consents):
            reasons.append(DenyReason.CATEGORY_NOT_COVERED)
            explanations.append(
                "no consent covers categories=" + str([c.value for c in request.data_categories])
            )
        if not any(request.requester_id in c.recipients for c in consents):
            reasons.append(DenyReason.RECIPIENT_NOT_COVERED)
            explanations.append(f"no consent covers recipient={request.requester_id}")
        if not reasons:
            # Dimensions each covered by *some* consent, but no single consent
            # covers all of them at once.
            reasons.append(DenyReason.SCOPE_NOT_COVERED)
            explanations.append("no single consent covers all requested dimensions simultaneously")

        return Decision(
            request_id=request.request_id,
            outcome=DecisionOutcome.DENY,
            deny_reasons=tuple(reasons),
            matched_consent_ids=(),
            matched_consent_versions=(),
            basis="; ".join(explanations),
            decided_at=decided_at,
        )

    # -- public entry point --------------------------------------------------
    def evaluate(self, request: AccessRequest, now: datetime | None = None) -> Decision:
        """Evaluate an access request. Idempotent on request_id: a repeated or
        concurrently-submitted request returns the original decision without
        recording a second audit entry."""
        existing = self._store.get_decision(request.request_id)
        if existing is not None:
            logger.info("duplicate request %s -> returning recorded decision", request.request_id)
            return existing

        eval_time = now or _now()
        # Snapshot consents once; the conclusion is based solely on this
        # snapshot, so mid-evaluation consent changes cannot make it unstable.
        consents = self._store.consents_for_patient(request.patient_id)
        decision = self._evaluate_snapshot(request, consents, eval_time)

        audit = AuditRecord(
            audit_id=uuid.uuid4().hex,
            request_id=request.request_id,
            patient_id=request.patient_id,
            requester_id=request.requester_id,
            outcome=decision.outcome,
            deny_reasons=decision.deny_reasons,
            matched_consent_ids=decision.matched_consent_ids,
            matched_consent_versions=decision.matched_consent_versions,
            basis=decision.basis,
            decided_at=decision.decided_at,
        )
        recorded = self._store.record_decision_and_audit(decision, audit)
        if not recorded:
            # A concurrent evaluation of the same request_id won the race.
            return self._store.get_decision(request.request_id)
        logger.info(
            "request %s patient=%s requester=%s purpose=%s -> %s (%s)",
            request.request_id,
            request.patient_id,
            request.requester_id,
            request.purpose.value,
            decision.outcome.value,
            decision.basis,
        )
        return decision

    def audits(self) -> list[AuditRecord]:
        return self._store.list_audits()
