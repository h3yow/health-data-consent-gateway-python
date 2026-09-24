"""Local JSON-file backed storage simulating institutions, patients and requests."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .models import (
    AuditRecord,
    ConsentStatement,
    ConsentStatus,
    DataCategory,
    Decision,
    DecisionOutcome,
    DenyReason,
    PatientRecord,
    Purpose,
)


def _dt_to_str(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dt_from_str(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _consent_to_dict(c: ConsentStatement) -> dict:
    return {
        "consent_id": c.consent_id,
        "patient_id": c.patient_id,
        "purposes": sorted(p.value for p in c.purposes),
        "data_categories": sorted(d.value for d in c.data_categories),
        "recipients": sorted(c.recipients),
        "valid_from": _dt_to_str(c.valid_from),
        "valid_until": _dt_to_str(c.valid_until),
        "status": c.status.value,
        "version": c.version,
        "created_at": _dt_to_str(c.created_at),
        "updated_at": _dt_to_str(c.updated_at),
        "revoked_at": _dt_to_str(c.revoked_at),
    }


def _consent_from_dict(d: dict) -> ConsentStatement:
    return ConsentStatement(
        consent_id=d["consent_id"],
        patient_id=d["patient_id"],
        purposes=frozenset(Purpose(p) for p in d["purposes"]),
        data_categories=frozenset(DataCategory(c) for c in d["data_categories"]),
        recipients=frozenset(d["recipients"]),
        valid_from=datetime.fromisoformat(d["valid_from"]),
        valid_until=_dt_from_str(d["valid_until"]),
        status=ConsentStatus(d["status"]),
        version=d["version"],
        created_at=datetime.fromisoformat(d["created_at"]),
        updated_at=datetime.fromisoformat(d["updated_at"]),
        revoked_at=_dt_from_str(d["revoked_at"]),
    )


def _decision_to_dict(d: Decision) -> dict:
    return {
        "request_id": d.request_id,
        "outcome": d.outcome.value,
        "deny_reasons": [r.value for r in d.deny_reasons],
        "matched_consent_ids": list(d.matched_consent_ids),
        "matched_consent_versions": list(d.matched_consent_versions),
        "basis": d.basis,
        "decided_at": _dt_to_str(d.decided_at),
    }


def _decision_from_dict(d: dict) -> Decision:
    return Decision(
        request_id=d["request_id"],
        outcome=DecisionOutcome(d["outcome"]),
        deny_reasons=tuple(DenyReason(r) for r in d["deny_reasons"]),
        matched_consent_ids=tuple(d["matched_consent_ids"]),
        matched_consent_versions=tuple(d["matched_consent_versions"]),
        basis=d["basis"],
        decided_at=datetime.fromisoformat(d["decided_at"]),
    )


def _audit_to_dict(a: AuditRecord) -> dict:
    d = asdict(a)
    d["outcome"] = a.outcome.value
    d["deny_reasons"] = [r.value for r in a.deny_reasons]
    d["decided_at"] = _dt_to_str(a.decided_at)
    return d


def _audit_from_dict(d: dict) -> AuditRecord:
    return AuditRecord(
        audit_id=d["audit_id"],
        request_id=d["request_id"],
        patient_id=d["patient_id"],
        requester_id=d["requester_id"],
        outcome=DecisionOutcome(d["outcome"]),
        deny_reasons=tuple(DenyReason(r) for r in d["deny_reasons"]),
        matched_consent_ids=tuple(d["matched_consent_ids"]),
        matched_consent_versions=tuple(d["matched_consent_versions"]),
        basis=d["basis"],
        decided_at=datetime.fromisoformat(d["decided_at"]),
    )


class JsonStore:
    """Thread-safe local store. State kept in memory and persisted to one JSON file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self._path = Path(path) if path else None
        self._lock = threading.RLock()
        self._patients: dict[str, PatientRecord] = {}
        self._consents: dict[str, ConsentStatement] = {}
        self._decisions: dict[str, Decision] = {}  # keyed by request_id (idempotency)
        self._audits: list[AuditRecord] = []
        if self._path and self._path.exists():
            self._load()

    # --- persistence ------------------------------------------------------
    def _load(self) -> None:
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        self._patients = {
            p["patient_id"]: PatientRecord(patient_id=p["patient_id"], attributes=p["attributes"])
            for p in raw.get("patients", [])
        }
        self._consents = {c["consent_id"]: _consent_from_dict(c) for c in raw.get("consents", [])}
        self._decisions = {d["request_id"]: _decision_from_dict(d) for d in raw.get("decisions", [])}
        self._audits = [_audit_from_dict(a) for a in raw.get("audits", [])]

    def _persist_locked(self) -> None:
        if not self._path:
            return
        payload = {
            "patients": [
                {"patient_id": p.patient_id, "attributes": p.attributes}
                for p in self._patients.values()
            ],
            "consents": [_consent_to_dict(c) for c in self._consents.values()],
            "decisions": [_decision_to_dict(d) for d in self._decisions.values()],
            "audits": [_audit_to_dict(a) for a in self._audits],
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # --- patients ---------------------------------------------------------
    def put_patient(self, patient: PatientRecord) -> None:
        with self._lock:
            self._patients[patient.patient_id] = patient
            self._persist_locked()

    def get_patient(self, patient_id: str) -> PatientRecord | None:
        with self._lock:
            return self._patients.get(patient_id)

    def list_patients(self) -> list[PatientRecord]:
        with self._lock:
            return list(self._patients.values())

    # --- consents ---------------------------------------------------------
    def put_consent(self, consent: ConsentStatement) -> None:
        with self._lock:
            self._consents[consent.consent_id] = consent
            self._persist_locked()

    def get_consent(self, consent_id: str) -> ConsentStatement | None:
        with self._lock:
            return self._consents.get(consent_id)

    def consents_for_patient(self, patient_id: str) -> list[ConsentStatement]:
        with self._lock:
            return [c for c in self._consents.values() if c.patient_id == patient_id]

    # --- decisions / audit --------------------------------------------------
    def get_decision(self, request_id: str) -> Decision | None:
        with self._lock:
            return self._decisions.get(request_id)

    def record_decision_and_audit(self, decision: Decision, audit: AuditRecord) -> bool:
        """Atomically record decision+audit if request_id unseen. Returns True if recorded."""
        with self._lock:
            if decision.request_id in self._decisions:
                return False
            self._decisions[decision.request_id] = decision
            self._audits.append(audit)
            self._persist_locked()
            return True

    def list_audits(self) -> list[AuditRecord]:
        with self._lock:
            return list(self._audits)
