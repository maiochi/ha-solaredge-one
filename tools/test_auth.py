#!/usr/bin/env python3
"""Testa a autenticação OAuth contra a API /services do SolarEdge.

Uso:
    python -I tools/test_auth.py 1234567 --user voce@email.com   (pede a senha)
    python -I tools/test_auth.py 1234567 --har tools/download
    SOLAREDGE_REFRESH_TOKEN=... python -I tools/test_auth.py 1234567

Só faz leituras. Nenhum token é impresso.
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode
from urllib.request import Request, urlopen

TOKEN_URL = "https://login.solaredge.com/oauth2/token"
CLIENT_ID = "ugfnsujd3384sshcjehaphlh3"
BASE = "https://monitoring.solaredge.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
)


def login(username: str, password: str) -> dict:
    """Login SRP com o mesmo módulo usado pela integração."""
    path = Path(__file__).resolve().parent.parent / "custom_components/solaredge_one/cognito.py"
    spec = importlib.util.spec_from_file_location("cognito", path)
    cognito = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cognito)

    def idp(action: str, body: dict) -> dict:
        status, text = call(
            "POST",
            "https://cognito-idp.eu-central-1.amazonaws.com/",
            {
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": f"AWSCognitoIdentityProviderService.{action}",
            },
            json.dumps(body).encode(),
        )
        payload = json.loads(text or "{}")
        print(f"cognito {action} -> HTTP {status} {payload.get('ChallengeName') or ''}")
        if status != 200:
            sys.exit(f"   {payload.get('__type')}: {payload.get('message')}")
        return payload

    srp = cognito.CognitoSrp("eu-central-1_fVUTz39em")
    challenge = idp(
        "InitiateAuth",
        {
            "AuthFlow": "USER_SRP_AUTH",
            "ClientId": CLIENT_ID,
            "AuthParameters": {"USERNAME": username, "SRP_A": srp.srp_a},
        },
    )
    result = idp(
        "RespondToAuthChallenge",
        {
            "ChallengeName": "PASSWORD_VERIFIER",
            "ClientId": CLIENT_ID,
            "ChallengeResponses": srp.respond(challenge["ChallengeParameters"], password),
        },
    )
    auth = result.get("AuthenticationResult") or sys.exit("   sem tokens (MFA?)")
    print(f"   login OK; expires_in={auth.get('ExpiresIn')} refresh_token={'RefreshToken' in auth}")
    return {"access_token": auth["AccessToken"], "id_token": auth["IdToken"]}


def refresh_token_from_har(path: Path) -> str:
    for entry in json.loads(path.read_text(encoding="utf-8"))["log"]["entries"]:
        if "oauth2/token" in entry["request"]["url"]:
            body = entry["request"].get("postData", {}).get("text", "")
            if token := parse_qs(body).get("refresh_token"):
                return token[0]
    sys.exit("Nenhum refresh_token encontrado no HAR")


def call(method: str, url: str, headers: dict, data: bytes | None = None) -> tuple[int, str]:
    req = Request(url, data=data, method=method, headers={"User-Agent": UA, **headers})
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def refresh_tokens(args: argparse.Namespace) -> dict:
    refresh = (
        refresh_token_from_har(args.har)
        if args.har
        else os.environ.get("SOLAREDGE_REFRESH_TOKEN") or sys.exit("Informe --user, --har ou a variável")
    )
    status, text = call(
        "POST",
        TOKEN_URL,
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": BASE,
            "Referer": BASE + "/",
        },
        urlencode(
            {"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh}
        ).encode(),
    )
    print(f"oauth2/token -> HTTP {status}")
    if status != 200:
        sys.exit("   " + text[:200])
    tokens = json.loads(text)
    print(f"   expires_in={tokens.get('expires_in')}  rotacionou refresh_token={'refresh_token' in tokens}")
    return tokens


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("site_id")
    parser.add_argument("--har", type=Path)
    parser.add_argument("--user", help="e-mail do portal (a senha é pedida no terminal)")
    args = parser.parse_args()

    if args.user:
        tokens = login(args.user, os.environ.get("SOLAREDGE_PASSWORD") or getpass.getpass("Senha: "))
    else:
        tokens = refresh_tokens(args)

    layout_url = f"{BASE}/services/layout/logical/generic/v2/site/{args.site_id}?include-optimizers=true"
    working = None
    for kind in ("access_token", "id_token"):
        status, text = call(
            "GET",
            layout_url,
            {
                "Authorization": f"Bearer {tokens[kind]}",
                "Accept": "application/json, text/plain, */*",
                "Referer": BASE + "/one",
            },
        )
        cloudflare = "cf-" in text or "challenge" in text.lower()
        print(f"layout com {kind} -> HTTP {status}{'  (Cloudflare)' if cloudflare and status >= 400 else ''}")
        if status == 200:
            working = kind
            break

    status, _ = call("GET", layout_url, {"Accept": "application/json", "Referer": BASE + "/one"})
    print(f"layout sem Authorization -> HTTP {status}  (esperado 401/403)")

    if not working:
        return 1
    structure = json.loads(text)["siteStructure"]
    serials: list[str] = []

    def walk(node: dict) -> None:
        if node.get("type") == "OPTIMIZER":
            serials.append(node["serial"])
        for child in node.get("children") or []:
            walk(child)

    walk(structure)
    print(f"OK: {working} funciona; {len(serials)} otimizadores no layout")

    status, text = call(
        "POST",
        f"{BASE}/services/layout/information/optimizers",
        {
            "Authorization": f"Bearer {tokens[working]}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": BASE,
            "Referer": BASE + "/one",
        },
        json.dumps(serials).encode(),
    )
    live = json.loads(text).get("serialToLiveData", {}) if status == 200 else {}
    print(f"information/optimizers com {len(serials)} seriais -> HTTP {status}, {len(live)} com telemetria")
    for serial, data in list(live.items())[:3]:
        print(f"   {serial}: {data.get('power_W')} W, {data.get('voltage_V')} V, {data.get('current_A')} A")
    return 0


if __name__ == "__main__":
    sys.exit(main())
