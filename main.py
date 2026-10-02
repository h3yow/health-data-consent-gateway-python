"""跨机构健康数据访问网关：FastAPI 入口。"""
from __future__ import annotations

import os
from datetime import datetime

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from gateway.consent import (
    ConsentAlreadyExistsError,
    ConsentNotFoundError,
    ConsentService,
    PatientNotRegisteredError,
    RecipientNotRegisteredError,
)
from gateway.decision import DecisionService
from gateway.models import (
    AccessRequest,
    DataCategory,
    DecisionOutcome,
    Purpose,
)
from gateway.publication import PublicationError, PublicationService
from gateway.storage import GatewayStore
from gateway.subjects import SubjectAlreadyExistsError, SubjectNotFoundError, SubjectService

store = GatewayStore()
subject_service = SubjectService(store)
consent_service = ConsentService(store)
decision_service = DecisionService(store, consent_service)
publication_service = PublicationService(
    store,
    salt=os.environ.get("GATEWAY_PSEUDONYM_SALT", "dev-only-salt").encode(),
    min_group_size=int(os.environ.get("GATEWAY_MIN_GROUP_SIZE", "5")),
)

app = FastAPI(title="Health Data Consent Gateway")


class InstitutionIn(BaseModel):
    institution_id: str
    name: str


class PatientIn(BaseModel):
    patient_id: str
    name: str
    birth_year: int
    diagnosis: str = ""
    lab_group: str = ""


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


# ---- 主体登记：机构与患者 ----


@app.post("/institutions", status_code=201)
def register_institution(body: InstitutionIn):
    try:
        return subject_service.register_institution(body.institution_id, body.name)
    except SubjectAlreadyExistsError:
        raise HTTPException(status_code=409, detail="institution_already_exists")


@app.get("/institutions")
def list_institutions():
    return subject_service.list_institutions()


@app.get("/institutions/{institution_id}")
def get_institution(institution_id: str):
    try:
        return subject_service.get_institution(institution_id)
    except SubjectNotFoundError:
        raise HTTPException(status_code=404, detail="institution_not_found")


@app.post("/patients", status_code=201)
def register_patient(body: PatientIn):
    try:
        return subject_service.register_patient(
            body.patient_id,
            body.name,
            body.birth_year,
            diagnosis=body.diagnosis,
            lab_group=body.lab_group,
        )
    except SubjectAlreadyExistsError:
        raise HTTPException(status_code=409, detail="patient_already_exists")


@app.get("/patients")
def list_patients():
    return subject_service.list_patients()


@app.get("/patients/{patient_id}")
def get_patient(patient_id: str):
    try:
        return subject_service.get_patient(patient_id)
    except SubjectNotFoundError:
        raise HTTPException(status_code=404, detail="patient_not_found")


# ---- 同意声明：建立、变更、撤回、历史 ----


@app.post("/consents", status_code=201)
def create_consent(body: ConsentCreateIn):
    try:
        return consent_service.create(
            consent_id=body.consent_id,
            patient_id=body.patient_id,
            purposes=set(body.purposes),
            categories=set(body.categories),
            recipients=set(body.recipients),
            valid_from=body.valid_from,
            valid_until=body.valid_until,
        )
    except PatientNotRegisteredError:
        raise HTTPException(status_code=422, detail="patient_not_found")
    except RecipientNotRegisteredError:
        raise HTTPException(status_code=422, detail="recipient_not_found")
    except ConsentAlreadyExistsError:
        raise HTTPException(status_code=409, detail="consent_already_exists")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


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
    except RecipientNotRegisteredError:
        raise HTTPException(status_code=422, detail="recipient_not_found")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/consents/{consent_id}/withdraw")
def withdraw_consent(consent_id: str):
    try:
        return consent_service.withdraw(consent_id)
    except ConsentNotFoundError:
        raise HTTPException(status_code=404, detail="consent_not_found")


@app.get("/consents/{consent_id}/history")
def consent_history(consent_id: str):
    """声明的完整变更历史：每次建立/变更/撤回一条，含版本、状态与时间。"""
    try:
        entries = consent_service.history(consent_id)
    except ConsentNotFoundError:
        raise HTTPException(status_code=404, detail="consent_not_found")
    return [
        {
            "consent_id": e.consent_id,
            "patient_id": e.patient_id,
            "version": e.version,
            "change_type": e.change_type,
            "status": e.status,
            "purposes": sorted(e.purposes),
            "categories": sorted(e.categories),
            "recipients": sorted(e.recipients),
            "valid_from": e.valid_from,
            "valid_until": e.valid_until,
            "changed_at": e.changed_at,
        }
        for e in entries
    ]


# ---- 访问判定、审计与回溯复核 ----


def _decision_payload(decision, request: AccessRequest | None = None):
    payload = {
        "request_id": decision.request_id,
        "outcome": decision.outcome,
        "reasons": list(decision.reasons),
        "basis": decision.basis,
        "decided_at": decision.decided_at,
        "consent_versions": [list(cv) for cv in decision.consent_versions],
        "allowed": decision.outcome == DecisionOutcome.ALLOW,
    }
    if request is not None:
        payload["submitted_at"] = request.submitted_at
    return payload


@app.post("/access-requests")
def submit_access_request(body: AccessRequestIn):
    # 提交那一刻就校验主体：未登记的患者/机构明确失败，不落判定与审计
    if store.get_patient(body.patient_id) is None:
        raise HTTPException(status_code=422, detail="patient_not_found")
    if store.get_institution(body.recipient_id) is None:
        raise HTTPException(status_code=422, detail="recipient_not_found")
    request = AccessRequest(
        request_id=body.request_id,
        patient_id=body.patient_id,
        recipient_id=body.recipient_id,
        purpose=body.purpose,
        categories=frozenset(body.categories),
        submitted_at=datetime.now().astimezone(),
    )
    decision = decision_service.evaluate(request)
    return _decision_payload(decision)


@app.get("/access-requests/{request_id}/review")
def review_access_request(request_id: str):
    """事后复核：按请求提交时刻还原同意状态重判，并与当时真实判定对照。"""
    result = decision_service.review(request_id)
    if result is None:
        raise HTTPException(status_code=404, detail="request_not_found")
    record, retrospective = result
    return {
        "original": _decision_payload(record.decision, record.request),
        "retrospective": _decision_payload(retrospective, record.request),
        "consistent": (
            record.decision.outcome == retrospective.outcome
            and record.decision.reasons == retrospective.reasons
        ),
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
