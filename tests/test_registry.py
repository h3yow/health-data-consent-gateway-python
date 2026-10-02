"""主体登记：机构/患者登记、查询、重复登记冲突与引用校验。"""
from __future__ import annotations

import pytest

from gateway.models import Institution, Patient
from gateway.registry import (
    InstitutionNotFoundError,
    PatientNotFoundError,
    RegistryService,
    SubjectAlreadyExistsError,
)
from gateway.storage import GatewayStore


@pytest.fixture()
def registry(store: GatewayStore) -> RegistryService:
    return RegistryService(store)


def test_register_and_get_institution(registry: RegistryService):
    inst, created = registry.register_institution("hosp-x", "医院X")
    assert created is True
    assert registry.get_institution("hosp-x") == inst
    print(f"\n[registry] institution registered: {inst}")


def test_register_patient_with_group_attributes(registry: RegistryService):
    patient, created = registry.register_patient(
        "p9", "王五", 1976, diagnosis="diabetes", lab_group="cohort-a"
    )
    assert created is True
    fetched = registry.get_patient("p9")
    assert fetched == patient
    # 发布统计分组属性随登记可查
    assert fetched.diagnosis == "diabetes"
    assert fetched.lab_group == "cohort-a"
    print(f"\n[registry] patient registered with groups: {fetched}")


def test_duplicate_registration_idempotent_same_content(registry: RegistryService):
    registry.register_institution("dup-h", "同名")
    existing, created = registry.register_institution("dup-h", "同名")
    assert created is False
    assert existing.name == "同名"
    assert sum(1 for i in registry.list_institutions() if i.institution_id == "dup-h") == 1
    print("\n[registry] duplicate same-content registration is idempotent")


def test_duplicate_registration_conflict_fails(registry: RegistryService):
    registry.register_patient("dup-p", "张三", 1980, diagnosis="flu")
    with pytest.raises(SubjectAlreadyExistsError) as exc:
        registry.register_patient("dup-p", "另一个人", 1990, diagnosis="cold")
    assert exc.value.kind == "patient"
    # 冲突登记不覆盖，旧记录保持一致，不会出现两条互相打架的记录
    assert registry.get_patient("dup-p") == Patient("dup-p", "张三", 1980, diagnosis="flu")
    print("\n[registry] conflicting duplicate registration rejected, old record kept")


def test_require_references_distinguishable(registry: RegistryService):
    registry.register_patient("ref-p", "张三", 1980)
    registry.register_institution("ref-h", "H")
    assert registry.require_patient("ref-p").patient_id == "ref-p"
    assert registry.require_institution("ref-h").institution_id == "ref-h"
    with pytest.raises(PatientNotFoundError) as pe:
        registry.require_patient("ghost")
    with pytest.raises(InstitutionNotFoundError) as ie:
        registry.require_institution("ghost-org")
    assert pe.value.patient_id == "ghost"
    assert ie.value.institution_id == "ghost-org"
    print("\n[registry] patient vs institution missing refs are distinguishable")


def test_concurrent_registration_single_record():
    """同一主体 ID 并发登记：只有一条记录，不出现重复或覆盖混乱。"""
    store = GatewayStore()
    registry = RegistryService(store)
    from threading import Thread

    def worker():
        registry.register_patient("pc", "并发患者", 2000, lab_group="g1")

    threads = [Thread(target=worker) for _ in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    patients = registry.list_patients()
    assert len(patients) == 1
    assert patients[0].patient_id == "pc"
    print(f"\n[registry] concurrent registrations collapsed into: {patients[0]}")
