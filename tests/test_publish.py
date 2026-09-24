"""Publishing: k-threshold suppression boundaries, stable pseudonyms, no leakage."""
import logging

from app.models import PatientRecord, PublishPolicy
from app.publish import PublishService

logger = logging.getLogger("tests")


def _patients(n_east: int, n_west: int):
    out = [PatientRecord(f"east-{i}", {"region": "east", "age_band": "30-39"}) for i in range(n_east)]
    out += [PatientRecord(f"west-{i}", {"region": "west", "age_band": "30-39"}) for i in range(n_west)]
    return out


def test_threshold_boundary_and_minimization():
    svc = PublishService(PublishPolicy(min_group_size=5))
    report = svc.publish(_patients(5, 4), ("region",))
    logger.info("threshold boundary report released=%d suppressed=%d", len(report.released), len(report.suppressed))

    assert len(report.released) == 1 and report.released[0].count == 5
    assert len(report.suppressed) == 1
    small = report.suppressed[0]
    # suppressed group never carries a count or member pseudonyms
    assert not hasattr(small, "count")
    assert getattr(small, "pseudonyms", None) is None
    # data minimization: only projected attribute appears
    assert dict(report.released[0].group_key) == {"region": "east"}


def test_pseudonym_stable_across_publishes_and_irreversible():
    svc = PublishService(PublishPolicy(min_group_size=1, salt="salt-1"))
    r1 = svc.publish([PatientRecord("patient-1", {"region": "east"})], ("region",))
    r2 = svc.publish([PatientRecord("patient-1", {"region": "east"})], ("region",))
    p1 = r1.released[0].pseudonyms[0]
    p2 = r2.released[0].pseudonyms[0]
    assert p1 == p2
    assert "patient-1" not in p1
    other_salt = PublishService(PublishPolicy(min_group_size=1, salt="salt-2"))
    assert other_salt.pseudonym_for("patient-1") != p1
    logger.info("pseudonym patient-1 -> %s", p1)


def test_threshold_change_does_not_reveal_suppressed_size():
    patients = _patients(6, 2)
    under_k3 = PublishService(PublishPolicy(min_group_size=3)).publish(patients, ("region",))
    under_k5 = PublishService(PublishPolicy(min_group_size=5)).publish(patients, ("region",))
    under_k7 = PublishService(PublishPolicy(min_group_size=7)).publish(patients, ("region",))

    for report in (under_k3, under_k5, under_k7):
        for g in report.suppressed:
            # regardless of threshold, suppressed responses look identical:
            # a group key and a boolean, never a count or membership list
            assert g.suppressed is True
            assert not hasattr(g, "count")
            assert getattr(g, "pseudonyms", None) is None
    logger.info(
        "suppressed under thresholds 3/5/7: %d/%d/%d groups, no counts exposed",
        len(under_k3.suppressed), len(under_k5.suppressed), len(under_k7.suppressed),
    )


def test_all_groups_suppressed_leaks_nothing():
    svc = PublishService(PublishPolicy(min_group_size=10))
    report = svc.publish(_patients(3, 1), ("region", "age_band"))
    assert len(report.released) == 0
    assert len(report.suppressed) == 2
    total_members_exposed = sum(getattr(g, "count", 0) for g in report.released)
    assert total_members_exposed == 0
