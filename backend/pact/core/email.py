"""Transactional email: a provider interface and a crash-safe outbox dispatcher.

Nothing here is configured in production. The module ships a clean provider
interface, a no-op provider used until an owner configures a real one, and an
HTTP provider that speaks the Resend API shape. Until ``PACT_EMAIL_PROVIDER`` and
its credentials are set, the outbox still records every message honestly as
``queued`` and the dispatcher reports that no provider is configured — it never
pretends an email was delivered.

Delivery semantics are explicit, because "the API returned 200" is not "the
recipient got it":

    queued -> accepted -> delivered
                       -> bounced
    (or)     -> permanently_failed

``accepted`` means the provider took responsibility for the message. Only a
provider webhook may move a message to ``delivered`` or ``bounced``; the
dispatcher never claims delivery on its own.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from ..config import settings

log = logging.getLogger("pact.email")

#: Delivery states, most-specific first. ``accepted`` is deliberately distinct
#: from ``delivered``.
DELIVERY_QUEUED = "queued"
DELIVERY_ACCEPTED = "accepted"
DELIVERY_DELIVERED = "delivered"
DELIVERY_BOUNCED = "bounced"
DELIVERY_FAILED = "permanently_failed"


@dataclass
class SendResult:
    """The outcome of one send attempt.

    ``accepted`` means the provider accepted the message; ``provider_id`` is its
    identifier, used later to correlate a delivery webhook. ``retryable`` says
    whether the caller should try again (a 5xx or a timeout) as opposed to a
    permanent rejection (a malformed address).
    """

    accepted: bool
    provider_id: str | None = None
    retryable: bool = False
    error: str | None = None


class EmailProvider(Protocol):
    name: str

    def send(self, *, to_email: str, subject: str, body: str, idempotency_key: str) -> SendResult: ...


class NoopProvider:
    """The default provider: configured to send nothing.

    It reports ``accepted=False, retryable=False`` so the dispatcher records the
    message as permanently failed *for now* but leaves it recoverable: enabling a
    real provider and re-draining picks it up. It never reports success.
    """

    name = "none"

    def send(self, *, to_email: str, subject: str, body: str, idempotency_key: str) -> SendResult:
        return SendResult(
            accepted=False,
            retryable=False,
            error="no email provider is configured (set PACT_EMAIL_PROVIDER and its credentials)",
        )


class HttpEmailProvider:
    """A provider that POSTs to an HTTP API (the Resend request shape).

    Idempotency is passed through as ``Idempotency-Key`` where the provider
    supports it, but the outbox does not *rely* on it: the unique idempotency key
    in the database is the primary guard, and provider keys expire (Resend keeps
    them for 24 hours), so they are a second line of defence, not the only one.
    """

    name = "http"

    def __init__(self, *, base_url: str, api_key: str, sender: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.sender = sender
        self.timeout = timeout

    def send(self, *, to_email: str, subject: str, body: str, idempotency_key: str) -> SendResult:
        import json
        import urllib.error
        import urllib.request

        payload = json.dumps(
            {"from": self.sender, "to": [to_email], "subject": subject, "text": body}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/emails",
            data=payload,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key[:256],
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8") or "{}")
            provider_id = (data.get("id") if isinstance(data, dict) else None)
            return SendResult(accepted=True, provider_id=provider_id)
        except urllib.error.HTTPError as exc:
            retryable = exc.code >= 500 or exc.code == 429
            return SendResult(accepted=False, retryable=retryable, error=f"HTTP {exc.code}")
        except Exception as exc:  # timeouts, DNS, connection reset
            return SendResult(accepted=False, retryable=True, error=type(exc).__name__)


def build_provider() -> EmailProvider:
    """Construct the configured provider, or the no-op default.

    A provider that is *named* but missing credentials is a configuration error
    the operator must fix; it is reported by returning the no-op provider, whose
    error message names the problem, rather than crashing the whole app at import.
    """
    name = (settings.email_provider or "none").strip().lower()
    if name in {"none", "", "disabled"}:
        return NoopProvider()
    if name in {"resend", "http"}:
        if not (settings.email_api_key and settings.email_from):
            log.error(
                "PACT_EMAIL_PROVIDER=%s but PACT_EMAIL_API_KEY/PACT_EMAIL_FROM are not set; "
                "email will not be sent.",
                name,
            )
            return NoopProvider()
        return HttpEmailProvider(
            base_url=settings.email_base_url or "https://api.resend.com",
            api_key=settings.email_api_key,
            sender=settings.email_from,
        )
    log.error("Unknown PACT_EMAIL_PROVIDER %r; email will not be sent.", name)
    return NoopProvider()


def render_verification_email(link: str) -> tuple[str, str]:
    subject = "Verify your Pact account"
    body = (
        "Confirm your email address to finish setting up your Pact account.\n\n"
        f"{link}\n\n"
        "This link expires shortly and can be used once. If you did not request it, "
        "you can ignore this message.\n"
    )
    return subject, body


def render_reset_email(link: str) -> tuple[str, str]:
    subject = "Reset your Pact password"
    body = (
        "A password reset was requested for your Pact account.\n\n"
        f"{link}\n\n"
        "This link expires shortly and can be used once. If you did not request it, "
        "no action is needed and your password is unchanged.\n"
    )
    return subject, body


def backoff_for_attempt(attempt: int) -> timedelta:
    """Bounded exponential backoff: 1m, 2m, 4m, 8m, 16m, capped at 1h."""
    minutes = min(60, 2 ** max(0, attempt - 1))
    return timedelta(minutes=minutes)


class OutboxDispatcher:
    """Drains the email outbox, recovering abandoned leases.

    The critical property is that a dispatcher which crashes *after claiming* a
    row but *before* sending it does not strand that row. The claim sets a lease;
    once the lease expires, :meth:`dispatch` reclaims the row (the claim SQL
    selects ``status='sending'`` rows whose lease has passed). A crash after the
    provider accepted the message is safe because the provider idempotency key
    makes the retry a no-op at the provider, and the database idempotency key
    prevents a duplicate row.
    """

    def __init__(self, store, provider: EmailProvider | None = None, *, link_builder=None) -> None:
        self.store = store
        self.provider = provider or build_provider()
        self.link_builder = link_builder

    def dispatch(self, batch: int = 10) -> dict[str, int]:
        """Claim and attempt up to ``batch`` due messages. Returns a small report."""
        messages = self.store.claim_outbox(batch)
        sent = failed = retried = 0
        for msg in messages:
            subject, body = self._render(msg)
            if subject is None:
                # No link could be built (e.g. missing PACT_PUBLIC_URL). Fail
                # permanently rather than sending a useless message.
                self.store.mark_outbox_failed(
                    msg.id, "no link could be built", datetime.now(timezone.utc), terminal=True
                )
                failed += 1
                continue
            result = self.provider.send(
                to_email=msg.to_email,
                subject=subject,
                body=body,
                idempotency_key=msg.idempotency_key,
            )
            if result.accepted:
                self.store.mark_outbox_sent(msg.id, result.provider_id, DELIVERY_ACCEPTED)
                sent += 1
            else:
                terminal = (not result.retryable) or msg.attempts >= _max_attempts()
                next_at = datetime.now(timezone.utc) + backoff_for_attempt(msg.attempts)
                self.store.mark_outbox_failed(msg.id, result.error or "send failed", next_at, terminal)
                if terminal:
                    failed += 1
                else:
                    retried += 1
        return {"claimed": len(messages), "sent": sent, "retried": retried, "failed": failed}

    def _render(self, msg) -> tuple[str | None, str | None]:
        if msg.kind == "verify_email":
            raw = getattr(msg, "_raw_token", None)
            link = self.link_builder("verify", raw) if (self.link_builder and raw) else None
            if not link:
                return None, None
            return render_verification_email(link)
        if msg.kind == "password_reset":
            raw = getattr(msg, "_raw_token", None)
            link = self.link_builder("reset", raw) if (self.link_builder and raw) else None
            if not link:
                return None, None
            return render_reset_email(link)
        return None, None


def _max_attempts() -> int:
    from .auth_store import OUTBOX_MAX_ATTEMPTS

    return OUTBOX_MAX_ATTEMPTS
