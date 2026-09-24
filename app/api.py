"""FastAPI HTTP layer exposing consent, access and publish endpoints."""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .consent import ConsentService
from .gateway import Gateway
from .models import (
    AccessRequest,
    DataCategory,
    PatientRecord,
    PublishPolicy,
    Purpose,
)
from .publish import PublishService
from .storage import JsonStore


class PatientIn(BaseModel):
    patient_id: str
    attributes: dict[str, str] = Field(default_factory=dict)


class ConsentIn(BaseModel):
    patient_id: str
    purposes: set[Purpose]
    data_categories: set[DataCategory]
    recipients: set[str]
    valid_from: datetime
    valid_until: datetime | None = None


class ConsentAmendIn(BaseModel):
    purposes: set[Purpose] | None = None
    data_categories: set[DataCategory] | None = None
    recipients: set[str] | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class AccessRequestIn(BaseModel):
    request_id: str
    patient_id: str
    requester_id: str
    purpose: Purpose
    data_categories: list[DataCategory]


class PublishIn(BaseModel):
    attributes: list[str]
    min_group_size: int = 5
    salt: str = "local-dev-salt"


def build_router(store: JsonStore) -> APIRouter:
    router = APIRouter()
    consents = ConsentService(store)
    gateway = Gateway(store)

    @router.post("/patients", status_code=201)
    def create_patient(body: PatientIn):
        store.put_patient(PatientRecord(patient_id=body.patient_id, attributes=body.attributes))
        return {"patient_id": body.patient_id}

    @router.post("/consents", status_code=201)
    def create_consent(body: ConsentIn):
        try:
            c = consents.create_consent(
                patient_id=body.patient_id,
                purposes=body.purposes,
                data_categories=body.data_categories,
                recipients=body.recipients,
                valid_from=body.valid_from,
                valid_until=body.valid_until,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"consent_id": c.consent_id, "version": c.version, "status": c.status.value}

    @router.post("/consents/{consent_id}/amend")
    def amend_consent(consent_id: str, body: ConsentAmendIn):
        try:
            c = consents.amend_consent(
                consent_id,
                purposes=body.purposes,
                data_categories=body.data_categories,
                recipients=body.recipients,
                valid_from=body.valid_from,
                valid_until=body.valid_until,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"consent_id": c.consent_id, "version": c.version, "status": c.status.value}

    @router.post("/consents/{consent_id}/revoke")
    def revoke_consent(consent_id: str):
        try:
            c = consents.revoke_consent(consent_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {"consent_id": c.consent_id, "version": c.version, "status": c.status.value}

    @router.get("/patients/{patient_id}/consents")
    def list_consents(patient_id: str):
        return [
            {
                "consent_id": c.consent_id,
                "status": c.status.value,
                "version": c.version,
                "purposes": sorted(p.value for p in c.purposes),
                "data_categories": sorted(d.value for d in c.data_categories),
                "recipients": sorted(c.recipients),
                "valid_from": c.valid_from.isoformat(),
                "valid_until": c.valid_until.isoformat() if c.valid_until else None,
            }
            for c in consents.consents_for_patient(patient_id)
        ]

    @router.post("/access-requests")
    def submit_access_request(body: AccessRequestIn):
        request = AccessRequest(
            request_id=body.request_id,
            patient_id=body.patient_id,
            requester_id=body.requester_id,
            purpose=body.purpose,
            data_categories=tuple(body.data_categories),
            submitted_at=datetime.now().astimezone(),
        )
        d = gateway.evaluate(request)
        return {
            "request_id": d.request_id,
            "outcome": d.outcome.value,
            "deny_reasons": [r.value for r in d.deny_reasons],
            "matched_consent_ids": list(d.matched_consent_ids),
            "basis": d.basis,
        }

    @router.get("/audits")
    def list_audits():
        return [
            {
                "audit_id": a.audit_id,
                "request_id": a.request_id,
                "patient_id": a.patient_id,
                "requester_id": a.requester_id,
                "outcome": a.outcome.value,
                "deny_reasons": [r.value for r in a.deny_reasons],
                "matched_consent_ids": list(a.matched_consent_ids),
                "matched_consent_versions": list(a.matched_consent_versions),
                "basis": a.basis,
                "decided_at": a.decided_at.isoformat(),
            }
            for a in gateway.audits()
        ]

    @router.post("/publish")
    def publish(body: PublishIn):
        svc = PublishService(PublishPolicy(min_group_size=body.min_group_size, salt=body.salt))
        report = svc.publish(store.list_patients(), tuple(body.attributes))
        return {
            "released": [
                {
                    "group_key": dict(g.group_key),
                    "count": g.count,
                    "pseudonyms": list(g.pseudonyms),
                }
                for g in report.released
            ],
            "suppressed": [{"group_key": dict(g.group_key), "suppressed": True} for g in report.suppressed],
            "min_group_size": report.policy.min_group_size,
        }

    return router
