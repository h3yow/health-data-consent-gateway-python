from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gateway import consent as consent_mod
from gateway.consent import ConsentService
from gateway.decision import DecisionService
from gateway.models import (
    AccessRequest,
    DataCategory,
    Decision,
    Institution,
    Patient,
    Purpose,
)
from gateway.publication import PublicationService
from gateway.storage import GatewayStore

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


class ControlledClock:
    """把同意服务取墙钟的时间固定到 NOW，保证历史 changed_at 与
    请求 submitted_at 处于同一时间基准（判定按 submitted_at 还原历史）。"""

    def __init__(self) -> None:
        self.current = NOW

        class _DT(consent_mod.datetime):
            outer = self

            @classmethod
            def now(cls, tz=None):
                return cls.outer.current

        self._dt = _DT

    def install(self) -> None:
        self._orig = consent_mod.datetime
        consent_mod.datetime = self._dt

    def restore(self) -> None:
        consent_mod.datetime = self._orig

    def advance(self, **delta) -> None:
        self.current += timedelta(**delta)


@pytest.fixture(autouse=True)
def controlled_clock():
    clock = ControlledClock()
    clock.install()
    yield clock
    clock.restore()


@pytest.fixture()
def store() -> GatewayStore:
    s = GatewayStore()
    s.add_institution(Institution("hosp-a", "医院A"))
    s.add_institution(Institution("lab-b", "检验机构B"))
    s.add_institution(Institution("pharma-c", "药企C"))
    # 声明测试直接建同意，患者主体需先登记（写入时即校验引用）；
    # 不带发布分组属性，避免污染发布聚合测试
    s.add_patient(Patient("p1", "张三", 1980))
    return s


@pytest.fixture()
def consents(store: GatewayStore) -> ConsentService:
    return ConsentService(store)


@pytest.fixture()
def decisions(store: GatewayStore, consents: ConsentService) -> DecisionService:
    return DecisionService(store, consents)


@pytest.fixture()
def publisher(store: GatewayStore) -> PublicationService:
    return PublicationService(store, salt=b"test-salt", min_group_size=3)


def make_request(
    request_id: str,
    patient_id: str = "p1",
    recipient_id: str = "hosp-a",
    purpose: Purpose = Purpose.TREATMENT,
    categories: frozenset[DataCategory] = frozenset({DataCategory.DIAGNOSIS}),
) -> AccessRequest:
    return AccessRequest(
        request_id=request_id,
        patient_id=patient_id,
        recipient_id=recipient_id,
        purpose=purpose,
        categories=categories,
        submitted_at=NOW,
    )


def log_decision(request: AccessRequest, decision: Decision) -> None:
    """单测日志：打印请求上下文与判定依据。"""
    print(
        f"\n[audit] request_id={request.request_id} "
        f"patient={request.patient_id} recipient={request.recipient_id} "
        f"purpose={request.purpose} categories={sorted(request.categories)} "
        f"-> outcome={decision.outcome} reasons={[r.value for r in decision.reasons]} "
        f"basis={decision.basis}"
    )


def active_consent_kwargs(**overrides):
    kwargs = {
        "consent_id": "c1",
        "patient_id": "p1",
        "purposes": {Purpose.TREATMENT},
        "categories": {DataCategory.DIAGNOSIS},
        "recipients": {"hosp-a"},
        "valid_from": NOW - timedelta(days=30),
        "valid_until": NOW + timedelta(days=30),
    }
    kwargs.update(overrides)
    return kwargs
