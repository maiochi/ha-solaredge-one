#!/usr/bin/env python3
"""Mapeia endpoints XHR/Fetch do SolarEdge a partir de um arquivo .har.

Uso:
    python -I tools/har_analyzer.py monitoring.har
    python -I tools/har_analyzer.py monitoring.har --dump amostras/

Valores de cookies, Authorization e CSRF são mascarados na saída.
Só usa a biblioteca padrão.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, urlsplit

SENSITIVE_HEADERS = {"cookie", "set-cookie", "authorization", "x-csrf-token", "x-xsrf-token"}
INTERESTING_HEADERS = SENSITIVE_HEADERS | {
    "content-type",
    "accept",
    "x-requested-with",
    "referer",
    "origin",
}
KEYWORDS = {
    "potencia_tempo_real": re.compile(r"currentPower|powerflow|\"power\"", re.I),
    "otimizadores": re.compile(r"POWER_BOX|optimi[sz]er|Optimizer Voltage|panel", re.I),
    "tensao_corrente": re.compile(r"voltage|current \[A\]|\"current\"", re.I),
    "temperatura": re.compile(r"temperature", re.I),
    "inversor_status": re.compile(r"INVERTER|inverterMode|operationMode|\"status\"", re.I),
}


TOKEN_RE = re.compile(
    r"((?:refresh|access|id)_token|password|client_secret|code)([\"']?\s*[=:]\s*[\"']?)([^&\"'\s,}]+)",
    re.I,
)


def mask(value: str) -> str:
    return value[:4] + "…" if len(value) > 8 else "***"


def mask_tokens(text: str) -> str:
    return TOKEN_RE.sub(lambda m: m[1] + m[2] + "***", text)


def template(path: str) -> str:
    """Troca IDs numéricos/serials por {id} para agrupar rotas iguais."""
    return re.sub(r"/(\d{3,}|[0-9A-F]{8}-[0-9A-F]{2})(?=/|$)", "/{id}", path, flags=re.I)


def top_keys(body: str) -> list[str]:
    try:
        data = json.loads(body)
    except ValueError:
        return []
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else {}
    return list(data)[:15] if isinstance(data, dict) else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("har", type=Path)
    parser.add_argument("--host", default="solaredge.com")
    parser.add_argument("--dump", type=Path, help="Salva um corpo de resposta por rota")
    args = parser.parse_args()

    entries = json.loads(args.har.read_text(encoding="utf-8"))["log"]["entries"]
    routes: dict[tuple[str, str], list[dict]] = defaultdict(list)
    cookie_names: set[str] = set()

    for entry in entries:
        req, resp = entry["request"], entry["response"]
        url = urlsplit(req["url"])
        if args.host not in url.netloc:
            continue
        mime = resp.get("content", {}).get("mimeType", "")
        kind = entry.get("_resourceType", "")
        if kind not in ("xhr", "fetch") and "json" not in mime and "javascript" not in mime:
            continue
        cookie_names.update(c["name"] for c in req.get("cookies", []))
        routes[(req["method"], url.netloc + template(url.path))].append(entry)

    if any("oauth2/token" in e["request"]["url"] for e in entries):
        print("ATENÇÃO: este HAR contém tokens OAuth válidos. Não compartilhe o arquivo.\n")
    if not routes:
        print("Nenhuma requisição XHR/Fetch encontrada para", args.host)
        return 1

    print(f"Cookies enviados ao portal: {', '.join(sorted(cookie_names)) or '-'}\n")
    if args.dump:
        args.dump.mkdir(parents=True, exist_ok=True)

    for index, ((method, route), items) in enumerate(sorted(routes.items())):
        sample = items[0]
        req, resp = sample["request"], sample["response"]
        body = resp.get("content", {}).get("text", "") or ""
        tags = [name for name, rx in KEYWORDS.items() if rx.search(body)]
        print(f"[{method}] {route}  x{len(items)}  HTTP {resp['status']}")
        params = dict(parse_qsl(urlsplit(req["url"]).query))
        if params:
            print(f"   query : {params}")
        for header in req.get("headers", []):
            name = header["name"].lower()
            if name in INTERESTING_HEADERS:
                value = mask(header["value"]) if name in SENSITIVE_HEADERS else header["value"]
                print(f"   header: {header['name']}: {value}")
        if req.get("postData", {}).get("text"):
            print(f"   body  : {mask_tokens(req['postData']['text'])[:200]}")
        if keys := top_keys(body):
            print(f"   json  : {keys}")
        if tags:
            print(f"   >>> candidato: {', '.join(tags)}")
        if args.dump and body:
            name = re.sub(r"[^A-Za-z0-9]+", "_", f"{index:02d}_{method}_{route}")[:120]
            (args.dump / f"{name}.json").write_text(body, encoding="utf-8")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
