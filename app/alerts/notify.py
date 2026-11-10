"""Family notification — swappable by design.

Telegram and SMS are deliberately not implemented. The brief's requirement is
that the channel is swappable and that the family is told; the family page is
the demonstrated channel, and adding Telegram later is one small class.

Everything here is I/O-light and safe to call from the alert path.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AlertPayload:
    call_id: str
    score: float
    signals: list[str]
    reason_hi: str
    reason_en: str
    summary: str
    at: float = field(default_factory=time.time)


@runtime_checkable
class Notifier(Protocol):
    async def notify(self, alert: AlertPayload) -> None: ...


class ConsoleNotifier:
    """Writes to the log. Useful in dev and in the eval harness."""

    async def notify(self, alert: AlertPayload) -> None:
        log.warning(
            "FAMILY ALERT call=%s risk=%.2f signals=%s reason=%s",
            alert.call_id, alert.score, ",".join(alert.signals), alert.reason_en,
        )


class CollectingNotifier:
    """Keeps alerts in memory so tests and the eval harness can assert on them."""

    def __init__(self) -> None:
        self.alerts: list[AlertPayload] = []

    async def notify(self, alert: AlertPayload) -> None:
        self.alerts.append(alert)


class NotifierHub:
    """Fan out to every configured channel.

    A failing channel must never break the alert path, so exceptions are
    swallowed per-channel and logged.
    """

    def __init__(self, notifiers: list[Notifier] | None = None) -> None:
        self._notifiers: list[Notifier] = list(notifiers or [])

    def add(self, notifier: Notifier) -> None:
        self._notifiers.append(notifier)

    async def notify(self, alert: AlertPayload) -> None:
        for notifier in self._notifiers:
            try:
                await notifier.notify(alert)
            except Exception:  # noqa: BLE001 - never break the call loop
                log.exception("notifier %s failed", type(notifier).__name__)
