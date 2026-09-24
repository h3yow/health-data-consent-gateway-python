"""Access decision tests: distinguishable denials, idempotency and concurrency."""
from concurrent.futures import ThreadPoolExecutor

from app.models import (
    AccessRequest,
    ConsentStatus,
    DataCategory,
    DecisionOutcome,
    DenyReason,
    Purpose,
)

from .conftest import log_decision


def _request(request_id, patient="patient-1", requester="org-a", purpose=Purpose.RESEARCH,
             categories=(DataCategory.DIAGNOSES,), now=None):
    return AccessRequest(request_id, patient, requester, purpose, categories, now)


def test_allow_and_distinguishable_deny_reasons(consent_service, gateway, patient, validity, now):
    valid_from, valid_until = validity
    consent_service.create_consent(
        patient, {Purpose.RESEARCH}, {DataCategory.DIAGNOSES}, {"org-a"}, valid_from, valid_until
    )

    # purpose outside scope
    r = _request("g-1", purpose=Purpose.BILLING, now=now)
    d = gateway.evaluate(r, now); log_decision(r, d)
    assert d.outcome is DecisionOutcome.DENY
    assert d.deny_reasons == (DenyReason.PURPOSE_NOT_COVERED,)

    # category outside scope
    r = _request("g-2", categories=(DataCategory.GENOMICS,), now=now)
    d = gateway.evaluate(r, now); log_decision(r, d)
    assert d.outcome is DecisionOutcome.DENY
    assert d.deny_reasons == (DenyReason.CATEGORY_NOT_COVERED,)

    # unknown recipient institution
    r = _request("g-3", requester="org-evil", now=now)
    d = gateway.evaluate(r, now); log_decision(r, d)
    assert d.outcome is DecisionOutcome.DENY
    assert d.deny_reasons == (DenyReason.RECIPIENT_NOT_COVERED,)

    # unknown patient
    r = _request("g-4", patient="ghost", now=now)
    d = gateway.evaluate(r, now); log_decision(r, d)
    assert d.deny_reasons == (DenyReason.PATIENT_NOT_FOUND,)


def test_duplicate_submission_records_single_decision_and_audit(
    consent_service, gateway, patient, validity, now
):
    valid_from, valid_until = validity
    consent_service.create_consent(
        patient, {Purpose.RESEARCH}, {DataCategory.DIAGNOSES}, {"org-a"}, valid_from, valid_until
    )
    r = _request("dup-1", now=now)
    first = gateway.evaluate(r, now); log_decision(r, first)
    assert first.outcome is DecisionOutcome.ALLOW

    for _ in range(5):
        again = gateway.evaluate(r, now)
        assert again == first

    decisions_for_request = [a for a in gateway.audits() if a.request_id == "dup-1"]
    assert len(decisions_for_request) == 1
    assert gateway.audits()[0].matched_consent_versions == (1,)


def test_concurrent_identical_requests_succeed_once(
    consent_service, gateway, patient, validity, now
):
    valid_from, valid_until = validity
    consent_service.create_consent(
        patient, {Purpose.RESEARCH}, {DataCategory.DIAGNOSES}, {"org-a"}, valid_from, valid_until
    )
    r = _request("cc-1", now=now)
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: gateway.evaluate(r, now), range(64)))

    assert all(d.outcome is DecisionOutcome.ALLOW for d in results)
    audits = [a for a in gateway.audits() if a.request_id == "cc-1"]
    assert len(audits) == 1
    log_decision(r, results[0])


def test_concurrent_distinct_requests_all_audited(
    consent_service, gateway, patient, validity, now
):
    valid_from, valid_until = validity
    consent_service.create_consent(
        patient, {Purpose.RESEARCH}, {DataCategory.DIAGNOSES}, {"org-a"}, valid_from, valid_until
    )
    requests = [_request(f"cd-{i}", requester="org-a", now=now) for i in range(32)]
    # also inject duplicate ids to stress idempotency
    requests += [_request("cd-0", now=now), _request("cd-7", now=now)]

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda r: gateway.evaluate(r, now), requests))

    assert all(d.outcome is DecisionOutcome.ALLOW for d in results)
    unique_ids = {r.request_id for r in requests}
    assert len(gateway.audits()) == len(unique_ids) == 32


def test_consent_changed_during_processing_gives_stable_decision(
    consent_service, gateway, patient, validity, now
):
    """While requests are in flight a consent is revoked: every decision must
    correspond to one identifiable consent-version snapshot (stable & explainable)."""
    valid_from, valid_until = validity
    c = consent_service.create_consent(
        patient, {Purpose.RESEARCH}, {DataCategory.DIAGNOSES}, {"org-a"}, valid_from, valid_until
    )

    def submit(i: int):
        r = _request(f"ch-{i}", now=now)
        d = gateway.evaluate(r, now)
        log_decision(r, d)
        return d

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(submit, i) for i in range(40)]
        consent_service.revoke_consent(c.consent_id)
        decisions = [f.result() for f in futures]

    # No third outcome; each decision is a consistent snapshot:
    # either matched v1 consent (allow), or explicitly deny due to revoke.
    for d in decisions:
        if d.outcome is DecisionOutcome.ALLOW:
            assert d.matched_consent_versions == (1,)
        else:
            assert DenyReason.CONSENT_REVOKED in d.deny_reasons
    assert len(gateway.audits()) == 40
    assert consent_service.consents_for_patient(patient)[0].status is ConsentStatus.REVOKED
