"""Integração SolarEdge One: dados do dashboard web monitoring.solaredge.com/one."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import logging
from pathlib import Path
from typing import Any

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import SolarEdgeWebClient
from .const import (
    CONF_ARRAY_INTERVAL,
    CONF_COOKIE,
    CONF_PASSWORD,
    CONF_POWER_INTERVAL,
    CONF_REFRESH_TOKEN,
    CONF_SITE_ID,
    CONF_USER_AGENT,
    CONF_USERNAME,
    DEFAULT_ARRAY_INTERVAL,
    DEFAULT_POWER_INTERVAL,
    DOMAIN,
)
from .coordinator import ArrayCoordinator, PowerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CARD_FILE = Path(__file__).parent / "frontend" / "solaredge-energy-card.js"
CARD_URL = f"/{DOMAIN}/solaredge-energy-card.js"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Serve o card do painel e o registra como recurso do Lovelace."""
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_FILE), False)]
    )
    # A data de modificação na URL força o navegador a buscar versões novas
    mtime = await hass.async_add_executor_job(lambda: CARD_FILE.stat().st_mtime)
    url = f"{CARD_URL}?v={int(mtime)}"
    if not await _async_register_lovelace_resource(hass, url):
        # Painéis em modo YAML: carrega junto com o index.html
        add_extra_js_url(hass, url)
        add_extra_js_url(hass, url, es5=True)
    return True


async def _async_register_lovelace_resource(hass: HomeAssistant, url: str) -> bool:
    """Cria/atualiza o recurso do card (como o HACS faz).

    Recursos são lidos do servidor a cada abertura do painel, então não
    dependem do index.html que o app guarda em cache.
    """
    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None)
    if resources is None and isinstance(lovelace, dict):
        resources = lovelace.get("resources")
    if resources is None or not hasattr(resources, "async_create_item"):
        _LOGGER.debug("Recursos do Lovelace em modo YAML; usando extra_js_url")
        return False

    if not getattr(resources, "loaded", True):
        await resources.async_load()
        resources.loaded = True

    existing = [
        item
        for item in resources.async_items()
        if str(item.get("url", "")).split("?")[0] == CARD_URL
    ]
    if not existing:
        await resources.async_create_item({"res_type": "module", "url": url})
    elif existing[0].get("url") != url:
        await resources.async_update_item(
            existing[0]["id"], {"res_type": "module", "url": url}
        )
    # Remove duplicatas (por exemplo, um recurso cadastrado à mão antes)
    for item in existing[1:]:
        await resources.async_delete_item(item["id"])
    return True


@dataclass(slots=True)
class SolarEdgeRuntimeData:
    """Objetos compartilhados pela entrada de configuração."""

    client: SolarEdgeWebClient
    power: PowerCoordinator
    array: ArrayCoordinator
    applied_options: dict[str, Any]


type SolarEdgeConfigEntry = ConfigEntry[SolarEdgeRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Configura a integração a partir de uma entrada."""
    @callback
    def _store_refresh_token(token: str) -> None:
        # Persiste o refresh token novo (após login ou rotação) para não
        # precisar refazer o login a cada reinício
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
        )

    client = SolarEdgeWebClient(
        async_create_clientsession(hass),
        entry.data[CONF_SITE_ID],
        username=entry.data.get(CONF_USERNAME),
        password=entry.data.get(CONF_PASSWORD),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        cookie=entry.data.get(CONF_COOKIE),
        user_agent=entry.data.get(CONF_USER_AGENT),
        on_refresh_token=_store_refresh_token,
    )

    options = entry.options
    power = PowerCoordinator(
        hass,
        entry,
        client,
        timedelta(seconds=options.get(CONF_POWER_INTERVAL, DEFAULT_POWER_INTERVAL)),
    )
    array = ArrayCoordinator(
        hass,
        entry,
        client,
        timedelta(seconds=options.get(CONF_ARRAY_INTERVAL, DEFAULT_ARRAY_INTERVAL)),
    )

    # Erros de autenticação aqui abrem o fluxo de reautenticação automaticamente
    await array.async_config_entry_first_refresh()
    await power.async_config_entry_first_refresh()

    entry.runtime_data = SolarEdgeRuntimeData(client, power, array, dict(entry.options))
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_update))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> bool:
    """Descarrega a entrada."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_on_update(hass: HomeAssistant, entry: SolarEdgeConfigEntry) -> None:
    # Disparado só por mudança de opções; a rotação do token não recarrega
    if entry.options != entry.runtime_data.applied_options:
        await hass.config_entries.async_reload(entry.entry_id)
