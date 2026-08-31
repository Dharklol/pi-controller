from __future__ import annotations

import base64
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


class KalshiCredentials:
    def __init__(self, key_id: str, private_key: rsa.RSAPrivateKey):
        self.key_id = key_id
        self.private_key = private_key

    @classmethod
    def from_environment(cls) -> "KalshiCredentials":
        key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
        if not key_id:
            raise RuntimeError("KALSHI_API_KEY_ID is not set")
        if not key_path:
            raise RuntimeError("KALSHI_PRIVATE_KEY_PATH is not set")
        payload = Path(key_path).read_bytes()
        key = serialization.load_pem_private_key(payload, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise RuntimeError("Kalshi private key is not RSA")
        return cls(key_id, key)

    def headers(self, method: str, path: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        path_without_query = path.split("?", 1)[0]
        message = f"{timestamp}{method.upper()}{path_without_query}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("ascii"),
        }
