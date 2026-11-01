#!/usr/bin/env python
"""Mint a Pact bearer token.

Usage:
    python -m scripts.issue_token <subject> <buyer|seller|admin> [--days N]

The signing secret is read from PACT_AUTH_SECRET (the same value the API uses).
The token is printed to stdout and nothing else, so it can be piped straight into
a shell variable:

    TOKEN=$(python -m scripts.issue_token demo-buyer buyer)
    curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/jobs

A token is a credential. Do not commit one, paste one into a public issue, or
bake an admin token into a public frontend bundle.
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta

from pact.api.auth import DEFAULT_TOKEN_TTL, VALID_ROLES, issue_token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mint a Pact bearer token.")
    parser.add_argument("subject", help="Who the token is for, e.g. demo-buyer")
    parser.add_argument("role", choices=sorted(VALID_ROLES), help="The role to grant")
    parser.add_argument(
        "--days",
        type=float,
        default=DEFAULT_TOKEN_TTL.days,
        help=f"Token lifetime in days (default {DEFAULT_TOKEN_TTL.days})",
    )
    args = parser.parse_args(argv)

    try:
        token = issue_token(args.subject, args.role, ttl=timedelta(days=args.days))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "Set PACT_AUTH_SECRET first, e.g. "
            'export PACT_AUTH_SECRET="$(python -c \'import secrets; '
            "print(secrets.token_urlsafe(48))\')",
            file=sys.stderr,
        )
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
