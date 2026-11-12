"""TCL Plus 登录使用的 RSA / MD5 工具。

TCL 的登录接口对 URL 参数和请求体分别做了 RSA(PKCS1 v1.5) 加密，
全部使用同一把 1024 位 LoginSDK 公钥。
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import urllib.parse
from functools import lru_cache

PUBLIC_KEY_LOGIN_SDK = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDLuCAxtV1Omu216OFdY0p2ypPR"
    "LptloLgMqvpmgkXD/SaB5RPx5oTzo5fdWjeYAx8N6YAe0DDJD5LsmNGhvVIiKOz2"
    "wYI17DQRK6aymvBuxioQzeAd5vI8RItTS7QpNh/ABH/B/3XhhVwnXn40MdDQxA3E"
    "2yfEk327Kqy4TqtscwIDAQAB"
)

_KEY_SIZE = 128  # 1024 bit / 8
_BLOCK_SIZE = _KEY_SIZE - 11  # PKCS1 v1.5 单块最大明文


@lru_cache(maxsize=1)
def _public_key():
    from cryptography.hazmat.primitives import serialization

    return serialization.load_der_public_key(base64.b64decode(PUBLIC_KEY_LOGIN_SDK))


def md5_hash(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def _rsa_encrypt(plaintext: bytes) -> bytes:
    from cryptography.hazmat.primitives.asymmetric import padding

    key = _public_key()
    out = b""
    offset = 0
    while offset < len(plaintext):
        out += key.encrypt(plaintext[offset : offset + _BLOCK_SIZE], padding.PKCS1v15())
        offset += _BLOCK_SIZE
    return out


def encrypt_url_params(params: dict) -> str:
    parts = []
    for key, value in params.items():
        blob = base64.b64encode(_rsa_encrypt(str(value).encode("utf-8"))).decode("ascii")
        parts.append(f"{key}={urllib.parse.quote(blob, safe='')}")
    return "&".join(parts)


def encrypt_body(params: dict) -> bytes:
    raw = json.dumps(params, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(_rsa_encrypt(raw))


def generate_sign(params: dict) -> str:
    sign_str = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return md5_hash(sign_str + PUBLIC_KEY_LOGIN_SDK)


def generate_nonce() -> str:
    return "02" + str(random.randint(0, 0x98967F))
