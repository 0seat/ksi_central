"""체결통보 복호화 (AES-256-CBC + base64). 공식 샘플 kis_auth.aes_cbc_base64_dec 와 동일.

Windows 에서는 OS 내장 CNG(bcrypt.dll)를 쓴다. 스마트 앱 컨트롤이 켜진 PC 에서는
pycryptodome 의 서명 없는 네이티브 모듈(_raw_cbc.pyd) 로드가 차단되기 때문이다 (2026-10-19 실측).
"""

from __future__ import annotations

import sys
from base64 import b64decode

BLOCK = 16


def _unpad(data: bytes) -> bytes:
    n = data[-1] if data else 0
    if not 1 <= n <= BLOCK or data[-n:] != bytes([n]) * n:
        raise ValueError("PKCS7 패딩 오류")
    return data[:-n]


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _bcrypt = ctypes.WinDLL("bcrypt.dll")
    _alg = ctypes.c_void_p()

    def _check(status: int, what: str) -> None:
        if status != 0:
            raise OSError(f"{what} 실패 NTSTATUS=0x{status & 0xFFFFFFFF:08X}")

    def _provider() -> ctypes.c_void_p:
        if not _alg.value:
            _check(_bcrypt.BCryptOpenAlgorithmProvider(ctypes.byref(_alg), "AES", None, 0), "BCryptOpenAlgorithmProvider")
            mode = ctypes.create_unicode_buffer("ChainingModeCBC")
            _check(_bcrypt.BCryptSetProperty(_alg, "ChainingMode", mode, ctypes.sizeof(mode), 0), "BCryptSetProperty")
        return _alg

    def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        """패딩 제거 전 평문."""
        if len(data) % BLOCK:
            raise ValueError("암호문 길이가 16의 배수가 아님")
        h_key = ctypes.c_void_p()
        _check(_bcrypt.BCryptGenerateSymmetricKey(_provider(), ctypes.byref(h_key), None, 0, key, len(key), 0),
               "BCryptGenerateSymmetricKey")
        try:
            iv_buf = ctypes.create_string_buffer(iv, len(iv))      # CNG 가 IV 버퍼를 덮어쓴다
            out = ctypes.create_string_buffer(len(data))
            got = wintypes.ULONG()
            _check(_bcrypt.BCryptDecrypt(h_key, data, len(data), None, iv_buf, len(iv), out, len(data),
                                         ctypes.byref(got), 0), "BCryptDecrypt")
            return out.raw[:got.value]
        finally:
            _bcrypt.BCryptDestroyKey(h_key)

else:
    from Crypto.Cipher import AES

    def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        """패딩 제거 전 평문."""
        return AES.new(key, AES.MODE_CBC, iv).decrypt(data)


def aes_cbc_base64_decrypt(key: str, iv: str, cipher_text: str) -> str:
    return _unpad(aes_cbc_decrypt(key.encode("utf-8"), iv.encode("utf-8"), b64decode(cipher_text))).decode("utf-8")
