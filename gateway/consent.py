"""同意声明服务：建立、变更、撤回与有效性判断。"""
from __future__ import annotations

from datetime import datetime

from .models import (
    ConsentStatement,
    ConsentStatus,
    DataCategory,
    Purpose,
)
from .storage import GatewayStore


class ConsentNotFoundError(Exception):
    pass


class ConsentService:
    def __init__(self, store: GatewayStore) -> None:
        self._store = store

    def create(
        self,
        consent_id: str,
        patient_id: str,
        purposes: set[Purpose],
        categories: set[DataCategory],
        recipients: set[str],
        valid_from: datetime,
        valid_until: datetime,
    ) -> ConsentStatement:
        raise NotImplementedError

    def amend(
        self,
        consent_id: str,
        *,
        purposes: set[Purpose] | None = None,
        categories: set[DataCategory] | None = None,
        recipients: set[str] | None = None,
        valid_until: datetime | None = None,
    ) -> ConsentStatement:
        """变更声明，版本号递增。"""
        raise NotImplementedError

    def withdraw(self, consent_id: str) -> ConsentStatement:
        raise NotImplementedError

    def is_effective(self, consent: ConsentStatement, at: datetime) -> bool:
        """声明在给定时刻是否可作为放行依据。"""
        raise NotImplementedError
