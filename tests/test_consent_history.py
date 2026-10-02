"""同意声明：写入时主体校验、历史留痕（建立/变更/撤回）与并发版本安全。"""
from __future__ import annotations

from datetime import timedelta
from threading import Thread

import pytest

from gateway.consent import ConsentService, DuplicateConsentError
from gateway.models import ConsentHistoryAction, DataCategory, Purpose
from gateway.registry import (
    InstitutionNotFoundError,
    PatientNotFoundError,
    RegistryService,
)
from gateway.storage import GatewayStore
from tests.conftest import NOW, active_consent_kwargs


@pytest.fixture()
def clock(controlled_clock):
    return controlled_clock


def test_create_rejects_unknown_patient(consents: ConsentService, store: GatewayStore):
    with pytest.raises(PatientNotFoundError):
        consents.create(**active_consent_kwargs(patient_id="ghost"))
    # 脏引用没有落库
    assert store.get_consent("c1") is None
    print("\n[consent] create with unregistered patient rejected at write time")


def test_create_rejects_unknown_recipient(consents: ConsentService):
    with pytest.raises(InstitutionNotFoundError):
        consents.create(**active_consent_kwargs(recipients={"ghost-org"}))
    print("\n[consent] create with unregistered recipient rejected at write time")


def test_duplicate_consent_id_rejected(consents: ConsentService):
    consents.create(**active_consent_kwargs())
    with pytest.raises(DuplicateConsentError):
        consents.create(**active_consent_kwargs())
    print("\n[consent] duplicate consent_id rejected")


def test_amend_new_recipient_must_exist(consents: ConsentService):
    consents.create(**active_consent_kwargs())
    with pytest.raises(InstitutionNotFoundError):
        consents.amend("c1", recipients={"ghost-org"})
    # 失败不留新版本、不留历史
    assert consents.history("c1")[-1].version == 1
    print("\n[consent] amend referencing unknown institution leaves no new version")


def test_full_history_created_amended_withdrawn(consents: ConsentService, clock):
    consents.create(**active_consent_kwargs())
    clock.advance(hours=2)
    consents.amend("c1", categories={DataCategory.DIAGNOSIS, DataCategory.LAB_RESULT})
    clock.advance(hours=3)
    consents.withdraw("c1")

    history = consents.history("c1")
    assert [h.action for h in history] == [
        ConsentHistoryAction.CREATED,
        ConsentHistoryAction.AMENDED,
        ConsentHistoryAction.WITHDRAWN,
    ]
    assert [h.version for h in history] == [1, 2, 3]
    assert history[0].changed_at == NOW
    assert history[1].changed_at == NOW + timedelta(hours=2)
    assert history[2].changed_at == NOW + timedelta(hours=5)
    # 每条历史都能看出改成了什么
    assert DataCategory.LAB_RESULT in history[1].statement.categories
    assert history[2].statement.status.value == "withdrawn"
    print(
        "\n[consent] history timeline: "
        + " | ".join(f"v{h.version}:{h.action.value}@{h.changed_at.isoformat()}" for h in history)
    )


def test_concurrent_amends_versions_contiguous(consents: ConsentService):
    """并发变更同一声明：版本连续不重复、历史不丢失。"""
    consents.create(**active_consent_kwargs())

    def worker(i):
        consents.amend("c1", categories={DataCategory.DIAGNOSIS, DataCategory.LAB_RESULT})

    threads = [Thread(target=worker, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    history = consents.history("c1")
    assert [h.version for h in history] == list(range(1, 14))
    assert consents._store.get_consent("c1").version == 13
    print(f"\n[consent] 12 concurrent amends -> versions {[h.version for h in history]}")


def test_concurrent_create_single_statement(store: GatewayStore):
    """并发建立同一 consent_id：只成功一条，其余明确冲突。"""
    service = ConsentService(store, RegistryService(store))
    outcomes = []

    def worker():
        try:
            service.create(**active_consent_kwargs())
            outcomes.append("created")
        except DuplicateConsentError:
            outcomes.append("conflict")

    threads = [Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("created") == 1
    assert outcomes.count("conflict") == 15
    assert len(service.history("c1")) == 1
    print(f"\n[consent] concurrent creates: 1 created, 15 conflicts, single history entry")


def test_concurrent_withdraw_and_amend_no_lost_history(consents: ConsentService):
    """撤回与变更并发：要么变更加撤回（v2,v3），要么直接撤回（v2），
    但版本必须连续、历史条数与最终版本一致、无异常外逃。"""
    consents.create(**active_consent_kwargs())
    errors = []

    def amend():
        try:
            consents.amend("c1", purposes={Purpose.TREATMENT, Purpose.RESEARCH})
        except ValueError as exc:
            errors.append(("amend", str(exc)))

    def withdraw():
        try:
            consents.withdraw("c1")
        except Exception as exc:  # noqa: BLE001
            errors.append(("withdraw", str(exc)))

    t1 = Thread(target=amend)
    t2 = Thread(target=withdraw)
    t1.start(); t2.start()
    t1.join(); t2.join()

    history = consents.history("c1")
    versions = [h.version for h in history]
    assert versions == list(range(1, len(versions) + 1))
    assert not errors
    final = consents._store.get_consent("c1")
    assert final.version == len(history)
    print(f"\n[consent] concurrent amend/withdraw -> {[(h.version, h.action.value) for h in history]}")
