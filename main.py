"""跨机构健康数据访问网关：FastAPI 入口。"""
from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime  # noqa: I001

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from gateway.consent import (
    ConsentNotFoundError,
    ConsentService,
    DuplicateConsentError,
    current_time,
)
from gateway.decision import DecisionService
from gateway.models import (
    AccessRequest,
    DataCategory,
    DecisionOutcome,
    Purpose,
)
from gateway.publication import PublicationError, PublicationService
from gateway.registry import (
    InstitutionNotFoundError,
    PatientNotFoundError,
    RegistryService,
    SubjectAlreadyExistsError,
)
from gateway.storage import GatewayStore

store = GatewayStore()
registry_service = RegistryService(store)
consent_service = ConsentService(store, registry_service)
decision_service = DecisionService(store, consent_service, registry_service)
publication_service = PublicationService(
    store,
    salt=os.environ.get("GATEWAY_PSEUDONYM_SALT", "dev-only-salt").encode(),
    min_group_size=int(os.environ.get("GATEWAY_MIN_GROUP_SIZE", "5")),
)

app = FastAPI(title="Health Data Consent Gateway")


class InstitutionIn(BaseModel):
    institution_id: str = Field(min_length=1)
    name: str = Field(min_length=1)


class PatientIn(BaseModel):
    patient_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    birth_year: int = Field(ge=1900, le=2200)
    diagnosis: str = ""
    lab_group: str = ""


class ConsentCreateIn(BaseModel):
    consent_id: str
    patient_id: str
    purposes: set[Purpose]
    categories: set[DataCategory]
    recipients: set[str] = Field(min_length=1)
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


class ReplayIn(BaseModel):
    as_of: datetime


def _consent_body(c) -> dict:
    return {
        "consent_id": c.consent_id,
        "patient_id": c.patient_id,
        "purposes": sorted(p.value for p in c.purposes),
        "categories": sorted(c.value for c in c.categories),
        "recipients": sorted(c.recipients),
        "valid_from": c.valid_from,
        "valid_until": c.valid_until,
        "status": c.status,
        "version": c.version,
        "updated_at": c.updated_at,
    }


@app.get("/")
def read_root():
    return {"service": "health-data-consent-gateway"}


# ---- 主体登记 ----------------------------------------------------


@app.post("/institutions", status_code=201)
def register_institution(body: InstitutionIn):
    try:
        institution, created = registry_service.register_institution(
            body.institution_id, body.name
        )
    except SubjectAlreadyExistsError:
        raise HTTPException(status_code=409, detail="institution_id_conflict")
    return {**asdict(institution), "created": created}


@app.get("/institutions")
def list_institutions():
    return [asdict(i) for i in registry_service.list_institutions()]


@app.get("/institutions/{institution_id}")
def get_institution(institution_id: str):
    institution = registry_service.get_institution(institution_id)
    if institution is None:
        raise HTTPException(status_code=404, detail="institution_not_found")
    return asdict(institution)


@app.post("/patients", status_code=201)
def register_patient(body: PatientIn):
    try:
        patient, created = registry_service.register_patient(
            body.patient_id,
            body.name,
            body.birth_year,
            diagnosis=body.diagnosis,
            lab_group=body.lab_group,
        )
    except SubjectAlreadyExistsError:
        raise HTTPException(status_code=409, detail="patient_id_conflict")
    return {**asdict(patient), "created": created}


@app.get("/patients")
def list_patients():
    return [asdict(p) for p in registry_service.list_patients()]


@app.get("/patients/{patient_id}")
def get_patient(patient_id: str):
    patient = registry_service.get_patient(patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="patient_not_found")
    return asdict(patient)


# ---- 同意声明 ----------------------------------------------------


@app.post("/consents", status_code=201)
def create_consent(body: ConsentCreateIn):
    try:
        consent = consent_service.create(
            consent_id=body.consent_id,
            patient_id=body.patient_id,
            purposes=set(body.purposes),
            categories=set(body.categories),
            recipients=set(body.recipients),
            valid_from=body.valid_from,
            valid_until=body.valid_until,
        )
    except DuplicateConsentError:
        raise HTTPException(status_code=409, detail="consent_id_conflict")
    except PatientNotFoundError:
        # 写入那一刻即拒绝脏引用，可分清是患者还是机构不存在
        raise HTTPException(status_code=422, detail="patient_not_found")
    except InstitutionNotFoundError:
        raise HTTPException(status_code=422, detail="institution_not_found")
    return _consent_body(consent)


@app.patch("/consents/{consent_id}")
def amend_consent(consent_id: str, body: ConsentAmendIn):
    try:
        amended = consent_service.amend(
            consent_id,
            purposes=set(body.purposes) if body.purposes is not None else None,
            categories=set(body.categories) if body.categories is not None else None,
            recipients=set(body.recipients) if body.recipients is not None else None,
            valid_until=body.valid_until,
        )
    except ConsentNotFoundError:
        raise HTTPException(status_code=404, detail="consent_not_found")
    except InstitutionNotFoundError:
        raise HTTPException(status_code=422, detail="institution_not_found")
    return _consent_body(amended)


@app.post("/consents/{consent_id}/withdraw")
def withdraw_consent(consent_id: str):
    try:
        withdrawn = consent_service.withdraw(consent_id)
    except ConsentNotFoundError:
        raise HTTPException(status_code=404, detail="consent_not_found")
    return _consent_body(withdrawn)


@app.get("/consents/{consent_id}/history")
def consent_history(consent_id: str):
    try:
        entries = consent_service.history(consent_id)
    except ConsentNotFoundError:
        raise HTTPException(status_code=404, detail="consent_not_found")
    return [
        {
            "consent_id": e.consent_id,
            "version": e.version,
            "action": e.action,
            "changed_at": e.changed_at,
            "statement": _consent_body(e.statement),
        }
        for e in entries
    ]


# ---- 访问判定与回溯 ----------------------------------------------


@app.post("/access-requests")
def submit_access_request(body: AccessRequestIn):
    request = AccessRequest(
        request_id=body.request_id,
        patient_id=body.patient_id,
        recipient_id=body.recipient_id,
        purpose=body.purpose,
        categories=frozenset(body.categories),
        submitted_at=current_time(),
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


@app.post("/access-requests/{request_id}/replay")
def replay_access_request(request_id: str, body: ReplayIn):
    """按时间点回溯重判，用于事后复核。"""
    audit = store.get_audit_by_request(request_id)
    if audit is None:
        raise HTTPException(status_code=404, detail="request_not_found")
    # 从原审计记录取回当时的请求要素，保证复核的就是同一条请求
    result = decision_service.replay(audit.request, body.as_of)
    return {
        "request_id": request_id,
        "as_of": body.as_of,
        "outcome": result.decision.outcome,
        "reasons": list(result.decision.reasons),
        "basis": result.decision.basis,
        "consent_versions": [
            {"consent_id": cid, "version": v} for cid, v in result.decision.consent_versions
        ],
        "matches_original": result.matches_original,
        "evidence": [
            {
                "consent_id": e.consent_id,
                "version": e.version,
                "status": e.status,
                "considered": e.considered,
                "reason": e.reason,
            }
            for e in result.evidence
        ],
    }


@app.get("/audits")
def list_audits():
    return [
        {
            "audit_id": a.audit_id,
            "request_id": a.request.request_id,
            "patient_id": a.request.patient_id,
            "recipient_id": a.request.recipient_id,
            "submitted_at": a.request.submitted_at,
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
