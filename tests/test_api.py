"""End-to-end HTTP tests over the FastAPI surface."""
import logging

import pytest
from fastapi.testclient import TestClient

from app.api import build_router
from app.storage import JsonStore
from fastapi import FastAPI

logger = logging.getLogger("tests")


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(build_router(JsonStore()))
    return TestClient(app)


def test_full_flow_over_http(client):
    assert client.post("/patients", json={"patient_id": "p1", "attributes": {"region": "east"}}).status_code == 201

    r = client.post("/consents", json={
        "patient_id": "p1", "purposes": ["research"], "data_categories": ["diagnoses"],
        "recipients": ["org-a"], "valid_from": "2026-01-01T00:00:00Z",
        "valid_until": "2027-01-01T00:00:00Z"})
    assert r.status_code == 201, r.text
    consent_id = r.json()["consent_id"]

    body = {"request_id": "api-1", "patient_id": "p1", "requester_id": "org-a",
            "purpose": "research", "data_categories": ["diagnoses"]}
    r = client.post("/access-requests", json=body)
    assert r.json()["outcome"] == "allow"
    logger.info("api allow basis: %s", r.json()["basis"])

    # duplicate submission over HTTP is idempotent
    assert client.post("/access-requests", json=body).json()["outcome"] == "allow"
    audits = client.get("/audits").json()
    assert len([a for a in audits if a["request_id"] == "api-1"]) == 1

    # revoke -> subsequent request denied with distinguishable reason
    assert client.post(f"/consents/{consent_id}/revoke").json()["status"] == "revoked"
    body["request_id"] = "api-2"
    r = client.post("/access-requests", json=body)
    assert r.json()["outcome"] == "deny"
    assert "consent_revoked" in r.json()["deny_reasons"]
    logger.info("api deny reasons: %s basis: %s", r.json()["deny_reasons"], r.json()["basis"])


def test_consent_for_unknown_patient_rejected(client):
    r = client.post("/consents", json={
        "patient_id": "ghost", "purposes": ["research"], "data_categories": ["diagnoses"],
        "recipients": ["org-a"], "valid_from": "2026-01-01T00:00:00Z"})
    assert r.status_code == 400


def test_publish_endpoint_suppresses_small_groups(client):
    for i in range(4):
        client.post("/patients", json={"patient_id": f"e{i}", "attributes": {"region": "east"}})
    client.post("/patients", json={"patient_id": "w0", "attributes": {"region": "west"}})

    r = client.post("/publish", json={"attributes": ["region"], "min_group_size": 3})
    payload = r.json()
    assert len(payload["released"]) == 1 and payload["released"][0]["count"] == 4
    assert len(payload["suppressed"]) == 1
    assert "count" not in payload["suppressed"][0]
    logger.info("api publish payload: %s", payload)
