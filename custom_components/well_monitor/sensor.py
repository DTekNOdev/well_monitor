"""Sensor platform for Well Monitor — one device, six sensors."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, DEVICE_MANUFACTURER, DEVICE_MODEL
from .coordinator import WellMonitorCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: WellMonitorCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        WellVoltageSensor(coordinator, entry),
        WellDepthSensor(coordinator, entry),
        WellVolumeSensor(coordinator, entry),
        WellLevelSensor(coordinator, entry),
        WellRateSensor(coordinator, entry),
        WellRechargeRateSensor(coordinator, entry),
    ])


# ── Shared device info ─────────────────────────────────────────────────────────

def _device(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer=DEVICE_MANUFACTURER,
        model=DEVICE_MODEL,
        entry_type=None,
    )


# ── Base class ─────────────────────────────────────────────────────────────────

class _WellBase(CoordinatorEntity[WellMonitorCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WellMonitorCoordinator,
        entry: ConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id   = f"{entry.entry_id}_{key}"
        self._attr_device_info = _device(entry)


# ── Sensors ────────────────────────────────────────────────────────────────────

class WellVoltageSensor(_WellBase):
    """Raw voltage from the depth sensor — hidden by default, useful for calibration."""

    _attr_name                        = "Voltage"
    _attr_native_unit_of_measurement  = "V"
    _attr_device_class                = SensorDeviceClass.VOLTAGE
    _attr_state_class                 = SensorStateClass.MEASUREMENT
    _attr_entity_registry_enabled_default = False   # diagnostic; enable when needed

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "voltage")

    @property
    def native_value(self):
        return self.coordinator.voltage


class WellDepthSensor(_WellBase):
    """Height of the water column in the borehole (metres)."""

    _attr_name                       = "Water Depth"
    _attr_native_unit_of_measurement = "m"
    _attr_device_class               = SensorDeviceClass.DISTANCE
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_icon                       = "mdi:waves-arrow-up"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "depth")

    @property
    def native_value(self):
        return self.coordinator.depth_m


class WellVolumeSensor(_WellBase):
    """Estimated water volume in the borehole (litres), assuming cylindrical geometry."""

    _attr_name                       = "Water Volume"
    _attr_native_unit_of_measurement = "L"
    _attr_device_class               = SensorDeviceClass.VOLUME_STORAGE
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_icon                       = "mdi:water-well"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "volume")

    @property
    def native_value(self):
        return self.coordinator.volume_litres


class WellLevelSensor(_WellBase):
    """Water level as a percentage of the calibrated maximum depth."""

    _attr_name                       = "Water Level"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_icon                       = "mdi:gauge"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "level")

    @property
    def native_value(self):
        return self.coordinator.level_pct


class WellRateSensor(_WellBase):
    """Rolling rate of change over the last 10 minutes (L/h).

    Positive  = well is filling (recharge > extraction).
    Negative  = well is draining (extraction > recharge).
    """

    _attr_name                       = "Change Rate"
    _attr_native_unit_of_measurement = "L/h"
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_icon                       = "mdi:water-sync"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "rate")

    @property
    def native_value(self):
        return self.coordinator.change_rate_lph

    @property
    def extra_state_attributes(self):
        rate = self.coordinator.change_rate_lph
        if rate is None:
            return {}
        if rate > 0.5:
            direction = "filling"
        elif rate < -0.5:
            direction = "draining"
        else:
            direction = "stable"
        return {"direction": direction}


class WellRechargeRateSensor(_WellBase):
    """Maximum natural recharge rate observed over the last 24 hours (L/h).

    Only samples during positive-rate (recovery) periods are considered.
    The maximum gives the fastest recharge when the well is lowest.
    """

    _attr_name                       = "Recharge Rate"
    _attr_native_unit_of_measurement = "L/h"
    _attr_state_class                = SensorStateClass.MEASUREMENT
    _attr_icon                       = "mdi:water-plus"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "recharge_rate")

    @property
    def native_value(self):
        return self.coordinator.recharge_rate_lph

    @property
    def extra_state_attributes(self):
        rate = self.coordinator.recharge_rate_lph
        if rate is None:
            return {}
        return {
            "window_hours": 24,
            "description": "Max recharge rate during recovery periods",
        }
