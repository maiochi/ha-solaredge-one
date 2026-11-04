"""Login no AWS Cognito por usuário e senha (protocolo SRP).

O app client do portal só aceita USER_SRP_AUTH (USER_PASSWORD_AUTH vem
desabilitado), então a senha nunca sai daqui: o servidor recebe apenas
uma prova de que o cliente a conhece. Implementação equivalente à do
AWS Amplify, sem dependências externas.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import hashlib
import hmac
import os
from typing import Any

# Grupo de 3072 bits da RFC 3526 (o mesmo usado pelo Cognito), gerador 2
_N_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3DC2007CB8A163BF05"
    "98DA48361C55D39A69163FA8FD24CF5F83655D23DCA3AD961C62F356208552BB"
    "9ED529077096966D670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B"
    "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9DE2BCBF695581718"
    "3995497CEA956AE515D2261898FA051015728E5A8AAAC42DAD33170D04507A33"
    "A85521ABDF1CBA64ECFB850458DBEF0A8AEA71575D060C7DB3970F85A6E1E4C7"
    "ABF5AE8CDB0933D71E8C94E04A25619DCEE3D2261AD2EE6BF12FFA06D98A0864"
    "D87602733EC86A64521F2B18177B200CBBE117577A615D6C770988C0BAD946E2"
    "08E24FA074E5AB3143DB5BFCE0FD108E4B82D120A93AD2CAFFFFFFFFFFFFFFFF"
)
_G_HEX = "2"
_N = int(_N_HEX, 16)
_G = int(_G_HEX, 16)
_INFO_BITS = b"Caldera Derived Key"

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def _hash_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().rjust(64, "0")


def _hex_hash(hex_string: str) -> str:
    return _hash_hex(bytes.fromhex(hex_string))


def _pad_hex(value: int | str) -> str:
    """Hex com número par de dígitos e sem bit de sinal (como no Amplify)."""
    text = value if isinstance(value, str) else f"{value:x}"
    if len(text) % 2:
        return "0" + text
    if text[0] in "89abcdefABCDEF":
        return "00" + text
    return text


def _hkdf(ikm: bytes, salt: bytes) -> bytes:
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, _INFO_BITS + b"\x01", hashlib.sha256).digest()[:16]


def _timestamp(now: datetime | None = None) -> str:
    """Ex.: 'Wed Nov 4 13:05:09 UTC 2026' (dia sem zero, nomes em inglês)."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return (
        f"{_WEEKDAYS[now.weekday()]} {_MONTHS[now.month - 1]} {now.day} "
        f"{now:%H:%M:%S} UTC {now.year}"
    )


_K = int(_hex_hash("00" + _N_HEX + "0" + _G_HEX), 16)


class CognitoSrp:
    """Uma tentativa de login SRP (gera um par efêmero novo a cada instância)."""

    def __init__(self, user_pool_id: str) -> None:
        self._pool_name = user_pool_id.split("_", 1)[1]
        self._small_a = int.from_bytes(os.urandom(128), "big") % _N
        self._large_a = pow(_G, self._small_a, _N)

    @property
    def srp_a(self) -> str:
        """Valor público A, enviado no InitiateAuth."""
        return f"{self._large_a:x}"

    def _auth_key(self, user_id: str, password: str, server_b: int, salt_hex: str) -> bytes:
        u = int(_hex_hash(_pad_hex(self._large_a) + _pad_hex(server_b)), 16)
        if u == 0 or server_b % _N == 0:
            raise ValueError("Parâmetros SRP inválidos recebidos do servidor")
        identity = _hash_hex(f"{self._pool_name}{user_id}:{password}".encode())
        x = int(_hex_hash(_pad_hex(salt_hex) + identity), 16)
        s = pow(server_b - _K * pow(_G, x, _N), self._small_a + u * x, _N)
        return _hkdf(bytes.fromhex(_pad_hex(s)), bytes.fromhex(_pad_hex(u)))

    def respond(
        self, challenge: dict[str, Any], password: str, now: datetime | None = None
    ) -> dict[str, str]:
        """Resposta ao desafio PASSWORD_VERIFIER."""
        user_id = challenge["USER_ID_FOR_SRP"]
        secret_block = challenge["SECRET_BLOCK"]
        timestamp = _timestamp(now)
        key = self._auth_key(
            user_id, password, int(challenge["SRP_B"], 16), challenge["SALT"]
        )
        message = (
            self._pool_name.encode()
            + user_id.encode()
            + base64.standard_b64decode(secret_block)
            + timestamp.encode()
        )
        signature = hmac.new(key, message, hashlib.sha256).digest()
        return {
            "USERNAME": user_id,
            "TIMESTAMP": timestamp,
            "PASSWORD_CLAIM_SECRET_BLOCK": secret_block,
            "PASSWORD_CLAIM_SIGNATURE": base64.standard_b64encode(signature).decode(),
        }
