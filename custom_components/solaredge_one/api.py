"""Cliente assíncrono para a API interna do dashboard SolarEdge /one.

Este módulo não depende do Home Assistant, para poder ser testado isoladamente.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
import json
import logging
import re
import time
from typing import Any

import aiohttp

from .const import (
    ALERT_CATALOG_FALLBACK,
    ALERT_CATALOG_URL,
    ALERTS_PATH,
    BASE_URL,
    CLIENT_ID,
    COGNITO_URL,
    COMMUNICATION_PATH,
    DEFAULT_USER_AGENT,
    ENERGY_BY_INVERTER_PATH,
    LAYOUT_PATH,
    LIVE_POWER_PATH,
    OPTIMIZER_LIVE_PATH,
    POWER_FLOW_PATH,
    REQUEST_TIMEOUT,
    SITE_INFO_PATH,
    TOKEN_REFRESH_MARGIN,
    TOKEN_URL,
    USER_POOL_ID,
)
from .cognito import CognitoSrp

_LOGGER = logging.getLogger(__name__)

KIND_INVERTER = "inverter"
KIND_OPTIMIZER = "optimizer"

# "optimizerVoltage_V" -> ("optimizerVoltage", "V")
_LIVE_FIELD_RE = re.compile(r"^(?P<name>.+?)_(?P<unit>A|V|W|WH|KWH|C|F)$", re.I)
# Campos numéricos que não viram sensor (energia só do último intervalo)
_LIVE_ATTRIBUTE_FIELDS = {"total_last_telemetry_energy"}


class SolarEdgeError(Exception):
    """Erro genérico do portal."""


class SolarEdgeAuthError(SolarEdgeError):
    """Usuário/senha incorretos, refresh token expirado ou token recusado."""


class SolarEdgeMfaRequiredError(SolarEdgeAuthError):
    """A conta exige um segundo fator (MFA), que a integração não suporta."""


class SolarEdgeRateLimitError(SolarEdgeError):
    """HTTP 429 ou desafio da Cloudflare."""


class SolarEdgeConnectionError(SolarEdgeError):
    """Falha de rede ou timeout."""


@dataclass(slots=True)
class Component:
    """Inversor ou otimizador da árvore lógica."""

    serial: str
    name: str
    kind: str
    model: str | None = None
    status: str | None = None
    parent_serial: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Layout:
    """Equipamentos do site."""

    site_name: str | None = None
    inverters: dict[str, Component] = field(default_factory=dict)
    optimizers: dict[str, Component] = field(default_factory=dict)


@dataclass(slots=True)
class LiveData:
    """Última telemetria de um otimizador."""

    values: dict[str, float] = field(default_factory=dict)
    units: dict[str, str] = field(default_factory=dict)
    attributes: dict[str, Any] = field(default_factory=dict)
    last_measurement: datetime | None = None


@dataclass(slots=True)
class DailyEnergy:
    """Energia do dia (Wh) e temperatura máxima por equipamento."""

    energy: dict[str, float] = field(default_factory=dict)
    temperature: dict[str, tuple[float, str | None]] = field(default_factory=dict)


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _energy_wh(block: Any) -> float | None:
    if not isinstance(block, dict):
        return None
    value = _to_float(block.get("value"))
    if value is None:
        return None
    unit = str(block.get("unit", "watt-hour")).lower()
    return value * {"kilowatt-hour": 1000.0, "megawatt-hour": 1_000_000.0}.get(unit, 1.0)


def _is_cloudflare(status: int, text: str) -> bool:
    return status in (403, 503) and ("cf-" in text or "challenge" in text.lower())


def parse_layout(payload: dict[str, Any]) -> Layout:
    """Extrai inversores e otimizadores de layout/logical/generic/v2."""
    root = payload.get("siteStructure", payload)
    layout = Layout(site_name=root.get("name"))

    def walk(node: dict[str, Any], inverter_serial: str | None) -> None:
        node_type = str(node.get("type", "")).upper()
        serial = node.get("serial")
        props = node.get("properties") or {}
        if serial and node_type == "INVERTER":
            inverter_serial = serial
            layout.inverters[serial] = Component(
                serial, node.get("name") or serial, KIND_INVERTER,
                props.get("model"), props.get("status"), None, props,
            )
        elif serial and node_type == "OPTIMIZER":
            layout.optimizers[serial] = Component(
                serial, node.get("name") or serial, KIND_OPTIMIZER,
                props.get("model"), props.get("status"), inverter_serial, props,
            )
        for child in node.get("children") or []:
            walk(child, inverter_serial)

    walk(root, None)
    return layout


def parse_optimizer_live(payload: dict[str, Any]) -> dict[str, LiveData]:
    """Normaliza serialToLiveData de layout/information/optimizers."""
    result: dict[str, LiveData] = {}
    for serial, raw in (payload.get("serialToLiveData") or {}).items():
        if not isinstance(raw, dict):
            continue
        live = LiveData(last_measurement=_parse_datetime(raw.get("lastMeasurement")))
        for field_name, raw_value in raw.items():
            value = _to_float(raw_value)
            match = _LIVE_FIELD_RE.match(field_name)
            if value is None or not match:
                continue
            key = _snake(match["name"])
            if key in _LIVE_ATTRIBUTE_FIELDS:
                live.attributes[key] = value
                continue
            live.values[key] = value
            live.units[key] = match["unit"]
        result[serial] = live
    return result


def parse_energy_by_inverter(payload: dict[str, Any]) -> DailyEnergy:
    """Energia do dia por inversor/otimizador (e temperatura, se houver)."""
    daily = DailyEnergy()
    for inverter in payload.get("inverters") or []:
        devices = [inverter, *(inverter.get("optimizers") or [])]
        for device in devices:
            serial = device.get("serial")
            if not serial:
                continue
            if (energy := _energy_wh(device.get("energy"))) is not None:
                daily.energy[serial] = energy
            temp = device.get("temperature") or {}
            if (value := _to_float(temp.get("temperature"))) is not None:
                daily.temperature[serial] = (value, temp.get("temperatureUnit"))
    return daily


# O formato de cada item de topAlerts não foi visto numa captura real (o site
# não tinha alertas), então o parser procura os campos por vários nomes.
_ALERT_CODE_KEYS = ("alertType", "alertCode", "code", "type", "alertName", "name")
_ALERT_TITLE_KEYS = ("title", "alertTypeName", "typeName", "displayName", "name")
_ALERT_TEXT_KEYS = ("description", "message", "details")
_ALERT_SEVERITY_KEYS = ("impact", "severity", "alertImpact", "level", "priority")
_ALERT_COMPONENT_KEYS = (
    "componentName", "deviceName", "equipmentName", "component", "inverterName",
)
_ALERT_SERIAL_KEYS = (
    "serial", "serialNumber", "componentSerial", "deviceSerial", "componentId",
)
_ALERT_DATE_KEYS = (
    "openDate", "startDate", "creationDate", "firstOccurrence", "openTime",
    "startTime", "date", "time", "timestamp",
)


def _first(raw: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def _alert_date(raw: dict[str, Any]) -> str | None:
    candidates = [raw.get(key) for key in _ALERT_DATE_KEYS]
    candidates += [v for k, v in raw.items() if re.search("date|time", k, re.I)]
    for value in candidates:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            seconds = value / 1000 if value > 1e11 else value  # epoch em ms ou s
            return datetime.fromtimestamp(seconds).astimezone().isoformat()
        if (parsed := _parse_datetime(value)) is not None:
            return parsed.isoformat()
    return None


def _alert_code(raw: dict[str, Any], catalog: dict[str, Any]) -> str | None:
    """Código do alerta: um campo conhecido ou qualquer valor que esteja no catálogo."""
    values = [raw.get(key) for key in _ALERT_CODE_KEYS] + list(raw.values())
    for value in values:
        if isinstance(value, dict):
            values.extend(value.values())
        elif isinstance(value, str) and value.strip().upper() in catalog:
            return value.strip().upper()
    code = _first(raw, _ALERT_CODE_KEYS)
    return str(code) if isinstance(code, (str, int)) else None


def parse_alerts(items: Any, catalog: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Normaliza topAlerts e completa com o catálogo de textos do portal."""
    catalog = catalog or {}
    result: list[dict[str, Any]] = []
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            continue
        code = _alert_code(raw, catalog)
        info = catalog.get(code or "")
        info = info if isinstance(info, dict) else {}
        serial = _first(raw, _ALERT_SERIAL_KEYS)
        result.append(
            {
                "code": code,
                "title": info.get("type") or _first(raw, _ALERT_TITLE_KEYS) or code,
                "description": info.get("description") or _first(raw, _ALERT_TEXT_KEYS),
                "resolution": info.get("resolution"),
                "severity": _first(raw, _ALERT_SEVERITY_KEYS),
                "component": _first(raw, _ALERT_COMPONENT_KEYS),
                "serial": str(serial) if serial is not None else None,
                "start": _alert_date(raw),
            }
        )
    return result


def catalog_languages(language: str | None) -> list[str]:
    """Idiomas a tentar para o catálogo: 'pt-BR' -> ['pt_BR', 'en_US']."""
    defaults = {
        "pt": "pt_BR", "en": "en_US", "es": "es_ES", "de": "de_DE",
        "fr": "fr_FR", "it": "it_IT", "nl": "nl_NL",
    }
    parts = (language or "").replace("-", "_").split("_")
    options = []
    if len(parts) == 2 and parts[1]:
        options.append(f"{parts[0].lower()}_{parts[1].upper()}")
    if parts[0].lower() in defaults:
        options.append(defaults[parts[0].lower()])
    options.append(ALERT_CATALOG_FALLBACK)
    return list(dict.fromkeys(options))


def parse_site_power(flow: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """Junta power-flow/v2 e live-power em um dicionário plano."""
    solar = flow.get("solarProduction") or {}
    result: dict[str, Any] = {
        "is_communicating": flow.get("isCommunicating"),
        "is_producing": solar.get("isProducing"),
        "last_update": _parse_datetime(flow.get("lastUpdateTime")),
        "max_ac_power": _to_float(live.get("maxAcPower")),
    }
    power = _to_float(live.get("currentAcPower"))
    if power is None and solar.get("isProducing") is False:
        # À noite o portal devolve null; para o HA isso é 0 W
        power = 0.0
    result["pv_power"] = power
    return result


class SolarEdgeWebClient:
    """Acesso ao portal com os tokens OAuth (Cognito) do dashboard /one.

    Com usuário e senha, faz login SRP no Cognito e renova os tokens com o
    refresh token obtido; se ele expirar, faz login de novo sozinho.
    Sem senha (entradas antigas), usa só o refresh token copiado do navegador.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        site_id: str,
        *,
        username: str | None = None,
        password: str | None = None,
        refresh_token: str | None = None,
        cookie: str | None = None,
        user_agent: str | None = None,
        on_refresh_token: Callable[[str], None] | None = None,
    ) -> None:
        if not refresh_token and not (username and password):
            raise ValueError("Informe usuário e senha ou um refresh token")
        self._session = session
        self.site_id = str(site_id)
        self._username = username.strip() if username else None
        self._password = password or None
        self._refresh_token = "".join(refresh_token.split()) if refresh_token else None
        self._cookie = " ".join(cookie.split()) if cookie else None
        self._user_agent = user_agent or DEFAULT_USER_AGENT
        self._on_refresh_token = on_refresh_token
        self._tokens: dict[str, str] = {}
        self._token_kind = "access_token"
        self._expires_at = 0.0
        self._token_lock = asyncio.Lock()

    @property
    def refresh_token(self) -> str | None:
        """Refresh token atual (para persistir e evitar logins repetidos)."""
        return self._refresh_token

    async def async_refresh_access_token(self) -> None:
        """Obtém access/id tokens novos, pelo refresh token ou por login."""
        if self._refresh_token:
            try:
                if self._password:
                    await self._async_cognito_refresh()
                else:
                    await self._async_oauth_refresh()
                return
            except SolarEdgeAuthError:
                if not self._password:
                    raise
                _LOGGER.debug("Refresh token recusado; fazendo login de novo")
                self._refresh_token = None
        await self.async_login()

    async def async_login(self) -> None:
        """Login SRP com usuário e senha (o mesmo fluxo do site)."""
        if not (self._username and self._password):
            raise SolarEdgeAuthError("Sem usuário e senha para fazer login")
        srp = CognitoSrp(USER_POOL_ID)
        response = await self._async_cognito(
            "InitiateAuth",
            {
                "AuthFlow": "USER_SRP_AUTH",
                "ClientId": CLIENT_ID,
                "AuthParameters": {"USERNAME": self._username, "SRP_A": srp.srp_a},
            },
        )
        if response.get("ChallengeName") != "PASSWORD_VERIFIER":
            raise SolarEdgeError(
                f"Login: desafio inesperado {response.get('ChallengeName')!r}"
            )
        try:
            answer = srp.respond(response["ChallengeParameters"], self._password)
        except (KeyError, ValueError) as err:
            raise SolarEdgeError(f"Login: desafio SRP inválido ({err})") from err
        response = await self._async_cognito(
            "RespondToAuthChallenge",
            {
                "ChallengeName": "PASSWORD_VERIFIER",
                "ClientId": CLIENT_ID,
                "ChallengeResponses": answer,
                **({"Session": response["Session"]} if response.get("Session") else {}),
            },
        )
        if "AuthenticationResult" not in response:
            challenge = str(response.get("ChallengeName"))
            if "MFA" in challenge or challenge == "SELECT_MFA_TYPE":
                raise SolarEdgeMfaRequiredError(f"A conta exige MFA ({challenge})")
            if challenge == "NEW_PASSWORD_REQUIRED":
                raise SolarEdgeAuthError(
                    "O portal exige trocar a senha: faça login no navegador primeiro"
                )
            raise SolarEdgeError(f"Login: desafio não suportado {challenge!r}")
        self._store_tokens(response["AuthenticationResult"])

    async def _async_cognito_refresh(self) -> None:
        response = await self._async_cognito(
            "InitiateAuth",
            {
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "ClientId": CLIENT_ID,
                "AuthParameters": {"REFRESH_TOKEN": self._refresh_token},
            },
        )
        if "AuthenticationResult" not in response:
            raise SolarEdgeAuthError("Renovação do token não devolveu tokens")
        self._store_tokens(response["AuthenticationResult"])

    def _store_tokens(self, result: dict[str, Any]) -> None:
        """Guarda o AuthenticationResult da API do Cognito."""
        self._apply_tokens(
            {
                "access_token": result.get("AccessToken"),
                "id_token": result.get("IdToken"),
                "refresh_token": result.get("RefreshToken"),
                "expires_in": result.get("ExpiresIn"),
            }
        )

    async def _async_cognito(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        """Chamada à API JSON do Cognito (cognito-idp)."""
        try:
            async with self._session.post(
                COGNITO_URL,
                data=json.dumps(body),
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": f"AWSCognitoIdentityProviderService.{action}",
                    "User-Agent": self._user_agent,
                },
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                status = resp.status
                text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SolarEdgeConnectionError(f"Cognito {action}: {err}") from err
        try:
            payload = json.loads(text) if text else {}
        except ValueError as err:
            raise SolarEdgeError(f"Cognito {action}: resposta não é JSON") from err
        if status == 200:
            return payload

        error = str(payload.get("__type", "")).rsplit("#", 1)[-1]
        message = payload.get("message") or payload.get("Message") or f"HTTP {status}"
        if error in (
            "NotAuthorizedException",
            "UserNotFoundException",
            "UserNotConfirmedException",
            "PasswordResetRequiredException",
        ):
            raise SolarEdgeAuthError(f"Cognito {action}: {message}")
        if error in ("TooManyRequestsException", "LimitExceededException") or status == 429:
            raise SolarEdgeRateLimitError(f"Cognito {action}: {message}")
        raise SolarEdgeError(f"Cognito {action}: {error or status} {message}")

    async def _async_oauth_refresh(self) -> None:
        """Renova pelo endpoint OAuth do site (refresh token do navegador)."""
        try:
            async with self._session.post(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": self._refresh_token,
                },
                headers={
                    "Accept": "application/json",
                    "Origin": BASE_URL,
                    "Referer": BASE_URL + "/",
                    "User-Agent": self._user_agent,
                },
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                status = resp.status
                text = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SolarEdgeConnectionError(f"Renovação do token: {err}") from err

        if status in (400, 401):
            raise SolarEdgeAuthError("Refresh token inválido ou expirado")
        if status == 429:
            raise SolarEdgeRateLimitError("Renovação do token: HTTP 429")
        if status >= 300:
            raise SolarEdgeError(f"Renovação do token: HTTP {status}")
        try:
            payload = json.loads(text)
        except ValueError as err:
            raise SolarEdgeError("Renovação do token: resposta não é JSON") from err
        self._apply_tokens(payload)

    def _apply_tokens(self, payload: dict[str, Any]) -> None:
        tokens = {
            kind: payload[kind]
            for kind in ("access_token", "id_token")
            if payload.get(kind)
        }
        if not tokens:
            raise SolarEdgeAuthError("O servidor de login não devolveu tokens")
        self._tokens = tokens
        lifetime = float(payload.get("expires_in") or 3600)
        self._expires_at = time.monotonic() + lifetime - TOKEN_REFRESH_MARGIN
        # O login sempre traz um refresh token; a renovação, só se houver rotação
        if (rotated := payload.get("refresh_token")) and rotated != self._refresh_token:
            self._refresh_token = rotated
            if self._on_refresh_token:
                self._on_refresh_token(rotated)

    async def _async_bearer(self, force: bool = False) -> str:
        async with self._token_lock:
            if force or not self._tokens or time.monotonic() >= self._expires_at:
                await self.async_refresh_access_token()
            return self._tokens.get(self._token_kind) or next(iter(self._tokens.values()))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        status, text, location, content_type = await self._send(
            method, path, params, json_body, await self._async_bearer()
        )
        if status in (401, 403) and not _is_cloudflare(status, text):
            # O HAR não mostra qual token o backend espera: tenta o outro
            # e, por fim, força uma renovação.
            other = "id_token" if self._token_kind == "access_token" else "access_token"
            if other in self._tokens:
                retry = await self._send(method, path, params, json_body, self._tokens[other])
                if retry[0] not in (401, 403):
                    _LOGGER.debug("Backend aceita %s; passando a usá-lo", other)
                    self._token_kind = other
                    status, text, location, content_type = retry
            if status in (401, 403):
                status, text, location, content_type = await self._send(
                    method, path, params, json_body, await self._async_bearer(force=True)
                )

        if status == 429:
            raise SolarEdgeRateLimitError(f"{method} {path}: HTTP 429")
        if _is_cloudflare(status, text):
            raise SolarEdgeRateLimitError(
                f"{method} {path}: bloqueado pela Cloudflare (HTTP {status})"
            )
        if (
            status in (401, 403)
            or (300 <= status < 400 and "login" in location.lower())
            or "text/html" in content_type
        ):
            raise SolarEdgeAuthError(f"{method} {path}: token recusado (HTTP {status})")
        if status >= 300:
            raise SolarEdgeError(f"{method} {path}: HTTP {status}")
        if status == 204 or not text:
            return {}
        try:
            return json.loads(text)
        except ValueError as err:
            raise SolarEdgeError(f"{method} {path}: resposta não é JSON") from err

    async def _send(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        json_body: Any,
        token: str,
    ) -> tuple[int, str, str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": f"Bearer {token}",
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/one",
            "User-Agent": self._user_agent,
        }
        if self._cookie:
            headers["Cookie"] = self._cookie
        try:
            async with self._session.request(
                method,
                BASE_URL + path.format(site_id=self.site_id),
                params=params,
                json=json_body,
                headers=headers,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
            ) as resp:
                return (
                    resp.status,
                    await resp.text(),
                    resp.headers.get("Location", ""),
                    resp.headers.get("Content-Type", ""),
                )
        except (TimeoutError, aiohttp.ClientError) as err:
            raise SolarEdgeConnectionError(f"{method} {path}: {err}") from err

    async def async_get_layout(self) -> Layout:
        payload = await self._request(
            "GET", LAYOUT_PATH, params={"include-optimizers": "true"}
        )
        return parse_layout(payload)

    async def async_get_optimizer_live(self, serials: list[str]) -> dict[str, LiveData]:
        """Telemetria de todos os otimizadores em uma única chamada."""
        if not serials:
            return {}
        payload = await self._request("POST", OPTIMIZER_LIVE_PATH, json_body=serials)
        return parse_optimizer_live(payload)

    async def async_get_energy(
        self,
        inverter_serials: list[str],
        start: date,
        end: date,
        *,
        include_temperature: bool = False,
    ) -> DailyEnergy:
        """Energia (Wh) por inversor/otimizador no intervalo [start, end]."""
        if not inverter_serials or start > end:
            return DailyEnergy()
        payload = await self._request(
            "GET",
            ENERGY_BY_INVERTER_PATH,
            params={
                "start-date": start.isoformat(),
                "end-date": end.isoformat(),
                "inverter-serials": ",".join(inverter_serials),
                "include-max-temperature": str(include_temperature).lower(),
                "include-color": "false",
            },
        )
        return parse_energy_by_inverter(payload)

    async def async_get_lifetime_start(self) -> date | None:
        """Data a partir da qual o portal tem produção (lifeTimeDate)."""
        payload = await self._request("GET", SITE_INFO_PATH)
        for key in ("lifeTimeDate", "installationDate"):
            try:
                return date.fromisoformat(str(payload.get(key))[:10])
            except ValueError:
                continue
        return None

    async def async_get_site_power(self) -> dict[str, Any]:
        flow = await self._request("GET", POWER_FLOW_PATH)
        live = await self._request("GET", LIVE_POWER_PATH)
        return parse_site_power(flow, live)

    async def async_get_alert_catalog(self, language: str | None) -> dict[str, Any]:
        """Textos dos alertas (CDN pública); tenta o idioma e cai para inglês."""
        for option in catalog_languages(language):
            try:
                async with self._session.get(
                    ALERT_CATALOG_URL.format(language=option),
                    headers={"Accept": "application/json", "User-Agent": self._user_agent},
                    timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
                ) as resp:
                    if resp.status != 200:
                        continue
                    payload = json.loads(await resp.text())
            except (TimeoutError, aiohttp.ClientError, ValueError) as err:
                raise SolarEdgeConnectionError(f"Catálogo de alertas: {err}") from err
            if isinstance(payload, dict) and payload:
                return payload
        raise SolarEdgeError("Catálogo de alertas indisponível")

    async def async_get_site_status(self) -> dict[str, Any]:
        communication = await self._request("GET", COMMUNICATION_PATH)
        alerts = await self._request("GET", ALERTS_PATH)
        return {
            "connectivity": communication.get("connectivityStatus"),
            "active": communication.get("active"),
            "alerts": alerts.get("totalAlertsCount"),
            "top_alerts": alerts.get("topAlerts") or [],
        }

