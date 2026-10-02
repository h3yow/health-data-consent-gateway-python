"""主体登记：机构/患者的登记、查询、重复冲突与写入时校验。"""
from threading import Thread

import pytest

from gateway.consent import (
    ConsentAlreadyExistsError,
    ConsentService,
    PatientNotRegisteredError,
    RecipientNotRegisteredError,
)
from gateway.models import Institution, Patient
from gateway.storage import GatewayStore
from gateway.subjects import (
    SubjectAlreadyExistsError,
    SubjectNotFoundError,
    SubjectService,
)
from tests.conftest import active_consent_kwargs


@pytest.fixture()
def subjects(store: GatewayStore) -> SubjectService:
    return SubjectService(store)


def test_register_and_get_institution(subjects: SubjectService):
    inst = subjects.register_institution("hosp-x", "医院X")
    assert subjects.get_institution("hosp-x") == inst
    assert inst in subjects.list_institutions()
    print(f"\n[subject] registered institution={inst}")


def test_register_and_get_patient_with_group_attrs(subjects: SubjectService):
    p = subjects.register_patient(
        "p-100", "王五", 1992, diagnosis="flu", lab_group="A+"
    )
    got = subjects.get_patient("p-100")
    assert got == p
    # 分组属性随登记保存，供发布统计使用
    assert got.diagnosis == "flu" and got.lab_group == "A+" and got.birth_year == 1992
    print(f"\n[subject] registered patient={got}")


def test_duplicate_registration_conflict(subjects: SubjectService):
    subjects.register_institution("hosp-x", "医院X")
    subjects.register_patient("p-100", "王五", 1992)
    with pytest.raises(SubjectAlreadyExistsError) as ei:
        subjects.register_institution("hosp-x", "冒名医院")
    assert ei.value.kind == "institution"
    with pytest.raises(SubjectAlreadyExistsError) as ep:
        subjects.register_patient("p-100", "冒名患者", 2000)
    assert ep.value.kind == "patient"
    # 原记录不被覆盖，不会出现两条互相打架的记录
    assert subjects.get_institution("hosp-x").name == "医院X"
    assert subjects.get_patient("p-100").name == "王五"
    assert [i for i in subjects.list_institutions() if i.institution_id == "hosp-x"] == [
        Institution("hosp-x", "医院X")
    ]
    assert [p for p in subjects.list_patients() if p.patient_id == "p-100"] == [
        Patient("p-100", "王五", 1992)
    ]
    print("\n[subject] duplicate registration rejected, original records intact")


def test_get_missing_subject(subjects: SubjectService):
    with pytest.raises(SubjectNotFoundError) as ei:
        subjects.get_institution("ghost")
    assert ei.value.kind == "institution"
    with pytest.raises(SubjectNotFoundError) as ep:
        subjects.get_patient("ghost")
    assert ep.value.kind == "patient"


def test_concurrent_registration_single_record(subjects: SubjectService):
    """并发登记同一 ID：恰好一个成功，其余冲突，库里只有一条记录。"""
    outcomes: list[str] = []

    def worker(i: int):
        try:
            subjects.register_patient("p-race", f"患者{i}", 1980 + i)
            outcomes.append("ok")
        except SubjectAlreadyExistsError:
            outcomes.append("conflict")

    threads = [Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 15
    assert len(subjects.list_patients()) == 1
    print(f"\n[subject] concurrent register: ok={outcomes.count('ok')} conflict={outcomes.count('conflict')}")


def test_consent_create_rejects_unregistered_patient(store: GatewayStore):
    """拿不存在的患者建声明：写入那一刻就失败，不落脏数据。"""
    consents = ConsentService(store)
    with pytest.raises(PatientNotRegisteredError) as exc:
        consents.create(**active_consent_kwargs(patient_id="ghost"))
    assert exc.value.patient_id == "ghost"
    assert store.consents == {}
    print(f"\n[subject] consent for ghost patient rejected: {exc.value}")


def test_consent_create_rejects_unregistered_recipient(store: GatewayStore):
    store.add_patient(Patient("p1", "张三", 1980))
    consents = ConsentService(store)
    with pytest.raises(RecipientNotRegisteredError) as exc:
        consents.create(**active_consent_kwargs(recipients={"ghost-org"}))
    assert exc.value.institution_id == "ghost-org"
    assert store.consents == {}
    print(f"\n[subject] consent for ghost recipient rejected: {exc.value}")


def test_consent_duplicate_id_conflict(store: GatewayStore):
    store.add_patient(Patient("p1", "张三", 1980))
    consents = ConsentService(store)
    consents.create(**active_consent_kwargs())
    with pytest.raises(ConsentAlreadyExistsError):
        consents.create(**active_consent_kwargs())
    assert len(store.consents) == 1
    assert store.get_consent("c1").version == 1


def test_amend_rejects_unregistered_recipient(store: GatewayStore):
    store.add_patient(Patient("p1", "张三", 1980))
    consents = ConsentService(store)
    consents.create(**active_consent_kwargs())
    with pytest.raises(RecipientNotRegisteredError):
        consents.amend("c1", recipients={"ghost-org"})
    # 变更未生效，仍是 v1
    assert store.get_consent("c1").version == 1
