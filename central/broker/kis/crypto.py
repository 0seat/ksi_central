"""체결통보 복호화 (AES-256-CBC + base64). 공식 샘플 kis_auth.aes_cbc_base64_dec 와 동일."""

from __future__ import annotations

from base64 import b64decode

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad


def aes_cbc_base64_decrypt(key: str, iv: str, cipher_text: str) -> str:
    cipher = AES.new(key.encode("utf-8"), AES.MODE_CBC, iv.encode("utf-8"))
    return unpad(cipher.decrypt(b64decode(cipher_text)), AES.block_size).decode("utf-8")
