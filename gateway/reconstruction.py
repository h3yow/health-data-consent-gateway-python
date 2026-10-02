"""同意历史的时间点还原：从历史条目重建某一刻的声明状态。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import (
    ConsentHistoryEntry,
    ConsentStatement,
    ConsentStatus,
)


@dataclass(frozen=True)
class ReconstructedConsent:
    """某条声明在指定时间点的还原结果。

    - statement：那一刻生效的版本快照；声明当时尚未建立则为 None。
    - state：used / withdrawn / not_yet_effective / expired / not_created
    """

    consent_id: str
    statement: ConsentStatement | None
    state: str


def reconstruct_at(
    history: list[ConsentHistoryEntry], as_of: datetime
) -> ReconstructedConsent:
    """按历史还原一条同意声明在 ``as_of`` 时刻的版本与状态。

    取 changed_at <= as_of 的最后一条历史作为当时版本：
    之后的建立/变更/撤回在该时刻都尚未发生，不能作为依据。
    """
    consent_id = history[0].consent_id if history else ""
    applicable = [e for e in history if e.changed_at <= as_of]
    if not applicable:
        return ReconstructedConsent(consent_id, None, "not_created")

    current = max(applicable, key=lambda e: (e.version, e.changed_at)).statement
    if current.status == ConsentStatus.WITHDRAWN:
        state = "withdrawn"
    elif as_of < current.valid_from:
        state = "not_yet_effective"
    elif as_of > current.valid_until:
        state = "expired"
    else:
        state = "used"
    return ReconstructedConsent(consent_id, current, state)
