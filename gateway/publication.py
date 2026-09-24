"""发布服务：数据最小化、去标识化假名与小样本抑制。"""
from __future__ import annotations

from .models import Patient, PublicationRow
from .storage import GatewayStore


class PublicationError(Exception):
    """发布失败。错误信息不得泄露被抑制个体的信息。"""

    pass


class PublicationService:
    def __init__(self, store: GatewayStore, *, salt: bytes, min_group_size: int = 5) -> None:
        self._store = store
        self._salt = salt
        self._min_group_size = min_group_size

    def pseudonym_for(self, patient_id: str) -> str:
        """稳定且不可逆的患者假名。"""
        raise NotImplementedError

    def publish_group_counts(
        self, group_by: str, patient_ids: list[str] | None = None
    ) -> list[PublicationRow]:
        """按属性分组发布聚合计数；小于阈值的分组被抑制。"""
        raise NotImplementedError

    def _group_key(self, patient: Patient, group_by: str) -> str:
        raise NotImplementedError
