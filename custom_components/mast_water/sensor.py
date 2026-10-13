"""Sensors for MAST water meter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import MastConfigEntry
from .const import CONF_CARD, DOMAIN
from .coordinator import MastCoordinator, MastData

# Deliberately no state_class: the long-term history comes from the external
# statistic (see README), and a state_class here would create a second,
# differently-timestamped series that double counts in the Energy dashboard.


@dataclass(frozen=True, kw_only=True)
class MastSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MastData], float | datetime | None]


SENSORS: tuple[MastSensorDescription, ...] = (
    MastSensorDescription(
        key="meter_reading",
        translation_key="meter_reading",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        value_fn=lambda d: d.latest_reading,
    ),
    MastSensorDescription(
        key="daily_usage",
        translation_key="daily_usage",
        device_class=SensorDeviceClass.WATER,
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        suggested_display_precision=3,
        value_fn=lambda d: d.daily_usage,
    ),
    MastSensorDescription(
        key="token_expiry",
        translation_key="token_expiry",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.token_expiry,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: MastConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        MastSensor(coordinator, entry.data[CONF_CARD], desc) for desc in SENSORS
    )


class MastSensor(CoordinatorEntity[MastCoordinator], SensorEntity):
    """A MAST sensor."""

    _attr_has_entity_name = True
    entity_description: MastSensorDescription

    def __init__(
        self, coordinator: MastCoordinator, card: str, description: MastSensorDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{card}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, card)},
            name="MAST water meter",
            manufacturer="MAST",
        )

    @property
    def native_value(self) -> float | datetime | None:
        return self.entity_description.value_fn(self.coordinator.data)
