"""发布服务：数据最小化、去标识化假名与小样本抑制。"""
from __future__ import annotations

import hashlib
import hmac

from .models import Patient, PublicationRow
from .storage import GatewayStore

_GROUPABLE = ("diagnosis", "lab_group", "birth_decade")


class PublicationError(Exception):
    """发布失败。错误信息不得泄露被抑制个体的信息。"""

    pass


class PublicationService:
    """聚合发布：

    - 去标识化：患者以 HMAC 假名出现，同一患者跨发布稳定、由假名不可逆推身份。
    - 数据最小化：只输出分组键、计数与假名，不输出其他原始属性。
    - 小样本抑制：计数低于阈值的分组整体省略；不返回 0 计数行、
      不在错误信息中点名分组，避免通过响应差异反推被抑制个体。
    """

    def __init__(self, store: GatewayStore, *, salt: bytes, min_group_size: int = 5) -> None:
        if min_group_size < 1:
            raise ValueError("min_group_size 必须 >= 1")
        self._store = store
        self._salt = salt
        self._min_group_size = min_group_size

    @property
    def min_group_size(self) -> int:
        return self._min_group_size

    def pseudonym_for(self, patient_id: str) -> str:
        """稳定且不可逆的患者假名（HMAC-SHA256 截断）。"""
        digest = hmac.new(self._salt, patient_id.encode("utf-8"), hashlib.sha256)
        return "ps_" + digest.hexdigest()[:24]

    def publish_group_counts(
        self, group_by: str = "diagnosis", patient_ids: list[str] | None = None
    ) -> list[PublicationRow]:
        """按属性分组发布聚合计数；小于阈值的分组被静默抑制。"""
        if group_by not in _GROUPABLE:
            # 通用错误信息，不携带任何个体或分组信息
            raise PublicationError("不支持的发布分组维度")

        if patient_ids is None:
            patients = list(self._store.patients.values())
        else:
            patients = [
                p
                for pid in patient_ids
                if (p := self._store.get_patient(pid)) is not None
            ]

        groups: dict[str, list[Patient]] = {}
        for patient in patients:
            groups.setdefault(self._group_key(patient, group_by), []).append(patient)

        rows: list[PublicationRow] = []
        for key in sorted(groups):
            members = groups[key]
            if len(members) < self._min_group_size:
                continue  # 小样本抑制：整体省略，不产生可区分的响应差异
            rows.append(
                PublicationRow(
                    group_key=key,
                    pseudonym_ids=tuple(
                        sorted(self.pseudonym_for(p.patient_id) for p in members)
                    ),
                    count=len(members),
                )
            )
        return rows

    @staticmethod
    def _group_key(patient: Patient, group_by: str) -> str:
        if group_by == "diagnosis":
            return patient.diagnosis or "unknown"
        if group_by == "lab_group":
            return patient.lab_group or "unknown"
        if group_by == "birth_decade":
            return f"{patient.birth_year // 10 * 10}s"
        raise PublicationError("不支持的发布分组维度")
