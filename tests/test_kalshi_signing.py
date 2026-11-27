"""Kalshi signs Ed25519 directly and RSA with PSS. The test button shows the error."""

from __future__ import annotations

import base64

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from src.clients.kalshi_client import KalshiAPIError, KalshiClient, sign_kalshi_message
from src.journal.web import probe_balance, public_probe_error


def _ed25519_pem() -> str:
    key = ed25519.Ed25519PrivateKey.generate()
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def _rsa_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()


def test_ed25519_signs_the_message_bytes_only():
    pem = _ed25519_pem()
    client = KalshiClient(api_key="k" * 36, private_key_pem=pem)
    message = b"1703123456789GET/trade-api/v2/portfolio/balance"
    signature = base64.b64decode(
        client._sign_request(
            "1703123456789",
            "get",
            "/trade-api/v2/portfolio/balance?limit=5",
        )
    )
    assert len(signature) == 64
    client.private_key.public_key().verify(signature, message)


def test_rsa_still_uses_pss_sha256():
    pem = _rsa_pem()
    client = KalshiClient(api_key="k" * 36, private_key_pem=pem)
    message = b"1703123456789GET/trade-api/v2/portfolio/balance"
    signature = base64.b64decode(
        client._sign_request("1703123456789", "GET", "/trade-api/v2/portfolio/balance")
    )
    client.private_key.public_key().verify(
        signature,
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def test_unsupported_key_type_is_named_without_key_material():
    with pytest.raises(KalshiAPIError, match="Unsupported private key type"):
        sign_kalshi_message(object(), b"msg")


def test_probe_error_keeps_the_signer_text_and_drops_a_pem():
    shown = public_probe_error(
        KalshiAPIError(
            "Failed to sign request: Ed25519PrivateKey.sign() takes 1 positional arguments but 3 were given"
        )
    )
    assert "Ed25519PrivateKey.sign()" in shown
    assert "Connection failed:" in shown
    header = "-----" + "BEGIN PRIVATE KEY" + "-----"
    hidden = public_probe_error(RuntimeError(header + "\nabc"))
    assert "BEGIN" not in hidden
    assert "PRIVATE KEY" not in hidden


@pytest.mark.asyncio
async def test_probe_balance_shows_the_exception_text(monkeypatch):
    pem = _ed25519_pem()

    async def boom(self):
        raise KalshiAPIError(
            "Failed to sign request: Ed25519PrivateKey.sign() takes 1 positional arguments but 3 were given"
        )

    monkeypatch.setattr(KalshiClient, "get_balance", boom)
    text = await probe_balance("k" * 36, pem)
    assert "Ed25519PrivateKey.sign()" in text
    assert "BEGIN" not in text
