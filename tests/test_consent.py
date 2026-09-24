"""Consent lifecycle: create, amend, revoke, expiry and their effect on decisions."""
from datetime import timedelta

import pytest

from app.models import (
    AccessRequest,
    ConsentStatus,
    DataCategory,
    DecisionOutcome,
    DenyReason,
    Purpose,
)

from .conftest import log_decision


def _request(request_id, patient_id, requester="org-a", purpose=Purpose.RESEARCH,
             categories=(DataCategory.DIAGNOSES,), now=None):
    return AccessRequest(
        request_id=request_id,
        patient_id=patient_id,
        requester_id=requester,
        purpose=purpose,
        data_categories=categories,
        submitted_at=now,
    )


def test_create_amend_revoke(consent_service, patient, validity):
    valid_from, valid_until = validity
    c = consent_service.create_consent(
        patient, {Purpose.RESEARCH}, {DataCategory.DIAGNOSES}, {"org-a"}, valid_from, valid_until
    )
    assert c.status is ConsentStatus.ACTIVE and c.version == 1

    amended = consent_service.amend_consent(c.consent_id, recipients={"org-a", "org-b"})
    assert amended.version == 2 and amended.recipients == {"org-a", "org-b"}

    revoked = consent_service.revoke_consent(c.consent_id)
    assert revoked.status is ConsentStatus.REVOKED and revoked.revoked_at is not None

    # revoke is idempotent
    again = consent_service.revoke_consent(c.consent_id)
    assert again.version == revoked.version

    with pytest.raises(ValueError):
        consent_service.amend_consent(c.consent_id, recipients={"org-c"})


def test_overlapping_consents_partial_coverage(consent_service, gateway, patient, validity, now):
    """Multiple partially overlapping statements: each covers its own scope only."""
    valid_from, valid_until = validity
    consent_service.create_consent(
        patient, {Purpose.RESEARCH}, {DataCategory.DIAGNOSES}, {"org-a"}, valid_from, valid_until
    )
    consent_service.create_consent(
        patient, {Purpose.RESEARCH}, {DataCategory.LAB_RESULTS}, {"org-b"}, valid_from, valid_until
    )

    req = _request("ov-1", patient, requester="org-a",
                   categories=(DataCategory.LAB_RESULTS,), now=now)
    decision = gateway.evaluate(req, now)
    log_decision(req, decision)
    assert decision.outcome is DecisionOutcome.DENY
    # each dimension is covered by *some* statement, but no single statement
    # covers the whole requested scope
    assert DenyReason.SCOPE_NOT_COVERED in decision.deny_reasons


def test_revoked_consent_no_longer_allows(consent_service, gateway, patient, validity, now):
    valid_from, valid_until = validity
    c = consent_service.create_consent(
        patient, {Purpose.RESEARCH}, {DataCategory.DIAGNOSES}, {"org-a"}, valid_from, valid_until
    )
    req1 = _request("rv-1", patient, now=now)
    d1 = gateway.evaluate(req1, now)
    log_decision(req1, d1)
    assert d1.outcome is DecisionOutcome.ALLOW

    consent_service.revoke_consent(c.consent_id)

    req2 = _request("rv-2", patient, now=now)
    d2 = gateway.evaluate(req2, now)
    log_decision(req2, d2)
    assert d2.outcome is DecisionOutcome.DENY
    assert DenyReason.CONSENT_REVOKED in d2.deny_reasons
    # the earlier allow decision is untouched and still auditable
    assert gateway.evaluate(req1, now).outcome is DecisionOutcome.ALLOW
    assert len(gateway.audits()) == 2


def test_expired_consent_denied_with_distinct_reason(consent_service, gateway, patient, now):
    c = consent_service.create_consent(
        patient, {Purpose.RESEARCH}, {DataCategory.DIAGNOSES}, {"org-a"},
        now - timedelta(days=10), now - timedelta(days=1),
    )
    assert c.status is ConsentStatus.ACTIVE  # expired by time, not revoked
    req = _request("ex-1", patient, now=now)
    decision = gateway.evaluate(req, now)
    log_decision(req, decision)
    assert decision.outcome is DecisionOutcome.DENY
    assert DenyReason.CONSENT_EXPIRED in decision.deny_reasons
    assert DenyReason.CONSENT_REVOKED not in decision.deny_reasons


def test_amend_narrows_scope(consent_service, gateway, patient, validity, now):
    valid_from, valid_until = validity
    c = consent_service.create_consent(
        patient, {Purpose.RESEARCH}, {DataCategory.DIAGNOSES, DataCategory.MEDICATIONS},
        {"org-a"}, valid_from, valid_until,
    )
    consent_service.amend_consent(c.consent_id, data_categories={DataCategory.DIAGNOSES})
    req = _request("am-1", patient, categories=(DataCategory.MEDICATIONS,), now=now)
    decision = gateway.evaluate(req, now)
    log_decision(req, decision)
    assert decision.outcome is DecisionOutcome.DENY
    assert DenyReason.CATEGORY_NOT_COVERED in decision.deny_reasons
