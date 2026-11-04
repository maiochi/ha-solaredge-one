"""Sensores da integração SolarEdge One."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import SolarEdgeConfigEntry
from .api import Component, LiveData
from .const import DASHBOARD_URL, DOMAIN, STALE_AFTER
from .coordinator import ArrayCoordinator, ArrayData, PowerCoordinator

PARALLEL_UPDATES = 0

UNIT_MAP: dict[str, str] = {
    "a": UnitOfElectricCurrent.AMPERE,
    "v": UnitOfElectricPotential.VOLT,
    "w": UnitOfPower.WATT,
    "wh": UnitOfEnergy.WATT_HOUR,
    "kwh": UnitOfEnergy.KILO_WATT_HOUR,
    "c": UnitOfTemperature.CELSIUS,
    "celsius": UnitOfTemperature.CELSIUS,
    "f": UnitOfTemperature.FAHRENHEIT,
    "fahrenheit": UnitOfTemperature.FAHRENHEIT,
}

DEVICE_CLASS_BY_UNIT: dict[str, SensorDeviceClass] = {
    UnitOfElectricCurrent.AMPERE: SensorDeviceClass.CURRENT,
    UnitOfElectricPotential.VOLT: SensorDeviceClass.VOLTAGE,
    UnitOfPower.WATT: SensorDeviceClass.POWER,
    UnitOfEnergy.WATT_HOUR: SensorDeviceClass.ENERGY,
    UnitOfEnergy.KILO_WATT_HOUR: SensorDeviceClass.ENERGY,
    UnitOfTemperature.CELSIUS: SensorDeviceClass.TEMPERATURE,
    UnitOfTemperature.FAHRENHEIT: SensorDeviceClass.TEMPERATURE,
}

# Medições com tradução própria (ver strings.json)
TRANSLATED_MEASUREMENTS = {"power", "current", "voltage", "optimizer_voltage"}
# Zeradas quando a última leitura do otimizador está velha (noite)
ZERO_WHEN_STALE = {"power", "current"}


def _is_stale(live: LiveData) -> bool:
    return (
        live.last_measurement is not None
        and dt_util.utcnow() - live.last_measurement > STALE_AFTER
    )


def _live_value(live: LiveData, key: str) -> float | None:
    if key in ZERO_WHEN_STALE and key in live.values and _is_stale(live):
        return 0.0
    return live.values.get(key)


@dataclass(frozen=True, kw_only=True)
class SitePowerDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    attrs_fn: Callable[[dict[str, Any]], dict[str, Any] | None] = lambda _: None


@dataclass(frozen=True, kw_only=True)
class SiteArrayDescription(SensorEntityDescription):
    value_fn: Callable[[ArrayData], Any]
    attrs_fn: Callable[[ArrayData], dict[str, Any] | None] = lambda _: None


def _array_power(data: ArrayData) -> float | None:
    values = [
        value
        for live in data.live.values()
        if (value := _live_value(live, "power")) is not None
    ]
    return round(sum(values), 1) if values else None


# Período -> função que extrai o dicionário serial->Wh dos dados
ENERGY_PERIODS: dict[str, Callable[[ArrayData], dict[str, float]]] = {
    "daily_energy": lambda d: d.daily.energy,
    "monthly_energy": lambda d: d.monthly,
    "lifetime_energy": lambda d: d.lifetime,
}


def _site_energy(period: str) -> Callable[[ArrayData], float | None]:
    def value(data: ArrayData) -> float | None:
        energy = ENERGY_PERIODS[period](data)
        values = [energy[s] for s in data.layout.inverters if s in energy]
        return sum(values) if values else None

    return value


SITE_POWER_SENSORS: tuple[SitePowerDescription, ...] = (
    SitePowerDescription(
        key="pv_power",
        translation_key="pv_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda d: d.get("pv_power"),
        attrs_fn=lambda d: {"max_ac_power": d.get("max_ac_power")},
    ),
    SitePowerDescription(
        key="production_state",
        translation_key="production_state",
        device_class=SensorDeviceClass.ENUM,
        options=["producing", "idle", "not_communicating"],
        value_fn=lambda d: (
            "not_communicating"
            if d.get("is_communicating") is False and not d.get("is_producing")
            else "producing"
            if d.get("is_producing")
            else "idle"
        ),
    ),
    SitePowerDescription(
        key="last_update",
        translation_key="last_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.get("last_update"),
    ),
)

SITE_ARRAY_SENSORS: tuple[SiteArrayDescription, ...] = (
    SiteArrayDescription(
        key="array_power",
        translation_key="array_power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=_array_power,
    ),
    *(
        SiteArrayDescription(
            key=period,
            translation_key=period,
            device_class=SensorDeviceClass.ENERGY,
            native_unit_of_measurement=UnitOfEnergy.WATT_HOUR,
            suggested_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            suggested_display_precision=1,
            state_class=SensorStateClass.TOTAL_INCREASING,
            value_fn=_site_energy(period),
        )
        for period in ENERGY_PERIODS
    ),
    SiteArrayDescription(
        key="connectivity",
        translation_key="connectivity",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.status.get("connectivity"),
        attrs_fn=lambda d: {"active": d.status.get("active")},
    ),
    SiteArrayDescription(
        key="alerts",
        translation_key="alerts",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.status.get("alerts"),
        attrs_fn=lambda d: {"top_alerts": d.status.get("top_alerts")},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SolarEdgeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Cria as entidades e acompanha equipamentos/medições que surgirem depois."""
    runtime = entry.runtime_data
    site_id = runtime.client.site_id
    device_registry = dr.async_get(hass)
    known: set[str] = set()

    def add_new(entities: list[SolarEdgeBaseSensor]) -> None:
        fresh = [e for e in entities if e.unique_id not in known]
        known.update(e.unique_id for e in fresh)
        if fresh:
            async_add_entities(fresh)

    @callback
    def discover() -> None:
        data = runtime.array.data
        if data is None:
            return
        site_device = device_registry.async_get_or_create(
            config_entry_id=entry.entry_id, **_site_device(site_id, data.layout.site_name)
        )
        entities: list[SolarEdgeBaseSensor] = [
            SitePowerSensor(runtime.power, site_id, d) for d in SITE_POWER_SENSORS
        ]
        entities.extend(SiteArraySensor(runtime.array, site_id, d) for d in SITE_ARRAY_SENSORS)

        inverter_device_ids: dict[str, str] = {}
        for inverter in data.layout.inverters.values():
            inverter_device_ids[inverter.serial] = device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                via_device_id=site_device.id,
                **_component_device(site_id, inverter),
            ).id
            entities.append(ComponentStatusSensor(runtime.array, site_id, inverter))
            entities.extend(
                ComponentEnergySensor(runtime.array, site_id, inverter, period)
                for period in ENERGY_PERIODS
            )

        for optimizer in data.layout.optimizers.values():
            device_registry.async_get_or_create(
                config_entry_id=entry.entry_id,
                via_device_id=inverter_device_ids.get(optimizer.parent_serial or "", site_device.id),
                **_component_device(site_id, optimizer),
            )
            entities.append(ComponentStatusSensor(runtime.array, site_id, optimizer))
            entities.extend(
                ComponentEnergySensor(runtime.array, site_id, optimizer, period)
                for period in ENERGY_PERIODS
            )
            if optimizer.serial in data.daily.temperature:
                entities.append(OptimizerTemperatureSensor(runtime.array, site_id, optimizer))
            if (live := data.live.get(optimizer.serial)) is not None:
                entities.extend(
                    OptimizerMeasurementSensor(runtime.array, site_id, optimizer, key)
                    for key in live.values
                )
                entities.append(LastMeasurementSensor(runtime.array, site_id, optimizer))
        add_new(entities)

    discover()
    entry.async_on_unload(runtime.array.async_add_listener(discover))


def _site_device(site_id: str, site_name: str | None) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, site_id)},
        name=site_name or f"SolarEdge {site_id}",
        manufacturer="SolarEdge",
        model="Site",
        configuration_url=DASHBOARD_URL.format(site_id=site_id),
    )


def _component_identifier(site_id: str, serial: str) -> tuple[str, str]:
    return (DOMAIN, f"{site_id}_{serial}")


def _component_device(site_id: str, component: Component) -> DeviceInfo:
    # Sem via_device: a hierarquia é registrada com via_device_id em discover()
    return DeviceInfo(
        identifiers={_component_identifier(site_id, component.serial)},
        name=component.name,
        manufacturer="SolarEdge",
        model=component.model,
        serial_number=component.serial,
    )


class SolarEdgeBaseSensor(CoordinatorEntity, SensorEntity):
    """Base comum."""

    _attr_has_entity_name = True


class SitePowerSensor(SolarEdgeBaseSensor):
    """Sensores do site alimentados pelo coordenador rápido."""

    coordinator: PowerCoordinator
    entity_description: SitePowerDescription

    def __init__(self, coordinator, site_id: str, description: SitePowerDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{site_id}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, site_id)})

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attrs_fn(self.coordinator.data or {})


class SiteArraySensor(SolarEdgeBaseSensor):
    """Sensores do site alimentados pelo coordenador do arranjo."""

    coordinator: ArrayCoordinator
    entity_description: SiteArrayDescription

    def __init__(self, coordinator, site_id: str, description: SiteArrayDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{site_id}_{description.key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, site_id)})

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attrs_fn(self.coordinator.data)


class _ComponentSensor(SolarEdgeBaseSensor):
    coordinator: ArrayCoordinator

    def __init__(self, coordinator, site_id: str, component: Component, key: str) -> None:
        super().__init__(coordinator)
        self._serial = component.serial
        self._key = key
        self._attr_unique_id = f"{site_id}_{component.serial}_{key}"
        # O dispositivo já existe (criado em discover()); aqui só o vínculo
        self._attr_device_info = DeviceInfo(
            identifiers={_component_identifier(site_id, component.serial)}
        )

    @property
    def _component(self) -> Component | None:
        layout = self.coordinator.data.layout
        return layout.optimizers.get(self._serial) or layout.inverters.get(self._serial)

    @property
    def _live(self) -> LiveData | None:
        return self.coordinator.data.live.get(self._serial)


class ComponentStatusSensor(_ComponentSensor):
    """Status do equipamento informado pelo layout (ACTIVE, ...)."""

    _attr_translation_key = "status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, site_id, component) -> None:
        super().__init__(coordinator, site_id, component, "status")

    @property
    def native_value(self) -> str | None:
        component = self._component
        return component.status if component else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        component = self._component
        if component is None:
            return None
        return {k: v for k, v in component.properties.items() if k != "status"}


class ComponentEnergySensor(_ComponentSensor):
    """Energia de hoje, do mês ou da vida útil (os dois primeiros zeram sozinhos)."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.WATT_HOUR
    _attr_suggested_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 2
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator, site_id, component, period: str) -> None:
        super().__init__(coordinator, site_id, component, period)
        self._attr_translation_key = period

    @property
    def native_value(self) -> float | None:
        return ENERGY_PERIODS[self._key](self.coordinator.data).get(self._serial)


class OptimizerMeasurementSensor(_ComponentSensor):
    """Medição ao vivo do otimizador (potência, tensão, corrente...)."""

    def __init__(self, coordinator, site_id, component, key: str) -> None:
        super().__init__(coordinator, site_id, component, key)
        unit = UNIT_MAP.get(self._live.units.get(key, "").lower())
        self._attr_native_unit_of_measurement = unit
        self._attr_device_class = DEVICE_CLASS_BY_UNIT.get(unit)
        if self._attr_device_class == SensorDeviceClass.ENERGY:
            self._attr_state_class = None
        elif unit is not None:
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if key in TRANSLATED_MEASUREMENTS:
            self._attr_translation_key = key
        else:
            self._attr_name = key.replace("_", " ").capitalize()

    @property
    def available(self) -> bool:
        live = self._live
        return super().available and live is not None and self._key in live.values

    @property
    def native_value(self) -> float | None:
        return _live_value(self._live, self._key)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        live = self._live
        if live is None or self._key not in ZERO_WHEN_STALE:
            return None
        return {"stale": _is_stale(live), "last_reported": live.values.get(self._key)}


class OptimizerTemperatureSensor(_ComponentSensor):
    """Temperatura máxima do dia, quando o portal fornece."""

    _attr_translation_key = "temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, site_id, component) -> None:
        super().__init__(coordinator, site_id, component, "temperature")
        _, unit = coordinator.data.daily.temperature[component.serial]
        self._attr_native_unit_of_measurement = UNIT_MAP.get(
            str(unit or "c").lower(), UnitOfTemperature.CELSIUS
        )

    @property
    def native_value(self) -> float | None:
        reading = self.coordinator.data.daily.temperature.get(self._serial)
        return reading[0] if reading else None


class LastMeasurementSensor(_ComponentSensor):
    """Horário da última telemetria recebida pelo portal."""

    _attr_translation_key = "last_measurement"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, site_id, component) -> None:
        super().__init__(coordinator, site_id, component, "last_measurement")

    @property
    def native_value(self) -> datetime | None:
        live = self._live
        return live.last_measurement if live else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        live = self._live
        return dict(live.attributes) if live else None
