"""同意历史与按时间点回溯判定。"""
from datetime import datetime, timedelta
from threading import Thread

import pytest

from gateway.consent import ConsentNotFoundError, ConsentService
from gateway.decision import DecisionService
from gateway.models import (
    AccessRequest,
    ChangeType,
    ConsentStatus,
    DataCategory,
    DecisionOutcome,
    DenyReason,
    Purpose,
)
from tests.conftest import active_consent_kwargs, log_decision


def _now() -> datetime:
    return datetime.now().astimezone()


def _request(request_id: str, submitted_at: datetime, **overrides) -> AccessRequest:
    kwargs = {
        "request_id": request_id,
        "patient_id": "p1",
        "recipient_id": "hosp-a",
        "purpose": Purpose.TREATMENT,
        "categories": frozenset({DataCategory.DIAGNOSIS}),
        "submitted_at": submitted_at,
    }
    kwargs.update(overrides)
    return AccessRequest(**kwargs)


def _create_active(consents: ConsentService, consent_id: str = "c1", **overrides):
    now = _now()
    kwargs = active_consent_kwargs(
        consent_id=consent_id,
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=30),
    )
    kwargs.update(overrides)
    return consents.create(**kwargs)


def test_history_records_create_amend_withdraw(consents: ConsentService):
    _create_active(consents)
    consents.amend("c1", categories={DataCategory.DIAGNOSIS, DataCategory.LAB_RESULT})
    consents.withdraw("c1")

    history = consents.history("c1")
    assert [e.version for e in history] == [1, 2, 3]
    assert [e.change_type for e in history] == [
        ChangeType.CREATED,
        ChangeType.AMENDED,
        ChangeType.WITHDRAWN,
    ]
    assert [e.status for e in history] == [
        ConsentStatus.ACTIVE,
        ConsentStatus.ACTIVE,
        ConsentStatus.WITHDRAWN,
    ]
    # 时间单调、内容可还原：v2 能看到变更后的类别
    assert history[0].changed_at <= history[1].changed_at <= history[2].changed_at
    assert DataCategory.LAB_RESULT in history[1].categories
    assert DataCategory.LAB_RESULT not in history[0].categories
    for e in history:
        print(f"\n[history] {e.consent_id} v{e.version} {e.change_type} "
              f"status={e.status} changed_at={e.changed_at.isoformat()}")


def test_history_missing_consent(consents: ConsentService):
    with pytest.raises(ConsentNotFoundError):
        consents.history("nope")


def test_retrospective_matches_original_decision(
    store, consents: ConsentService, decisions: DecisionService
):
    """提交时放行；之后撤回声明；按提交时刻回溯，结论仍与当时一致。"""
    _create_active(consents)
    req = _request("r-review", _now())
    original = decisions.evaluate(req)
    assert original.outcome == DecisionOutcome.ALLOW

    consents.withdraw("c1")  # 判定之后声明被撤回

    record, retrospective = decisions.review("r-review")
    log_decision(req, retrospective)
    assert record.decision.outcome == DecisionOutcome.ALLOW
    assert retrospective.outcome == DecisionOutcome.ALLOW
    assert retrospective.consent_versions == (("c1", 1),)
    assert "c1@v1" in retrospective.basis
    # 而按当前状态判定则是拒绝：回溯确实还原了「那一刻」
    current = decisions.evaluate(_request("r-current", _now()))
    assert current.reasons == (DenyReason.CONSENT_WITHDRAWN,)


def test_retrospective_uses_version_at_that_time(
    store, consents: ConsentService, decisions: DecisionService
):
    """当时 v1 不覆盖用途被拒；之后变更为覆盖。回溯仍拒绝并指向 v1。"""
    _create_active(consents, purposes={Purpose.RESEARCH})
    req = _request("r-ver", _now())
    original = decisions.evaluate(req)
    assert original.reasons == (DenyReason.PURPOSE_NOT_COVERED,)

    consents.amend("c1", purposes={Purpose.TREATMENT})  # 后来才覆盖

    record, retrospective = decisions.review("r-ver")
    log_decision(req, retrospective)
    assert retrospective.outcome == DecisionOutcome.DENY
    assert retrospective.reasons == original.reasons
    # 覆盖维度不匹配时判定依据不含命中声明，回溯与原始判定一致
    assert retrospective.consent_versions == original.consent_versions
    # 新请求按当前状态放行，依据是 v2
    now_req = _request("r-ver-now", _now())
    d = decisions.evaluate(now_req)
    assert d.outcome == DecisionOutcome.ALLOW
    assert d.consent_versions == (("c1", 2),)


def test_evaluate_at_explicit_times(store, consents: ConsentService, decisions: DecisionService):
    """显式时间点回溯：建立前无声明；已过期/未生效的声明不作有效依据。"""
    created = _create_active(consents)
    req = _request("r-t", created.updated_at)

    # 声明建立之前：无同意
    before = decisions.evaluate_at(req, created.updated_at - timedelta(seconds=1))
    log_decision(req, before)
    assert before.reasons == (DenyReason.NO_CONSENT,)

    # 有效期内：放行
    during = decisions.evaluate_at(req, created.updated_at + timedelta(days=1))
    assert during.outcome == DecisionOutcome.ALLOW

    # 有效期之后：过期拒绝
    after = decisions.evaluate_at(req, created.valid_until + timedelta(seconds=1))
    log_decision(req, after)
    assert after.reasons == (DenyReason.CONSENT_EXPIRED,)

    # 尚未生效的未来声明：在生效前的时间点不作依据
    future = _create_active(
        consents,
        consent_id="c-future",
        valid_from=_now() + timedelta(days=10),
        valid_until=_now() + timedelta(days=40),
    )
    not_yet = decisions.evaluate_at(
        _request("r-future", future.updated_at), future.valid_from - timedelta(seconds=1)
    )
    assert not_yet.outcome == DecisionOutcome.ALLOW  # c1 仍有效，放行不依赖未来声明
    assert ("c-future", 1) not in not_yet.consent_versions


def test_retrospective_excludes_withdrawn_at_that_time(
    store, consents: ConsentService, decisions: DecisionService
):
    """提交请求时声明已撤回：回溯同样拒绝，且依据指向撤回后的版本。"""
    _create_active(consents)
    consents.withdraw("c1")
    req = _request("r-wd", _now())
    original = decisions.evaluate(req)
    assert original.reasons == (DenyReason.CONSENT_WITHDRAWN,)

    record, retrospective = decisions.review("r-wd")
    log_decision(req, retrospective)
    assert retrospective.outcome == DecisionOutcome.DENY
    assert retrospective.reasons == (DenyReason.CONSENT_WITHDRAWN,)
    assert retrospective.consent_versions == (("c1", 2),)  # 撤回后的版本


def test_review_unknown_request(decisions: DecisionService):
    assert decisions.review("ghost-request") is None


def test_concurrent_amend_withdraw_history_complete(store, consents: ConsentService):
    """并发变更/撤回同一声明：版本严格递增无重复无缺口，历史一条不丢。"""
    _create_active(consents)
    n = 20

    def worker(i: int):
        try:
            if i % 2 == 0:
                consents.amend("c1", categories={DataCategory.DIAGNOSIS})
            else:
                consents.withdraw("c1")
        except ValueError:
            pass  # 已撤回后不可再变更，属预期拒绝

    threads = [Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    history = consents.history("c1")
    versions = [e.version for e in history]
    # 版本从 1 开始严格连续递增：无重复、无缺口、无丢失
    assert versions == list(range(1, len(history) + 1))
    assert store.get_consent("c1").version == len(history)
    # 每次变更都留下一条历史
    assert all(e.consent_id == "c1" for e in history)
    print(f"\n[history] concurrent ops -> {len(history)} entries, "
          f"final=v{store.get_consent('c1').version} status={store.get_consent('c1').status}")


def test_concurrent_create_same_consent_id(store, consents: ConsentService):
    """并发建立同一 consent_id：恰好一个成功，历史只有一条 CREATED。"""
    created: list[str] = []

    def worker():
        try:
            consents.create(**active_consent_kwargs(
                valid_from=_now() - timedelta(days=1),
                valid_until=_now() + timedelta(days=30),
            ))
            created.append("ok")
        except Exception:
            created.append("conflict")

    threads = [Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert created.count("ok") == 1
    history = consents.history("c1")
    assert len(history) == 1 and history[0].change_type == ChangeType.CREATED
    print(f"\n[history] concurrent create: ok={created.count('ok')} conflict={created.count('conflict')}")
