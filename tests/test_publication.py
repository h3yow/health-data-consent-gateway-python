"""发布：去标识化假名、小样本抑制阈值与防推断。"""
import pytest

from gateway.models import Patient, PublicationRow
from gateway.publication import PublicationError, PublicationService
from gateway.storage import GatewayStore


def _populate(store: GatewayStore, diagnosis: str, n: int, prefix: str) -> list[str]:
    ids = [f"{prefix}-{i}" for i in range(n)]
    for pid in ids:
        store.add_patient(Patient(pid, "某患者", 1985, diagnosis=diagnosis))
    return ids


def test_pseudonym_stable_and_irreversible(publisher: PublicationService):
    p1 = publisher.pseudonym_for("p1")
    p2 = publisher.pseudonym_for("p1")
    assert p1 == p2  # 同一患者跨发布稳定
    assert p1.startswith("ps_") and "p1" not in p1
    # 不同盐值产生不同假名：无法跨域关联，且无法由假名反推身份
    other = PublicationService(GatewayStore(), salt=b"other-salt", min_group_size=3)
    assert other.pseudonym_for("p1") != p1
    print(f"\n[publish] pseudonym(p1)={p1} (stable, salted)")


def test_threshold_boundary(store: GatewayStore, publisher: PublicationService):
    """样本量边界：恰好达到阈值放行，差一例即抑制。"""
    _populate(store, "flu", 3, "flu")      # == min_group_size(3) -> 发布
    _populate(store, "rare", 2, "rare")    # < 3 -> 抑制
    rows = publisher.publish_group_counts("diagnosis")
    by_key = {r.group_key: r for r in rows}
    print(f"\n[publish] groups={[(r.group_key, r.count) for r in rows]} threshold={publisher.min_group_size}")
    assert by_key["flu"].count == 3
    assert "rare" not in by_key  # 小样本分组整体省略，而非 0 计数行


def test_suppression_not_leakable_via_response(store: GatewayStore):
    """阈值调整后，不得通过计数、错误信息或响应差异反推被抑制个体。"""
    _populate(store, "rare", 2, "rare")
    pub3 = PublicationService(store, salt=b"s", min_group_size=3)
    pub2 = PublicationService(store, salt=b"s", min_group_size=2)

    rows3 = pub3.publish_group_counts("diagnosis")
    rows2 = pub2.publish_group_counts("diagnosis")
    # 阈值 3 时：响应中完全没有 rare 组的痕迹（无 0 行、无占位符）
    assert rows3 == []
    # 阈值 2 时放行；两次响应的差异仅体现在是否包含该组，
    # 被抑制时不产生任何指向该组的计数或错误信息
    assert [r.count for r in rows2] == [2]
    # 假名在两次发布中稳定
    assert rows2[0].pseudonym_ids == tuple(sorted(pub3.pseudonym_for(f"rare-{i}") for i in range(2)))


def test_unknown_dimension_generic_error(publisher: PublicationService):
    with pytest.raises(PublicationError) as exc:
        publisher.publish_group_counts("home_address")
    # 错误信息为通用文案，不携带任何个体或分组信息
    assert str(exc.value) == "不支持的发布分组维度"


def test_data_minimization(store: GatewayStore, publisher: PublicationService):
    _populate(store, "flu", 4, "flu")
    rows = publisher.publish_group_counts("diagnosis")
    row: PublicationRow = rows[0]
    # 只输出分组键、计数与假名；假名中不含患者 ID
    assert set(row.pseudonym_ids).isdisjoint({f"flu-{i}" for i in range(4)})
    assert all(p.startswith("ps_") for p in row.pseudonym_ids)
    assert row.count == 4
