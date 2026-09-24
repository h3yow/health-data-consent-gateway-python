"""Data minimization & de-identified publishing with k-threshold suppression."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone

from .models import (
    PatientRecord,
    PublishPolicy,
    PublishedGroup,
    PublishReport,
    SuppressedGroup,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PublishService:
    def __init__(self, policy: PublishPolicy | None = None) -> None:
        self._policy = policy or PublishPolicy()

    @property
    def policy(self) -> PublishPolicy:
        return self._policy

    def pseudonym_for(self, patient_id: str) -> str:
        """Stable, irreversible pseudonym: keyed HMAC over the patient id.

        The same patient maps to the same pseudonym across publishes (given the
        same salt), and the mapping cannot be inverted without the salt."""
        digest = hmac.new(
            self._policy.salt.encode("utf-8"),
            patient_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return digest[: self._policy.pseudonym_length]

    def publish(
        self,
        patients: list[PatientRecord],
        attributes: tuple[str, ...],
        now: datetime | None = None,
    ) -> PublishReport:
        """Group patients by the given quasi-identifier attributes (data
        minimization: only these attributes are projected) and release only
        groups meeting the size threshold.

        Suppressed groups expose neither their count nor their membership, so
        threshold/policy changes cannot be used to infer individuals via count
        or response differences."""
        groups: dict[tuple[tuple[str, str], ...], list[str]] = {}
        for patient in patients:
            key = tuple(sorted((attr, patient.attributes.get(attr, "")) for attr in attributes))
            groups.setdefault(key, []).append(patient.patient_id)

        released: list[PublishedGroup] = []
        suppressed: list[SuppressedGroup] = []
        for key in sorted(groups):
            members = groups[key]
            if len(members) >= self._policy.min_group_size:
                released.append(
                    PublishedGroup(
                        group_key=key,
                        count=len(members),
                        pseudonyms=tuple(sorted(self.pseudonym_for(pid) for pid in members)),
                    )
                )
            else:
                suppressed.append(SuppressedGroup(group_key=key))

        return PublishReport(
            released=tuple(released),
            suppressed=tuple(suppressed),
            policy=self._policy,
            published_at=now or _now(),
        )
