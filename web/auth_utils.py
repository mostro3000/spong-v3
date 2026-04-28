"""Authentication helpers for SPONG web Basic Auth."""

from __future__ import annotations

import hmac

from werkzeug.security import check_password_hash


_HASH_PREFIXES = ("scrypt:", "pbkdf2:")


def _looks_like_hash(value: str) -> bool:
    return value.startswith(_HASH_PREFIXES) and "$" in value


def _check_hash(stored_hash: str, password: str) -> bool:
    try:
        return check_password_hash(stored_hash, password)
    except (TypeError, ValueError):
        return False


def check_basic_auth(
    username: str | None,
    password: str | None,
    expected_user: str | None,
    expected_password: str | None = "",
    expected_password_hash: str | None = "",
) -> bool:
    """Validate a Basic Auth user/password against plain or hashed config.

    Hash config takes precedence. Plain config remains supported for backward
    compatibility, and may also contain a Werkzeug hash directly.
    """
    expected_user = expected_user or ""
    if not expected_user:
        return True
    if username is None or password is None:
        return False
    if not hmac.compare_digest(username, expected_user):
        return False

    stored_hash = expected_password_hash or ""
    if stored_hash:
        return _check_hash(stored_hash, password)

    stored_password = expected_password or ""
    if _looks_like_hash(stored_password):
        return _check_hash(stored_password, password)
    return hmac.compare_digest(password, stored_password)
