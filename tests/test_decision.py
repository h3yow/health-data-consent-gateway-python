"""访问判定：放行、可区分拒绝、撤回、幂等与并发。"""
from datetime import timedelta
from threading import Thread

from gateway.consent import ConsentService
from gateway.decision import DecisionService
from gateway.models import (
    DataCategory,
    DecisionOutcome,
    DenyReason,
    Patient,
    Purpose,
)
from gateway.storage import GatewayStore
from tests.conftest import NOW, active_consent_kwargs, log_decision, make_request


def _setup_patient(store: GatewayStore, patient_id: str = "p1") -> None:
    store.add_patient(Patient(patient_id, "张三", 1980, diagnosis="flu"))


def test_allow_with_basis(store, consents, decisions):
    _setup_patient(store)
    consents.create(**active_consent_kwargs())
    req = make_request("r-allow")
    d = decisions.evaluate(req)
    log_decision(req, d)
    assert d.outcome == DecisionOutcome.ALLOW
    assert d.consent_versions == (("c1", 1),)
    assert "c1@v1" in d.basis
    # 放行也留痕
    audits = store.list_audits()
    assert len(audits) == 1 and audits[0].decision.outcome == DecisionOutcome.ALLOW


def test_deny_reasons_distinguishable(store, consents, decisions):
    _setup_patient(store)
    consents.create(**active_consent_kwargs())

    cases = [
        ("r-purpose", make_request("r-purpose", purpose=Purpose.RESEARCH), DenyReason.PURPOSE_NOT_COVERED),
        ("r-category", make_request("r-category", categories=frozenset({DataCategory.GENOMIC})), DenyReason.CATEGORY_NOT_COVERED),
        ("r-recipient", make_request("r-recipient", recipient_id="pharma-c"), DenyReason.RECIPIENT_NOT_COVERED),
        ("r-no-consent", make_request("r-no-consent", patient_id="p2"), DenyReason.NO_CONSENT),
        ("r-no-patient", make_request("r-no-patient", patient_id="ghost"), DenyReason.PATIENT_NOT_FOUND),
        ("r-no-recipient", make_request("r-no-recipient", recipient_id="ghost-org"), DenyReason.RECIPIENT_NOT_FOUND),
    ]
    store.add_patient(Patient("p2", "李四", 1990))
    outcomes = []
    for request_id, req, expected in cases:
        d = decisions.evaluate(req)
        log_decision(req, d)
        assert d.outcome == DecisionOutcome.DENY
        assert d.reasons == (expected,), f"{request_id}: {d.reasons}"
        outcomes.append(d.reasons[0])
    # 每种失败原因可区分
    assert len(set(outcomes)) == len(cases)


def test_expired_and_withdrawn_denied(store, consents, decisions):
    _setup_patient(store)
    consents.create(
        **active_consent_kwargs(
            consent_id="c-exp",
            valid_from=NOW - timedelta(days=60),
            valid_until=NOW - timedelta(days=1),
        )
    )
    req = make_request("r-expired")
    d = decisions.evaluate(req)
    log_decision(req, d)
    assert d.reasons == (DenyReason.CONSENT_EXPIRED,)

    consents.create(**active_consent_kwargs(consent_id="c-w"))
    consents.withdraw("c-w")
    req2 = make_request("r-withdrawn")
    d2 = decisions.evaluate(req2)
    log_decision(req2, d2)
    # 撤回与过期原因可区分：两条匹配声明各自贡献原因
    assert DenyReason.CONSENT_WITHDRAWN in d2.reasons
    assert DenyReason.CONSENT_EXPIRED in d2.reasons
    assert ("c-w", 2) in d2.consent_versions


def test_overlapping_consents_allow_via_any(store, consents, decisions):
    """多条部分重叠声明：任一有效声明覆盖即可放行。"""
    _setup_patient(store)
    consents.create(**active_consent_kwargs(consent_id="c1", recipients={"lab-b"}))
    consents.create(**active_consent_kwargs(consent_id="c2"))
    req = make_request("r-overlap")
    d = decisions.evaluate(req)
    log_decision(req, d)
    assert d.outcome == DecisionOutcome.ALLOW
    assert ("c2", 1) in d.consent_versions


def test_duplicate_submission_idempotent(store, consents, decisions):
    """同一 request_id 重复提交：不重复放行、不重复计数、审计不丢失。"""
    _setup_patient(store)
    consents.create(**active_consent_kwargs())
    req = make_request("r-dup")
    d1 = decisions.evaluate(req)
    d2 = decisions.evaluate(req)
    d3 = decisions.evaluate(req)
    log_decision(req, d3)
    assert d1 is d2 is d3
    assert len(store.list_audits()) == 1
    assert len(store.decisions) == 1


def test_unregistered_subject_denial_idempotent(store, consents, decisions):
    """未登记患者 / 机构的 DENY 同样落判定与审计，且重复、并发提交只各一条。"""
    req_p = make_request("r-ghost-p", patient_id="ghost")
    req_r = make_request("r-ghost-r", recipient_id="ghost-org")
    for req, expected in (
        (req_p, DenyReason.PATIENT_NOT_FOUND),
        (req_r, DenyReason.RECIPIENT_NOT_FOUND),
    ):
        d1 = decisions.evaluate(req)
        d2 = decisions.evaluate(req)
        log_decision(req, d2)
        assert d1 is d2
        assert d1.outcome == DecisionOutcome.DENY
        assert d1.reasons == (expected,)

    results: list = []

    def worker():
        results.append(decisions.evaluate(make_request("r-ghost-conc", patient_id="ghost2")))

    threads = [Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(d is results[0] for d in results)
    assert results[0].reasons == (DenyReason.PATIENT_NOT_FOUND,)

    # 三条不同请求（含并发那条）恰好三条判定、三条审计
    assert len(store.decisions) == 3
    audits = store.list_audits()
    assert len(audits) == 3
    assert {a.request.request_id for a in audits} == {
        "r-ghost-p", "r-ghost-r", "r-ghost-conc"
    }


def test_concurrent_submission_single_decision(store, consents, decisions):
    """并发提交同一请求：只产生一条判定与一条审计，结论一致。"""
    _setup_patient(store)
    consents.create(**active_consent_kwargs())
    req = make_request("r-concurrent")
    results = []

    def worker():
        results.append(decisions.evaluate(req))

    threads = [Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log_decision(req, results[0])
    assert all(d is results[0] for d in results)
    assert results[0].outcome == DecisionOutcome.ALLOW
    assert len(store.list_audits()) == 1


def test_concurrent_mixed_requests_consistent_count(store, consents, decisions):
    """并发提交不同请求：每个请求恰好一条审计，无丢失。"""
    _setup_patient(store)
    consents.create(**active_consent_kwargs())
    reqs = [make_request(f"r-mix-{i}") for i in range(20)]

    def worker(r):
        d = decisions.evaluate(r)
        log_decision(r, d)

    threads = [Thread(target=worker, args=(r,)) for r in reqs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.list_audits()) == 20
    assert len(store.decisions) == 20


def test_decision_stable_when_consent_changes(store, consents, decisions):
    """声明在请求处理期间被变更：结论基于快照，稳定且可解释。"""
    _setup_patient(store)
    consents.create(**active_consent_kwargs())
    req = make_request("r-stable")
    d1 = decisions.evaluate(req)
    # 判定后撤回声明
    consents.withdraw("c1")
    # 同一请求再次提交：结论不变（幂等），依据仍指向判定时的版本
    d2 = decisions.evaluate(req)
    log_decision(req, d2)
    assert d1 is d2
    assert d2.outcome == DecisionOutcome.ALLOW
    assert d2.consent_versions == (("c1", 1),)
    # 新请求则看到撤回后的状态
    d3 = decisions.evaluate(make_request("r-after-withdraw"))
    log_decision(make_request("r-after-withdraw"), d3)
    assert d3.reasons == (DenyReason.CONSENT_WITHDRAWN,)
