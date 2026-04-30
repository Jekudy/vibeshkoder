from __future__ import annotations

from cryptography.fernet import Fernet


def _key_bytes(key: str) -> bytes:
    return key.encode('ascii')


def encrypt(plaintext: str, key: str) -> str:
    return Fernet(_key_bytes(key)).encrypt(plaintext.encode('utf-8')).decode('ascii')


def decrypt(ciphertext: str, key: str) -> str:
    return Fernet(_key_bytes(key)).decrypt(ciphertext.encode('ascii')).decode('utf-8')
