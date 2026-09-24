import logging
from datetime import datetime, timedelta, timezone

import pytest

from app.consent import ConsentService
from app.gateway import Gateway
from app.models import PatientRecord
from app.storage import JsonStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("tests")


@pytest.fixture()
def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture()
def store() -> JsonStore:
    return JsonStore()  # in-memory only for tests


@pytest.fixture()
def consent_service(store) -> ConsentService:
    return ConsentService(store)


@pytest.fixture()
def gateway(store) -> Gateway:
    return Gateway(store)


@pytest.fixture()
def patient(store) -> str:
    store.put_patient(PatientRecord("patient-1", {"region": "east", "age_band": "30-39"}))
    return "patient-1"


@pytest.fixture()
def validity(now):
    return now - timedelta(days=1), now + timedelta(days=30)


def log_decision(request, decision):
    """Print request context and decision basis into the test log."""
    logger.info(
        "request id=%s patient=%s requester=%s purpose=%s categories=%s",
        request.request_id,
        request.patient_id,
        request.requester_id,
        request.purpose.value,
        [c.value for c in request.data_categories],
    )
    logger.info(
        "decision outcome=%s reasons=%s matched=%s basis=%s",
        decision.outcome.value,
        [r.value for r in decision.deny_reasons],
        list(zip(decision.matched_consent_ids, decision.matched_consent_versions)),
        decision.basis,
    )
