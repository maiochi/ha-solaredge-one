"""Coordenadores de atualização da integração SolarEdge One."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    DailyEnergy,
    Layout,
    LiveData,
    SolarEdgeAuthError,
    SolarEdgeError,
    SolarEdgeRateLimitError,
    SolarEdgeWebClient,
)
from .const import DOMAIN, MAX_BACKOFF_INTERVAL

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ArrayData:
    """Estado do arranjo fotovoltaico."""

    layout: Layout
    live: dict[str, LiveData] = field(default_factory=dict)
    daily: DailyEnergy = field(default_factory=DailyEnergy)
    monthly: dict[str, float] = field(default_factory=dict)
    lifetime: dict[str, float] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)


def _add(base: dict[str, float], today: dict[str, float]) -> dict[str, float]:
    return {key: base.get(key, 0.0) + today.get(key, 0.0) for key in base.keys() | today.keys()}


class _BackoffCoordinator[_DataT](DataUpdateCoordinator[_DataT]):
    """Dobra o intervalo ao ser limitado e volta ao normal no primeiro sucesso."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SolarEdgeWebClient,
        name: str,
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=name, update_interval=interval
        )
        self.client = client
        self._base_interval = interval

    async def _async_update_data(self) -> _DataT:
        try:
            data = await self._async_fetch()
        except SolarEdgeAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SolarEdgeRateLimitError as err:
            self.update_interval = min(self.update_interval * 2, MAX_BACKOFF_INTERVAL)
            raise UpdateFailed(
                f"{err}; próxima tentativa em {self.update_interval}"
            ) from err
        except SolarEdgeError as err:
            raise UpdateFailed(str(err)) from err
        self.update_interval = self._base_interval
        return data

    async def _async_fetch(self) -> _DataT:
        raise NotImplementedError


class PowerCoordinator(_BackoffCoordinator[dict[str, Any]]):
    """Potência instantânea do site."""

    def __init__(self, hass, entry, client, interval: timedelta) -> None:
        super().__init__(hass, entry, client, f"{DOMAIN}_power", interval)

    async def _async_fetch(self) -> dict[str, Any]:
        return await self.client.async_get_site_power()


class ArrayCoordinator(_BackoffCoordinator[ArrayData]):
    """Layout, telemetria dos otimizadores, energia e status do site.

    São 5 requisições por ciclo, independentemente do número de painéis.
    Os acumulados do mês e da vida útil são lidos "até ontem" uma vez por dia
    e somados à energia de hoje, o que os mantém monotônicos.
    """

    def __init__(self, hass, entry, client, interval: timedelta) -> None:
        super().__init__(hass, entry, client, f"{DOMAIN}_array", interval)
        self._lifetime_start: date | None = None
        self._base_day: date | None = None
        self._monthly_base: dict[str, float] = {}
        self._lifetime_base: dict[str, float] = {}

    async def _async_update_bases(self, inverters: list[str], today: date) -> None:
        yesterday = today - timedelta(days=1)
        if self._lifetime_start is None:
            self._lifetime_start = await self.client.async_get_lifetime_start() or today
        monthly = await self.client.async_get_energy(inverters, today.replace(day=1), yesterday)
        lifetime = await self.client.async_get_energy(inverters, self._lifetime_start, yesterday)
        self._monthly_base, self._lifetime_base = monthly.energy, lifetime.energy
        self._base_day = today

    async def _async_fetch(self) -> ArrayData:
        previous = self.data
        # O layout também carrega o status de cada equipamento, então é relido todo ciclo
        layout = await self.client.async_get_layout()
        live = await self.client.async_get_optimizer_live(list(layout.optimizers))
        if layout.optimizers and not live:
            raise SolarEdgeError("O portal não retornou telemetria dos otimizadores")

        today = dt_util.now().date()
        inverters = list(layout.inverters)
        monthly = previous.monthly if previous else {}
        lifetime = previous.lifetime if previous else {}
        daily_ok = False
        try:
            daily = await self.client.async_get_energy(
                inverters, today, today, include_temperature=True
            )
            daily_ok = True
            if self._base_day != today:
                await self._async_update_bases(inverters, today)
        except (SolarEdgeAuthError, SolarEdgeRateLimitError):
            raise
        except SolarEdgeError as err:
            _LOGGER.debug("Energia indisponível: %s", err)
            if not daily_ok:
                daily = previous.daily if previous else DailyEnergy()
        # Só recalcula com a energia de hoje e a base do mesmo dia; misturar dias
        # faria o total "voltar", e o HA interpretaria como reinício do contador.
        if daily_ok and self._base_day == today:
            monthly = _add(self._monthly_base, daily.energy)
            lifetime = _add(self._lifetime_base, daily.energy)

        try:
            status = await self.client.async_get_site_status()
        except (SolarEdgeAuthError, SolarEdgeRateLimitError):
            raise
        except SolarEdgeError as err:
            _LOGGER.debug("Status do site indisponível: %s", err)
            status = previous.status if previous else {}

        return ArrayData(layout, live, daily, monthly, lifetime, status)
