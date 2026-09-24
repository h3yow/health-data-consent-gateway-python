"""访问判定服务：同意匹配、幂等判定与审计留痕。"""
from __future__ import annotations

from datetime import datetime

from .consent import ConsentService
from .models import AccessRequest, Decision
from .storage import GatewayStore


class DecisionService:
    def __init__(self, store: GatewayStore, consents: ConsentService) -> None:
        self._store = store
        self._consents = consents

    def evaluate(self, request: AccessRequest) -> Decision:
        """判定访问请求。同一 request_id 幂等：重复/并发提交返回同一结论。"""
        raise NotImplementedError

    def _evaluate_fresh(self, request: AccessRequest, now: datetime) -> Decision:
        """基于当前同意快照做出新判定（含审计依据）。"""
        raise NotImplementedError
