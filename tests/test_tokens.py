"""Cryptographic authority token boundaries for the reference PDP."""
from unittest.mock import patch

from app.tokens import AuthorityTokenIssuer


def scope():
    return {
        "sub": "FCA-01",
        "engagementId": "ENG-01",
        "requestId": "REQ-01",
        "trackId": "TRK-01",
        "effectorId": "EFF-01",
        "engagementType": "EW_DEFEAT",
        "policyVersion": "POLICY-1",
        "weaponsControlStatus": "WEAPONS_TIGHT",
        "trackSnapshotTimeObserved": "2026-01-01T00:00:00Z",
    }


def test_token_signature_scope_and_single_use():
    issuer = AuthorityTokenIssuer("a-test-key-that-is-long-enough-for-hmac-material", "NODE-1")
    token, claims = issuer.mint(**scope())
    assert claims["exp"] > claims["iat"]
    assert issuer.inspect(token, {"effectorId": "EFF-01"}).valid is True
    assert issuer.inspect(token, {"effectorId": "EFF-WRONG"}).reason.startswith(
        "SCOPE_MISMATCH"
    )
    assert issuer.consume(token, {"engagementId": "ENG-01"}).valid is True
    assert issuer.consume(token, {"engagementId": "ENG-01"}).reason == "TOKEN_ALREADY_USED"


def test_token_tampering_and_expiry_fail_closed():
    issuer = AuthorityTokenIssuer("a-test-key-that-is-long-enough-for-hmac-material", "NODE-1", 2)
    with patch("app.tokens.time.time", return_value=100):
        token, _ = issuer.mint(**scope())
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert issuer.inspect(tampered).reason == "INVALID_SIGNATURE"
    with patch("app.tokens.time.time", return_value=103):
        assert issuer.inspect(token).reason == "TOKEN_EXPIRED"
