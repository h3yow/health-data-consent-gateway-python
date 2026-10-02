"""主体登记服务：机构与患者的登记、查询与重复登记冲突处理。"""
from __future__ import annotations

from .models import Institution, Patient
from .storage import DuplicateSubjectError, GatewayStore


class SubjectAlreadyExistsError(Exception):
    """同一 ID 重复登记。kind 区分是机构还是患者。"""

    def __init__(self, kind: str, subject_id: str) -> None:
        self.kind = kind
        self.subject_id = subject_id
        super().__init__(f"{kind} 已登记: {subject_id}")


class SubjectNotFoundError(Exception):
    """查询的主体不存在。"""

    def __init__(self, kind: str, subject_id: str) -> None:
        self.kind = kind
        self.subject_id = subject_id
        super().__init__(f"{kind} 不存在: {subject_id}")


class SubjectService:
    """机构与患者的登记处。

    登记是幂等冲突语义：同一 ID 重复登记明确失败（409 语义），
    已有记录不被覆盖，不会出现两条互相打架的记录。
    """

    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def register_institution(self, institution_id: str, name: str) -> Institution:
        institution = Institution(institution_id=institution_id, name=name)
        try:
            self._store.register_institution(institution)
        except DuplicateSubjectError as exc:
            raise SubjectAlreadyExistsError("institution", institution_id) from exc
        return institution

    def register_patient(
        self,
        patient_id: str,
        name: str,
        birth_year: int,
        *,
        diagnosis: str = "",
        lab_group: str = "",
    ) -> Patient:
        patient = Patient(
            patient_id=patient_id,
            name=name,
            birth_year=birth_year,
            diagnosis=diagnosis,
            lab_group=lab_group,
        )
        try:
            self._store.register_patient(patient)
        except DuplicateSubjectError as exc:
            raise SubjectAlreadyExistsError("patient", patient_id) from exc
        return patient

    def get_institution(self, institution_id: str) -> Institution:
        institution = self._store.get_institution(institution_id)
        if institution is None:
            raise SubjectNotFoundError("institution", institution_id)
        return institution

    def get_patient(self, patient_id: str) -> Patient:
        patient = self._store.get_patient(patient_id)
        if patient is None:
            raise SubjectNotFoundError("patient", patient_id)
        return patient

    def list_institutions(self) -> list[Institution]:
        return self._store.list_institutions()

    def list_patients(self) -> list[Patient]:
        return self._store.list_patients()
