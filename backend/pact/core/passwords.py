"""Password hashing with Argon2id.

Parameters follow the approved specification: 64 MiB of memory, 3 iterations, and
a parallelism of 1. These are the OWASP-recommended minimums for Argon2id and fit
comfortably inside a 1024 MB serverless function, but the memory cost is real: a
handful of concurrent hashes will each hold 64 MiB, so hashing must not be fanned
out without bound (see ``docs/AUTH.md``).

The pepper is optional and is never hard-coded. It is read from
``PACT_PASSWORD_PEPPER`` and, when set, is mixed into the password before hashing.
A pepper is a defence in depth against a database-only compromise (the attacker
has the hashes but not the application secret); it is *not* a substitute for
per-user salts, which Argon2 includes by construction. If the pepper is not
configured the module still works — it simply omits that extra layer — and the
stored string records which scheme produced it so verification keeps working if
the pepper is added later.
"""

from __future__ import annotations

import base64
import hmac
import os
import secrets
from typing import Final

#: Argon2id parameters, fixed by the specification.
MEMORY_KIB: Final = 64 * 1024  # 64 MiB
TIME_COST: Final = 3
PARALLELISM: Final = 1
HASH_LEN: Final = 32
SALT_LEN: Final = 16

#: Prefix of a stored hash. ``v1`` is argon2id-without-pepper; ``v1p`` is
#: argon2id-with-pepper. Recording the scheme lets a pepper be introduced later
#: without invalidating existing credentials.
SCHEME_PLAIN: Final = "v1"
SCHEME_PEPPERED: Final = "v1p"


def _pepper() -> bytes:
    return (os.getenv("PACT_PASSWORD_PEPPER") or "").encode("utf-8")


def _argon2id(password: bytes, salt: bytes, *, time_cost: int = TIME_COST) -> bytes:
    from argon2.low_level import Type, hash_secret_raw

    return hash_secret_raw(
        secret=password,
        salt=salt,
        time_cost=time_cost,
        memory_cost=MEMORY_KIB,
        parallelism=PARALLELISM,
        hash_len=HASH_LEN,
        type=Type.ID,
    )


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str, *, pepper: str | None = None) -> str:
    """Hash ``password`` and return a self-describing string.

    Format: ``<scheme>$<time_cost>$<b64-salt>$<b64-hash>``. The scheme records
    whether a pepper was applied, so verification can reproduce it exactly. The
    password itself is never stored or logged.
    """
    if not password:
        raise ValueError("password must not be empty")
    effective_pepper = _pepper() if pepper is None else pepper.encode("utf-8")
    salt = secrets.token_bytes(SALT_LEN)
    material = password.encode("utf-8") + effective_pepper
    digest = _argon2id(material, salt)
    scheme = SCHEME_PEPPERED if effective_pepper else SCHEME_PLAIN
    return f"{scheme}${TIME_COST}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str, *, pepper: str | None = None) -> bool:
    """Constant-time verification of ``password`` against a stored hash.

    Returns False for any malformed stored value rather than raising, so a
    corrupted row cannot be turned into an exception that leaks its shape. The
    comparison uses ``hmac.compare_digest`` so it does not short-circuit.
    """
    try:
        scheme, time_cost_text, salt_text, hash_text = stored.split("$")
        time_cost = int(time_cost_text)
        salt = _unb64(salt_text)
        expected = _unb64(hash_text)
    except (ValueError, TypeError):
        return False

    if scheme not in {SCHEME_PLAIN, SCHEME_PEPPERED}:
        return False

    effective_pepper = _pepper() if pepper is None else pepper.encode("utf-8")
    if scheme == SCHEME_PLAIN:
        effective_pepper = b""

    material = password.encode("utf-8") + effective_pepper
    try:
        actual = _argon2id(material, salt, time_cost=time_cost)
    except Exception:  # pragma: no cover - defensive (bad parameters in a row)
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    """Whether a stored hash should be upgraded on next successful login.

    True when the parameters or scheme no longer match the current configuration —
    for example after the pepper is introduced, or the cost is raised. A caller
    that has just verified the plaintext can transparently re-hash and store the
    new value.
    """
    try:
        scheme, time_cost_text, _, _ = stored.split("$")
        time_cost = int(time_cost_text)
    except (ValueError, TypeError):
        return True
    current_scheme = SCHEME_PEPPERED if _pepper() else SCHEME_PLAIN
    return scheme != current_scheme or time_cost != TIME_COST
