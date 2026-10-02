"""HTTP 层新能力：主体登记、写入校验、历史查询与按时间点回溯。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from main import app, store
from tests.conftest import NOW as _NOW

NOW = _NOW  # 与受控时钟完全同一对象（同时区表示），避免边界比较歧义


@pytest.fixture()
def client():
    store.institutions.clear()
    store.patients.clear()
    store.consents.clear()
    store.consent_history.clear()
    store.decisions.clear()
    store.audits.clear()
    return TestClient(app)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _register_subjects(client: TestClient):
    r = client.post("/institutions", json={"institution_id": "hosp-a", "name": "医院A"})
    assert r.status_code == 201 and r.json()["created"] is True
    r = client.post(
        "/patients",
        json={
            "patient_id": "p1",
            "name": "张三",
            "birth_year": 1980,
            "diagnosis": "flu",
            "lab_group": "cohort-a",
        },
    )
    assert r.status_code == 201 and r.json()["created"] is True


def test_register_subjects_and_query(client: TestClient):
    _register_subjects(client)
    assert client.get("/institutions/hosp-a").json()["name"] == "医院A"
    patient = client.get("/patients/p1").json()
    assert patient["diagnosis"] == "flu" and patient["lab_group"] == "cohort-a"
    assert any(p["patient_id"] == "p1" for p in client.get("/patients").json())
    print(f"\n[api-v2] registrations queryable: {patient}")


def test_duplicate_registration_semantics(client: TestClient):
    _register_subjects(client)
    # 同内容重复登记：幂等 201 + created=false
    r = client.post("/institutions", json={"institution_id": "hosp-a", "name": "医院A"})
    assert r.status_code == 201 and r.json()["created"] is False
    # 内容冲突：409
    r = client.post("/patients", json={"patient_id": "p1", "name": "李四", "birth_year": 1990})
    assert r.status_code == 409 and r.json()["detail"] == "patient_id_conflict"
    # 查不到的主体：404
    assert client.get("/patients/nope").status_code == 404
    print("\n[api-v2] duplicate registration: idempotent vs 409 conflict")


def test_consent_write_rejects_missing_subjects(client: TestClient):
    _register_subjects(client)
    body = {
        "consent_id": "c1",
        "patient_id": "ghost",
        "purposes": ["treatment"],
        "categories": ["diagnosis"],
        "recipients": ["hosp-a"],
        "valid_from": _iso(NOW - timedelta(days=1)),
        "valid_until": _iso(NOW + timedelta(days=30)),
    }
    r = client.post("/consents", json=body)
    assert r.status_code == 422 and r.json()["detail"] == "patient_not_found"
    body["patient_id"] = "p1"
    body["recipients"] = ["ghost-org"]
    r = client.post("/consents", json=body)
    assert r.status_code == 422 and r.json()["detail"] == "institution_not_found"
    # 脏数据没有落库
    assert client.get("/consents/c1/history").status_code == 404
    print("\n[api-v2] consent write with missing subject rejected at write time")


def test_history_and_point_in_time_replay(client: TestClient, controlled_clock):
    _register_subjects(client)
    clock = controlled_clock
    # v1：建立（changed_at = NOW）
    client.post(
        "/consents",
        json={
            "consent_id": "c1",
            "patient_id": "p1",
            "purposes": ["treatment"],
            "categories": ["diagnosis"],
            "recipients": ["hosp-a"],
            "valid_from": _iso(NOW - timedelta(days=30)),
            "valid_until": _iso(NOW + timedelta(days=30)),
        },
    )
    # 提交请求 r1（此刻放行）
    req_body = {
        "request_id": "r1",
        "patient_id": "p1",
        "recipient_id": "hosp-a",
        "purpose": "treatment",
        "categories": ["diagnosis"],
    }
    r = client.post("/access-requests", json=req_body)
    assert r.json()["allowed"] is True

    # v2：变更（次日）；v3：撤回（再两日）——历史时间线必须真实推进
    clock.advance(days=1)
    client.patch("/consents/c1", json={"categories": ["diagnosis", "lab_result"]})
    clock.advance(days=2)
    client.post("/consents/c1/withdraw")

    history = client.get("/consents/c1/history").json()
    assert [h["action"] for h in history] == ["created", "amended", "withdrawn"]
    assert [h["version"] for h in history] == [1, 2, 3]
    print(f"\n[api-v2] consent history: {[(h['version'], h['action']) for h in history]}")

    # 撤回后的新请求：拒绝（保持兼容语义）
    req_body["request_id"] = "r2"
    r = client.post("/access-requests", json=req_body)
    assert r.json()["allowed"] is False
    assert "consent_withdrawn" in r.json()["reasons"]

    # 回溯 r1：按其提交时刻还原为 v1 放行，与原判定一致
    r = client.post(
        f"/access-requests/r1/replay", json={"as_of": _iso(NOW)}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "allow"
    assert body["consent_versions"] == [{"consent_id": "c1", "version": 1}]
    assert body["matches_original"] is True
    used = [e for e in body["evidence"] if e["consent_id"] == "c1"]
    assert used[0]["version"] == 1 and used[0]["reason"] == "used"
    print(f"\n[api-v2] replay r1@submitted: {body['outcome']} basis={body['basis']}")

    # 按撤回后时刻回溯 r1：v3 withdrawn 拒绝，但这是另一个时间点的复核
    r_after = client.post(
        "/access-requests/r1/replay",
        json={"as_of": history[2]["changed_at"]},
    ).json()
    assert r_after["outcome"] == "deny"
    assert r_after["consent_versions"] == [{"consent_id": "c1", "version": 3}]
    assert any(e["reason"] == "withdrawn" for e in r_after["evidence"])

    # 不存在的请求不能回溯
    assert client.post("/access-requests/nope/replay", json={"as_of": _iso(NOW)}).status_code == 404


def test_replay_unknown_request_404(client: TestClient):
    r = client.post("/access-requests/missing/replay", json={"as_of": _iso(NOW)})
    assert r.status_code == 404 and r.json()["detail"] == "request_not_found"
