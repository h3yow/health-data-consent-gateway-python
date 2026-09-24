"""跨机构健康数据访问网关：FastAPI 入口。"""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from gateway.consent import ConsentNotFoundError, ConsentService
from gateway.decision import DecisionService
from gateway.models import (
    AccessRequest,
    DataCategory,
    DecisionOutcome,
    Purpose,
)
from gateway.publication import PublicationError, PublicationService
from gateway.storage import GatewayStore

store = GatewayStore()
consent_service = ConsentService(store)
decision_service = DecisionService(store, consent_service)
publication_service = PublicationService(
    store,
    salt=os.environ.get("GATEWAY_PSEUDONYM_SALT", "dev-only-salt").encode(),
    min_group_size=int(os.environ.get("GATEWAY_MIN_GROUP_SIZE", "5")),
)

app = FastAPI(title="Health Data Consent Gateway")


class ConsentCreateIn(BaseModel):
    consent_id: str
    patient_id: str
    purposes: set[Purpose]
    categories: set[DataCategory]
    recipients: set[str]
    valid_from: datetime
    valid_until: datetime


class ConsentAmendIn(BaseModel):
    purposes: set[Purpose] | None = None
    categories: set[DataCategory] | None = None
    recipients: set[str] | None = None
    valid_until: datetime | None = None


class AccessRequestIn(BaseModel):
    request_id: str
    patient_id: str
    recipient_id: str
    purpose: Purpose
    categories: set[DataCategory] = Field(min_length=1)


@app.get("/")
def read_root():
    return {"service": "health-data-consent-gateway"}


@app.post("/consents", status_code=201)
def create_consent(body: ConsentCreateIn):
    return consent_service.create(
        consent_id=body.consent_id,
        patient_id=body.patient_id,
        purposes=set(body.purposes),
        categories=set(body.categories),
        recipients=set(body.recipients),
        valid_from=body.valid_from,
        valid_until=body.valid_until,
    )


@app.patch("/consents/{consent_id}")
def amend_consent(consent_id: str, body: ConsentAmendIn):
    try:
        return consent_service.amend(
            consent_id,
            purposes=set(body.purposes) if body.purposes is not None else None,
            categories=set(body.categories) if body.categories is not None else None,
            recipients=set(body.recipients) if body.recipients is not None else None,
            valid_until=body.valid_until,
        )
    except ConsentNotFoundError:
        raise HTTPException(status_code=404, detail="consent_not_found")


@app.post("/consents/{consent_id}/withdraw")
def withdraw_consent(consent_id: str):
    try:
        return consent_service.withdraw(consent_id)
    except ConsentNotFoundError:
        raise HTTPException(status_code=404, detail="consent_not_found")


@app.post("/access-requests")
def submit_access_request(body: AccessRequestIn):
    request = AccessRequest(
        request_id=body.request_id,
        patient_id=body.patient_id,
        recipient_id=body.recipient_id,
        purpose=body.purpose,
        categories=frozenset(body.categories),
        submitted_at=datetime.now().astimezone(),
    )
    decision = decision_service.evaluate(request)
    return {
        "request_id": decision.request_id,
        "outcome": decision.outcome,
        "reasons": list(decision.reasons),
        "basis": decision.basis,
        "decided_at": decision.decided_at,
        "allowed": decision.outcome == DecisionOutcome.ALLOW,
    }


@app.get("/audits")
def list_audits():
    return [
        {
            "audit_id": a.audit_id,
            "request_id": a.request.request_id,
            "patient_id": a.request.patient_id,
            "recipient_id": a.request.recipient_id,
            "outcome": a.decision.outcome,
            "reasons": list(a.decision.reasons),
            "basis": a.decision.basis,
        }
        for a in store.list_audits()
    ]


@app.post("/publications/group-counts")
def publish_group_counts(group_by: str = "diagnosis"):
    try:
        rows = publication_service.publish_group_counts(group_by)
    except PublicationError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return [
        {"group_key": r.group_key, "count": r.count, "pseudonym_ids": list(r.pseudonym_ids)}
        for r in rows
    ]
