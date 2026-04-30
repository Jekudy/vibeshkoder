from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from ops.healing.crypto import decrypt, encrypt


def test_encrypt_decrypt_roundtrip() -> None:
    key = Fernet.generate_key().decode('ascii')
    ciphertext = encrypt('BOT_TOKEN=123456:test\n', key)

    assert ciphertext != 'BOT_TOKEN=123456:test\n'
    assert decrypt(ciphertext, key) == 'BOT_TOKEN=123456:test\n'


def test_wrong_key_fails() -> None:
    ciphertext = encrypt('secret', Fernet.generate_key().decode('ascii'))
    wrong_key = Fernet.generate_key().decode('ascii')

    with pytest.raises(InvalidToken):
        decrypt(ciphertext, wrong_key)


def test_tampered_ciphertext_fails() -> None:
    key = Fernet.generate_key().decode('ascii')
    ciphertext = encrypt('secret', key)
    tampered = ciphertext[:-2] + 'aa'

    with pytest.raises(InvalidToken):
        decrypt(tampered, key)
