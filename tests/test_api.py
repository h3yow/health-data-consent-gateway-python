"""HTTP 层冒烟测试：同意 -> 判定 -> 审计 -> 发布。"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from main import app, store
from gateway.models import Institution, Patient

NOW = datetime.now().astimezone()


@pytest.fixture()
def client():
    store.institutions.clear()
    store.patients.clear()
    store.consents.clear()
    store.consent_history.clear()
    store.decisions.clear()
    store.audits.clear()
    store.add_institution(Institution("hosp-a", "医院A"))
    store.add_patient(Patient("p1", "张三", 1980, diagnosis="flu"))
    return TestClient(app)


def test_full_flow(client: TestClient):
    resp = client.post("/consents", json={
        "consent_id": "c1",
        "patient_id": "p1",
        "purposes": ["treatment"],
        "categories": ["diagnosis"],
        "recipients": ["hosp-a"],
        "valid_from": (NOW - timedelta(days=1)).isoformat(),
        "valid_until": (NOW + timedelta(days=30)).isoformat(),
    })
    assert resp.status_code == 201, resp.text

    body = {
        "request_id": "r1",
        "patient_id": "p1",
        "recipient_id": "hosp-a",
        "purpose": "treatment",
        "categories": ["diagnosis"],
    }
    resp = client.post("/access-requests", json=body)
    assert resp.status_code == 200
    assert resp.json()["allowed"] is True
    print(f"\n[api] decision={resp.json()}")

    # 重复提交同一 request_id：幂等，审计仍只有一条
    resp = client.post("/access-requests", json=body)
    assert resp.json()["allowed"] is True
    audits = client.get("/audits").json()
    assert len(audits) == 1
    assert audits[0]["outcome"] == "allow"

    # 撤回后新请求被拒，原因可区分
    resp = client.post("/consents/c1/withdraw")
    assert resp.status_code == 200
    body["request_id"] = "r2"
    resp = client.post("/access-requests", json=body)
    assert resp.json()["allowed"] is False
    assert "consent_withdrawn" in resp.json()["reasons"]
    print(f"[api] after withdraw: {resp.json()['reasons']}")


def test_deny_out_of_scope(client: TestClient):
    client.post("/consents", json={
        "consent_id": "c1",
        "patient_id": "p1",
        "purposes": ["treatment"],
        "categories": ["diagnosis"],
        "recipients": ["hosp-a"],
        "valid_from": (NOW - timedelta(days=1)).isoformat(),
        "valid_until": (NOW + timedelta(days=30)).isoformat(),
    })
    resp = client.post("/access-requests", json={
        "request_id": "r-scope",
        "patient_id": "p1",
        "recipient_id": "hosp-a",
        "purpose": "research",
        "categories": ["diagnosis"],
    })
    assert resp.json()["reasons"] == ["purpose_not_covered"]


def _consent_body(**overrides):
    body = {
        "consent_id": "c1",
        "patient_id": "p1",
        "purposes": ["treatment"],
        "categories": ["diagnosis"],
        "recipients": ["hosp-a"],
        "valid_from": (NOW - timedelta(days=1)).isoformat(),
        "valid_until": (NOW + timedelta(days=30)).isoformat(),
    }
    body.update(overrides)
    return body


def test_subject_registration_api(client: TestClient):
    # 起服务后只用接口即可登记机构与患者，并可查回
    resp = client.post("/institutions", json={"institution_id": "lab-x", "name": "检验所X"})
    assert resp.status_code == 201
    resp = client.post("/patients", json={
        "patient_id": "p9", "name": "赵六", "birth_year": 1975,
        "diagnosis": "flu", "lab_group": "O-",
    })
    assert resp.status_code == 201

    got = client.get("/patients/p9").json()
    assert got["diagnosis"] == "flu" and got["lab_group"] == "O-" and got["birth_year"] == 1975
    assert client.get("/institutions/lab-x").json()["name"] == "检验所X"
    assert any(p["patient_id"] == "p9" for p in client.get("/patients").json())

    # 同一 ID 重复登记：明确 409，原记录不被覆盖
    resp = client.post("/patients", json={"patient_id": "p9", "name": "冒名", "birth_year": 2000})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "patient_already_exists"
    resp = client.post("/institutions", json={"institution_id": "lab-x", "name": "冒名机构"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "institution_already_exists"
    assert client.get("/patients/p9").json()["name"] == "赵六"

    # 查询不存在的主体：404 且能分清是患者还是机构
    assert client.get("/patients/ghost").status_code == 404
    assert client.get("/patients/ghost").json()["detail"] == "patient_not_found"
    assert client.get("/institutions/ghost").json()["detail"] == "institution_not_found"
    print("\n[api] subject registration + duplicate conflict OK")


def test_consent_create_validates_subjects(client: TestClient):
    # 患者未登记：写入那一刻 422，能分清是患者不存在
    resp = client.post("/consents", json=_consent_body(patient_id="ghost"))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "patient_not_found"

    # 接收方机构未登记：同样写入时失败
    resp = client.post("/consents", json=_consent_body(recipients=["ghost-org"]))
    assert resp.status_code == 422
    assert resp.json()["detail"] == "recipient_not_found"

    # 同一 consent_id 重复建立：409
    assert client.post("/consents", json=_consent_body()).status_code == 201
    resp = client.post("/consents", json=_consent_body())
    assert resp.status_code == 409
    assert resp.json()["detail"] == "consent_already_exists"
    print("\n[api] consent write-time validation OK")


def test_access_request_validates_subjects(client: TestClient):
    # 提交那一刻就失败，不落判定与审计
    resp = client.post("/access-requests", json={
        "request_id": "r-ghost-p", "patient_id": "ghost",
        "recipient_id": "hosp-a", "purpose": "treatment", "categories": ["diagnosis"],
    })
    assert resp.status_code == 422
    assert resp.json()["detail"] == "patient_not_found"

    resp = client.post("/access-requests", json={
        "request_id": "r-ghost-r", "patient_id": "p1",
        "recipient_id": "ghost-org", "purpose": "treatment", "categories": ["diagnosis"],
    })
    assert resp.status_code == 422
    assert resp.json()["detail"] == "recipient_not_found"

    assert client.get("/audits").json() == []
    print("\n[api] access-request submit-time validation OK, no dirty audits")


def test_consent_history_and_review_api(client: TestClient):
    assert client.post("/consents", json=_consent_body()).status_code == 201
    client.patch("/consents/c1", json={"categories": ["diagnosis", "lab_result"]})

    body = {
        "request_id": "r-review", "patient_id": "p1", "recipient_id": "hosp-a",
        "purpose": "treatment", "categories": ["diagnosis"],
    }
    resp = client.post("/access-requests", json=body)
    assert resp.json()["allowed"] is True

    client.post("/consents/c1/withdraw")  # 判定之后撤回

    # 历史：建立、变更、撤回各一条，版本与状态轨迹完整
    history = client.get("/consents/c1/history").json()
    assert [h["version"] for h in history] == [1, 2, 3]
    assert [h["change_type"] for h in history] == ["created", "amended", "withdrawn"]
    assert [h["status"] for h in history] == ["active", "active", "withdrawn"]
    print(f"\n[api] history={[(h['version'], h['change_type']) for h in history]}")

    # 回溯复核：按提交时刻还原，结论与当时真实判定一致
    review = client.get("/access-requests/r-review/review").json()
    assert review["consistent"] is True
    assert review["original"]["outcome"] == "allow"
    assert review["retrospective"]["outcome"] == "allow"
    # 提交请求时声明已是 v2，回溯依据指向那一刻的版本
    assert review["retrospective"]["consent_versions"] == [["c1", 2]]
    print(f"[api] review consistent={review['consistent']} "
          f"basis={review['retrospective']['basis']}")

    # 未知资源
    assert client.get("/consents/ghost/history").status_code == 404
    assert client.get("/access-requests/ghost/review").status_code == 404
