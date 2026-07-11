"""Scoped authority tokens for the reference Policy Decision Point.

The compact token format intentionally uses only the Python standard library so
the reference node can demonstrate cryptographic scoping without adding a JWT
implementation dependency.  It is *not* a substitute for a fielded PKI/HSM: in a
deployment the signing key is supplied by the accredited key-management service
and effectors verify tokens locally.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@dataclass(frozen=True)
class TokenResult:
    valid: bool
    reason: str
    claims: dict[str, Any] | None = None


class AuthorityTokenIssuer:
    """Mint, verify, and consume HMAC signed, single-use authority tokens."""

    def __init__(self, secret: str | bytes, issuer: str, ttl_seconds: int = 20) -> None:
        key = secret.encode("utf-8") if isinstance(secret, str) else secret
        if len(key) < 32:
            # A configured weak secret must not silently become the effective key.
            # Derivation gives the HMAC a fixed-size key while the `ephemeral`
            # health flag still makes the demo trust boundary visible.
            key = hashlib.sha256(key).digest()
        self._key = key
        self.issuer = issuer
        self.ttl_seconds = max(1, min(int(ttl_seconds), 300))
        self._issued: dict[str, int] = {}
        self._consumed: set[str] = set()
        self._lock = threading.Lock()

    @staticmethod
    def ephemeral_secret() -> str:
        return secrets.token_urlsafe(48)

    def mint(self, **scope: Any) -> tuple[str, dict[str, Any]]:
        now = int(time.time())
        claims: dict[str, Any] = {
            "jti": secrets.token_urlsafe(18),
            "iss": self.issuer,
            "iat": now,
            "exp": now + self.ttl_seconds,
            **scope,
        }
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
        encoded = _b64url(payload)
        signature = _b64url(hmac.new(self._key, f"v1.{encoded}".encode(), hashlib.sha256).digest())
        token = f"v1.{encoded}.{signature}"
        with self._lock:
            self._prune_locked(now)
            self._issued[claims["jti"]] = claims["exp"]
        return token, claims

    def inspect(self, token: str, expected: dict[str, Any] | None = None) -> TokenResult:
        """Verify signature, expiry, issuer, issuance, and exact expected scopes."""
        try:
            version, encoded, supplied_sig = token.split(".", 2)
            if version != "v1":
                return TokenResult(False, "UNSUPPORTED_TOKEN_VERSION")
            expected_sig = _b64url(
                hmac.new(self._key, f"v1.{encoded}".encode(), hashlib.sha256).digest()
            )
            if not hmac.compare_digest(supplied_sig, expected_sig):
                return TokenResult(False, "INVALID_SIGNATURE")
            claims = json.loads(_unb64url(encoded))
            if not isinstance(claims, dict):
                return TokenResult(False, "INVALID_CLAIMS")
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error):
            return TokenResult(False, "MALFORMED_TOKEN")

        now = int(time.time())
        if claims.get("iss") != self.issuer:
            return TokenResult(False, "WRONG_ISSUER")
        if not isinstance(claims.get("iat"), int) or not isinstance(claims.get("exp"), int):
            return TokenResult(False, "INVALID_TIME_SCOPE")
        if claims["iat"] > now + 5:
            return TokenResult(False, "TOKEN_NOT_YET_VALID")
        if claims["exp"] <= now:
            return TokenResult(False, "TOKEN_EXPIRED")
        if claims["exp"] - claims["iat"] > 300:
            return TokenResult(False, "TOKEN_LIFETIME_EXCEEDED")

        jti = claims.get("jti")
        if not isinstance(jti, str):
            return TokenResult(False, "INVALID_JTI")
        with self._lock:
            self._prune_locked(now)
            if jti not in self._issued:
                return TokenResult(False, "UNKNOWN_TOKEN")
            if jti in self._consumed:
                return TokenResult(False, "TOKEN_ALREADY_USED")

        for name, value in (expected or {}).items():
            if claims.get(name) != value:
                return TokenResult(False, f"SCOPE_MISMATCH_{name.upper()}")
        return TokenResult(True, "OK", claims)

    def consume(self, token: str, expected: dict[str, Any] | None = None) -> TokenResult:
        result = self.inspect(token, expected)
        if not result.valid or result.claims is None:
            return result
        jti = result.claims["jti"]
        with self._lock:
            if jti in self._consumed:
                return TokenResult(False, "TOKEN_ALREADY_USED")
            self._consumed.add(jti)
        return result

    def _prune_locked(self, now: int) -> None:
        expired = [jti for jti, exp in self._issued.items() if exp <= now]
        for jti in expired:
            self._issued.pop(jti, None)
            self._consumed.discard(jti)
