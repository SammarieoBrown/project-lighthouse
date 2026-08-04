"""Fail-closed settlement executor boundary.

No real payment provider is implemented in this release.  The only available
adapter is a deterministic, in-process demo executor, and it exists solely when
``DISBURSEMENT_EXECUTOR_MODE=simulated`` is configured explicitly.  Its receipt
is evidence of a simulated confirmation, never evidence that money moved.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from lighthouse_contracts import DisbursementChannel

from .config import Settings

SIMULATED_EXECUTOR_PROVIDER = "LIGHTHOUSE_DEMO_EXECUTOR_V1"
SIMULATED_EXECUTOR_PROVENANCE = "SIMULATED_DEMO"


class ExecutorUnavailable(Exception):
    """Raised when execution is disabled rather than guessing a payment rail."""


@dataclass(frozen=True, slots=True)
class ProviderConfirmation:
    provider: str
    provenance: str
    simulated: bool
    reference: str
    confirmed_at: datetime
    receipt_hash: str


def _receipt_hash(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class SimulatedDemoExecutor:
    """Return a local demo receipt without performing network or money I/O."""

    provider = SIMULATED_EXECUTOR_PROVIDER
    provenance = SIMULATED_EXECUTOR_PROVENANCE
    simulated = True

    def execute(
        self,
        *,
        disbursement_id: uuid.UUID,
        disbursement_snapshot_hash: str,
        request_hash: str,
        amount: Decimal,
        currency: str,
        channel: DisbursementChannel,
        executed_at: datetime,
        now: datetime | None = None,
    ) -> ProviderConfirmation:
        confirmed_at = now or datetime.now(UTC)
        reference_material = (
            f"{self.provider}:{disbursement_id}:{disbursement_snapshot_hash}:"
            f"{request_hash}"
        )
        reference = (
            "DEMO-"
            + hashlib.sha256(reference_material.encode("utf-8"))
            .hexdigest()[:24]
            .upper()
        )
        receipt = {
            "provider": self.provider,
            "provenance": self.provenance,
            "simulated": True,
            "reference": reference,
            "disbursement_snapshot_hash": disbursement_snapshot_hash,
            "request_hash": request_hash,
            "amount": f"{amount:.2f}",
            "currency": currency,
            "channel": str(channel),
            "executed_at": executed_at.isoformat(),
            "confirmed_at": confirmed_at.isoformat(),
        }
        return ProviderConfirmation(
            provider=self.provider,
            provenance=self.provenance,
            simulated=True,
            reference=reference,
            confirmed_at=confirmed_at,
            receipt_hash=_receipt_hash(receipt),
        )


def configured_executor(settings: Settings) -> SimulatedDemoExecutor:
    if settings.disbursement_executor_mode != "simulated":
        raise ExecutorUnavailable(
            "disbursement execution is disabled; no payment provider is configured"
        )
    return SimulatedDemoExecutor()


__all__ = [
    "ExecutorUnavailable",
    "ProviderConfirmation",
    "SIMULATED_EXECUTOR_PROVENANCE",
    "SIMULATED_EXECUTOR_PROVIDER",
    "SimulatedDemoExecutor",
    "configured_executor",
]
