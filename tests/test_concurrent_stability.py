"""并发交叉：实时判定与同意变更同时进行时的一致性与审计完整性。"""
from __future__ import annotations

from datetime import timedelta
from threading import Thread

from gateway.consent import ConsentService
from gateway.decision import DecisionService
from gateway.models import (
    DataCategory,
    DecisionOutcome,
    Purpose,
)
from gateway.registry import RegistryService
from tests.conftest import NOW, active_consent_kwargs, make_request


def test_evaluation_interleaved_with_amends(store):
    """判定与变更交错并发：每个请求恰好一条审计；
    所有判定依据版本都真实存在于历史，结论可解释。"""
    registry = RegistryService(store)
    consents = ConsentService(store, registry)
    decisions = DecisionService(store, consents, registry)
    consents.create(
        **active_consent_kwargs(
            valid_from=NOW - timedelta(days=30), valid_until=NOW + timedelta(days=30)
        )
    )

    # 所有历史上出现过的 (consent_id, version) 集合
    def valid_versions():
        return {
            ("c1", e.version)
            for e in consents.history("c1")
        }

    results = []

    def decide_worker(i):
        d = decisions.evaluate(make_request(f"r-cross-{i}"))
        results.append(d)

    def amend_worker(i):
        try:
            consents.amend(
                "c1",
                categories={DataCategory.DIAGNOSIS, DataCategory.LAB_RESULT}
                if i % 2
                else {DataCategory.DIAGNOSIS},
            )
        except ValueError:
            pass  # 已撤回或交错冲突时的业务拒绝不算并发故障

    threads = [Thread(target=decide_worker, args=(i,)) for i in range(30)]
    threads += [Thread(target=amend_worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 30 个请求各一条判定与审计，无丢失无重复
    assert len(store.list_audits()) == 30
    assert len(store.decisions) == 30
    versions_seen = valid_versions()
    for d in results:
        # 每条判定引用的声明版本必须真实存在于历史中（不会引用半个版本）
        assert set(d.consent_versions) <= versions_seen, d
    # 没有任何请求丢失：30 条结论都产出了
    assert len(results) == 30
    allows = [d for d in results if d.outcome == DecisionOutcome.ALLOW]
    print(
        f"\n[concurrent] 30 evaluations vs 10 amends: "
        f"{len(allows)} allow, all basis versions exist in history "
        f"(final v{store.get_consent('c1').version})"
    )


def test_repeated_withdraw_concurrency_single_final(store):
    """并发撤回同一声明：只追加一条 withdrawn 历史，最终版本唯一。"""
    registry = RegistryService(store)
    consents = ConsentService(store, registry)
    consents.create(**active_consent_kwargs())
    final_versions = []

    def worker():
        final_versions.append(consents.withdraw("c1").version)

    threads = [Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    history = consents.history("c1")
    # v1 created + 恰好一条 withdrawn
    assert [h.action.value for h in history].count("withdrawn") == 1
    assert len({v for v in final_versions}) == 1
    final_v = final_versions[0]
    assert history[-1].version == final_v and len(history) == final_v
    print(f"\n[concurrent] 12 concurrent withdraws -> single withdrawn v{final_v}")
