"""主体登记服务：机构与患者的登记、查询及引用校验。

写操作（同意声明建立、访问请求提交）在写入/提交那一刻即通过
``require_patient`` / ``require_institution`` 校验引用主体，
脏数据不会先落库。
"""
from __future__ import annotations

from .models import Institution, Patient
from .storage import GatewayStore


class SubjectAlreadyExistsError(Exception):
    """同一主体 ID 重复登记，且登记内容与已有记录不一致。"""

    def __init__(self, kind: str, subject_id: str) -> None:
        super().__init__(f"{kind}_already_exists:{subject_id}")
        self.kind = kind
        self.subject_id = subject_id


class PatientNotFoundError(Exception):
    def __init__(self, patient_id: str) -> None:
        super().__init__(f"patient_not_found:{patient_id}")
        self.patient_id = patient_id


class InstitutionNotFoundError(Exception):
    def __init__(self, institution_id: str) -> None:
        super().__init__(f"institution_not_found:{institution_id}")
        self.institution_id = institution_id


class RegistryService:
    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def register_institution(self, institution_id: str, name: str) -> tuple[Institution, bool]:
        """登记机构。重复登记同一 ID 且内容相同视为成功（幂等）；
        内容冲突则明确失败，不允许出现互相打架的记录。"""
        institution = Institution(institution_id=institution_id, name=name)
        existing, created = self._store.register_institution(institution)
        if not created and existing != institution:
            raise SubjectAlreadyExistsError("institution", institution_id)
        return existing, created

    def register_patient(
        self,
        patient_id: str,
        name: str,
        birth_year: int,
        *,
        diagnosis: str = "",
        lab_group: str = "",
    ) -> tuple[Patient, bool]:
        """登记患者，携带发布统计分组属性（诊断、检验分组、出生年代）。"""
        patient = Patient(
            patient_id=patient_id,
            name=name,
            birth_year=birth_year,
            diagnosis=diagnosis,
            lab_group=lab_group,
        )
        existing, created = self._store.register_patient(patient)
        if not created and existing != patient:
            raise SubjectAlreadyExistsError("patient", patient_id)
        return existing, created

    def get_patient(self, patient_id: str) -> Patient | None:
        return self._store.get_patient(patient_id)

    def get_institution(self, institution_id: str) -> Institution | None:
        return self._store.get_institution(institution_id)

    def list_institutions(self) -> list[Institution]:
        return self._store.list_institutions()

    def list_patients(self) -> list[Patient]:
        return self._store.list_patients()

    def require_patient(self, patient_id: str) -> Patient:
        patient = self._store.get_patient(patient_id)
        if patient is None:
            raise PatientNotFoundError(patient_id)
        return patient

    def require_institution(self, institution_id: str) -> Institution:
        institution = self._store.get_institution(institution_id)
        if institution is None:
            raise InstitutionNotFoundError(institution_id)
        return institution
