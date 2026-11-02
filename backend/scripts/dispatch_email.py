#!/usr/bin/env python
"""Drain the transactional-email outbox.

Usage:
    python -m scripts.dispatch_email [--batch N] [--loop]

Claims due outbox rows under a lease and hands them to the configured provider
(see PACT_EMAIL_PROVIDER). With no provider configured this is a no-op that
records each message honestly as queued — it never claims an email was sent.

Run it on a schedule (a platform cron, a worker, or manually). ``--loop`` keeps
draining until the outbox is empty, which is useful right after enabling a
provider so messages queued while it was unconfigured are delivered.

Nothing here prints a token or a credential; only counts.
"""

from __future__ import annotations

import argparse
import json

from pact.core.accounts import accounts
from pact.core.auth_store import auth_store
from pact.core.email import OutboxDispatcher


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drain the Pact email outbox.")
    parser.add_argument("--batch", type=int, default=25, help="Messages per claim (default 25)")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep draining until a pass claims nothing (bounded by the outbox size)",
    )
    args = parser.parse_args(argv)

    dispatcher = OutboxDispatcher(auth_store, link_builder=accounts._public_link)

    total = {"claimed": 0, "sent": 0, "retried": 0, "failed": 0}
    while True:
        report = dispatcher.dispatch(batch=args.batch)
        for key in total:
            total[key] += report.get(key, 0)
        if not args.loop or report["claimed"] == 0:
            break

    print(json.dumps(total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
