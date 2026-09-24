"""同意声明的建立、变更、撤回与有效期。"""
from datetime import timedelta

import pytest

from gateway.consent import ConsentNotFoundError, ConsentService
from gateway.models import ConsentStatus, DataCategory, Purpose
from tests.conftest import NOW, active_consent_kwargs


def test_create_consent(consents: ConsentService):
    c = consents.create(**active_consent_kwargs())
    assert c.status == ConsentStatus.ACTIVE
    assert c.version == 1
    assert c.purposes == frozenset({Purpose.TREATMENT})


def test_amend_increments_version(consents: ConsentService):
    consents.create(**active_consent_kwargs())
    amended = consents.amend("c1", categories={DataCategory.DIAGNOSIS, DataCategory.LAB_RESULT})
    assert amended.version == 2
    assert DataCategory.LAB_RESULT in amended.categories
    print(f"\n[consent] amended c1 -> v{amended.version} categories={sorted(amended.categories)}")


def test_withdraw_blocks_further_amend(consents: ConsentService):
    consents.create(**active_consent_kwargs())
    withdrawn = consents.withdraw("c1")
    assert withdrawn.status == ConsentStatus.WITHDRAWN
    with pytest.raises(ValueError):
        consents.amend("c1", purposes={Purpose.RESEARCH})


def test_missing_consent_raises(consents: ConsentService):
    with pytest.raises(ConsentNotFoundError):
        consents.withdraw("nope")


def test_validity_window(consents: ConsentService):
    expired = consents.create(
        **active_consent_kwargs(
            consent_id="c-exp",
            valid_from=NOW - timedelta(days=60),
            valid_until=NOW - timedelta(days=1),
        )
    )
    future = consents.create(
        **active_consent_kwargs(
            consent_id="c-future",
            valid_from=NOW + timedelta(days=1),
            valid_until=NOW + timedelta(days=30),
        )
    )
    active = consents.create(**active_consent_kwargs(consent_id="c-ok"))
    assert not consents.is_effective(expired, NOW)
    assert not consents.is_effective(future, NOW)
    assert consents.is_effective(active, NOW)
    withdrawn = consents.withdraw("c-ok")
    assert not consents.is_effective(withdrawn, NOW)


def test_overlapping_statements_coexist(consents: ConsentService):
    """同一患者可同时存在多条部分重叠的声明。"""
    consents.create(**active_consent_kwargs(consent_id="c1"))
    consents.create(
        **active_consent_kwargs(
            consent_id="c2",
            purposes={Purpose.TREATMENT, Purpose.RESEARCH},
            categories={DataCategory.DIAGNOSIS, DataCategory.LAB_RESULT},
            recipients={"hosp-a", "lab-b"},
        )
    )
    patient_consents = {c.consent_id for c in consents._store.consents_for_patient("p1")}
    assert patient_consents == {"c1", "c2"}
