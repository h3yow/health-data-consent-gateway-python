"""HTTP 层冒烟测试：同意 -> 判定 -> 审计 -> 发布。"""
from datetime import timedelta

from tests.conftest import NOW as _NOW
NOW = _NOW.astimezone()

import pytest
from fastapi.testclient import TestClient

from main import app, store
from gateway.models import Institution, Patient



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
