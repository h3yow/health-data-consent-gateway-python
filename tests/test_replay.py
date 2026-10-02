"""按时间点回溯判定：版本还原、撤回/未生效/过期排除、与原判定对账。"""
from __future__ import annotations

from datetime import timedelta
from threading import Thread

import pytest

from gateway.consent import ConsentService
from gateway.decision import DecisionService
from gateway.models import (
    AccessRequest,
    DataCategory,
    DecisionOutcome,
    DenyReason,
    Purpose,
)
from gateway.registry import RegistryService
from gateway.storage import GatewayStore
from tests.conftest import NOW, active_consent_kwargs, log_decision, make_request


@pytest.fixture()
def clock(controlled_clock):
    return controlled_clock


def _timeline_service(store, clock):
    """v1@NOW active；v2@NOW+1d 变更（扩大类别）；v3@NOW+3d 撤回。"""
    registry = RegistryService(store)
    consents = ConsentService(store, registry)
    decisions = DecisionService(store, consents, registry)
    consents.create(
        **active_consent_kwargs(
            valid_from=NOW - timedelta(days=30),
            valid_until=NOW + timedelta(days=30),
        )
    )
    clock.advance(days=1)
    consents.amend("c1", categories={DataCategory.DIAGNOSIS, DataCategory.LAB_RESULT})
    clock.advance(days=2)
    consents.withdraw("c1")
    clock.advance(days=1)  # 停在撤回之后，模拟“事后复核”
    return consents, decisions


def test_replay_restores_version_at_each_point(store, clock):
    _, decisions = _timeline_service(store, clock)
    req = make_request("r1")

    r_v1 = decisions.replay(req, NOW)
    assert r_v1.decision.outcome == DecisionOutcome.ALLOW
    assert r_v1.decision.consent_versions == (("c1", 1),)
    assert r_v1.evidence[0].version == 1 and r_v1.evidence[0].reason == "used"

    r_v2 = decisions.replay(req, NOW + timedelta(days=1))
    assert r_v2.decision.consent_versions == (("c1", 2),)

    r_v3 = decisions.replay(req, NOW + timedelta(days=3))
    assert r_v3.decision.outcome == DecisionOutcome.DENY
    assert r_v3.decision.reasons == (DenyReason.CONSENT_WITHDRAWN,)
    assert r_v3.decision.consent_versions == (("c1", 3),)
    assert r_v3.evidence[0].reason == "withdrawn"
    print(
        "\n[replay] v1@NOW allow; v2@+1d allow; v3@+3d withdrawn -> "
        f"{[ (e.version, e.reason) for e in r_v3.evidence ]}"
    )


def test_replay_before_creation_is_no_consent(store, clock):
    _, decisions = _timeline_service(store, clock)
    req = make_request("r-early")
    r = decisions.replay(req, NOW - timedelta(days=40))
    assert r.decision.outcome == DecisionOutcome.DENY
    assert r.decision.reasons == (DenyReason.NO_CONSENT,)
    assert r.evidence[0].reason == "not_created" and r.evidence[0].version == 0
    print(f"\n[replay] before creation: {r.decision.basis}")


def test_replay_excludes_future_and_expired(store):
    """当时还没生效 / 已经过期的声明不能作为回溯有效依据。"""
    registry = RegistryService(store)
    consents = ConsentService(store, registry)
    decisions = DecisionService(store, consents, registry)
    # 未来生效的声明：NOW 时刻尚未生效
    consents.create(
        **active_consent_kwargs(
            consent_id="c-future",
            valid_from=NOW + timedelta(days=1),
            valid_until=NOW + timedelta(days=30),
        )
    )
    req = make_request("r-future")
    r = decisions.replay(req, NOW)
    assert r.decision.outcome == DecisionOutcome.DENY
    assert r.decision.reasons == (DenyReason.CONSENT_EXPIRED,)
    assert r.evidence[0].reason == "not_yet_effective"

    # 已过期声明
    consents.create(
        **active_consent_kwargs(
            consent_id="c-exp",
            valid_from=NOW - timedelta(days=60),
            valid_until=NOW - timedelta(days=1),
        )
    )
    r2 = decisions.replay(make_request("r-exp"), NOW)
    reasons = {e.reason for e in r2.evidence}
    assert "expired" in reasons
    assert r2.decision.outcome == DecisionOutcome.DENY
    print(f"\n[replay] future/expired excluded: {[(e.consent_id, e.reason) for e in r2.evidence]}")


def test_replay_matches_original_and_stable_after_later_changes(store, clock):
    """原请求在 v1 期间判定 ALLOW；之后变更撤回；
    按 submitted_at 回溯仍是 ALLOW、依据指向 v1，且 matches_original 为真。"""
    consents, decisions = _timeline_service(store, clock)
    req = make_request("r-original")
    original = decisions.evaluate(req)
    log_decision(req, original)
    assert original.outcome == DecisionOutcome.ALLOW
    assert original.consent_versions == (("c1", 1),)

    # 时间已推进到撤回之后（历史在 NOW+3d 完成），重放 NOW 时刻
    r = decisions.replay(req, NOW)
    assert r.decision.outcome == DecisionOutcome.ALLOW
    assert r.decision.consent_versions == (("c1", 1),)
    assert r.matches_original is True
    print(f"\n[replay] post-withdraw replay at submitted_at: {r.decision.basis}")

    # 已撤回声明不可再变更（保持既有兼容语义）
    import pytest
    with pytest.raises(ValueError):
        consents.amend("c1", categories={DataCategory.GENOMIC})
    # 历史继续存在但不再增长，回溯结论仍稳定指向 v1
    r2 = decisions.replay(req, NOW)
    assert r2.decision.outcome == DecisionOutcome.ALLOW
    assert r2.decision.consent_versions == (("c1", 1),)
    assert [h.version for h in consents.history("c1")] == [1, 2, 3]


def test_replay_of_denied_request_reconciles(store, clock):
    consents, decisions = _timeline_service(store, clock)
    # 在撤回后提交的真实请求：DENY withdrawn
    req = make_request("r-denied")
    # 用撤回后的提交时间
    from gateway.models import AccessRequest
    req = AccessRequest(
        request_id="r-denied",
        patient_id="p1",
        recipient_id="hosp-a",
        purpose=Purpose.TREATMENT,
        categories=frozenset({DataCategory.DIAGNOSIS}),
        submitted_at=NOW + timedelta(days=4),
    )
    decision = decisions.evaluate(req)
    log_decision(req, decision)
    assert decision.outcome == DecisionOutcome.DENY
    r = decisions.replay(req, NOW + timedelta(days=4))
    assert r.decision.outcome == DecisionOutcome.DENY
    assert r.matches_original is True
    assert r.decision.consent_versions == (("c1", 3),)
    print(f"\n[replay] denied request reconciled: {r.decision.outcome} / {r.decision.reasons}")


def test_replay_concurrent_changes_remain_stable(store, clock):
    """回溯进行期间声明被并发变更：每次回溯结论只依赖历史快照，结论稳定。

    变更作用在另一条独立声明上，模拟「判定之后声明又被改了」的并发环境；
    对固定时间点 NOW 的回溯始终还原到 c1@v1 放行。
    """
    consents, decisions = _timeline_service(store, clock)
    # 再建一条会被并发变更的无关声明
    consents.create(
        **active_consent_kwargs(
            consent_id="c2",
            recipients={"lab-b"},
            valid_from=NOW - timedelta(days=30),
            valid_until=NOW + timedelta(days=30),
        )
    )
    req = make_request("r-con")
    decisions.evaluate(req)
    results = []

    def replay_worker():
        r = decisions.replay(req, NOW)
        results.append((r.decision.outcome, r.decision.consent_versions, r.matches_original))

    def amend_worker():
        try:
            consents.amend("c2", purposes={Purpose.TREATMENT, Purpose.RESEARCH})
        except ValueError:
            pass

    threads = [Thread(target=replay_worker) for _ in range(8)]
    threads += [Thread(target=amend_worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 固定时间点 NOW 的回溯：全部还原到 v1 放行
    assert results
    assert all(
        outcome == DecisionOutcome.ALLOW and versions == (("c1", 1),) and matches
        for outcome, versions, matches in results
    ), results
    print(f"\n[replay] {len(results)} concurrent replays stable at c1@v1 allow")
