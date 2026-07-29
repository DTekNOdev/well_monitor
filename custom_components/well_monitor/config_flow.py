"""Config flow and options flow for Well Monitor."""
from __future__ import annotations

import math
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.selector import EntitySelector, EntitySelectorConfig

from .const import (
    DOMAIN,
    CONF_VOLTAGE_ENTITY,
    CONF_CAL_VOLTAGE_LOW,
    CONF_CAL_DEPTH_LOW,
    CONF_CAL_VOLTAGE_HIGH,
    CONF_CAL_DEPTH_HIGH,
    CONF_WELL_DIAMETER_MM,
    CONF_EMA_TAU,
    CONF_LONG_RATE_WINDOW,
    CONF_WATER_TABLE_WINDOW,
    CONF_MAX_RECHARGE_RATE,
    CONF_MAX_DISCHARGE_RATE,
    DEFAULT_CAL_VOLTAGE_LOW,
    DEFAULT_CAL_DEPTH_LOW,
    DEFAULT_WELL_DIAMETER_MM,
    DEFAULT_EMA_TAU,
    DEFAULT_LONG_RATE_WINDOW,
    DEFAULT_WATER_TABLE_WINDOW,
    DEFAULT_MAX_RECHARGE_RATE,
    DEFAULT_MAX_DISCHARGE_RATE,
)


def _litres_per_metre(diameter_mm: float) -> float:
    return math.pi * ((diameter_mm / 1000.0) / 2.0) ** 2 * 1000.0


# ── Initial config flow ────────────────────────────────────────────────────────

class WellMonitorConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Three-step setup: source → calibration → geometry."""

    VERSION = 1

    def __init__(self):
        super().__init__()
        self._data: dict = {}

    # Step 1: pick the voltage sensor entity
    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_calibration()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_VOLTAGE_ENTITY): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
            }),
            errors=errors,
        )

    # Step 2: two-point voltage → depth calibration
    async def async_step_calibration(self, user_input=None):
        errors = {}
        if user_input is not None:
            v_low  = user_input[CONF_CAL_VOLTAGE_LOW]
            v_high = user_input[CONF_CAL_VOLTAGE_HIGH]
            d_low  = user_input[CONF_CAL_DEPTH_LOW]
            d_high = user_input[CONF_CAL_DEPTH_HIGH]

            if v_high <= v_low:
                errors[CONF_CAL_VOLTAGE_HIGH] = "voltage_high_must_exceed_low"
            elif d_high <= d_low:
                errors[CONF_CAL_DEPTH_HIGH] = "depth_high_must_exceed_low"
            else:
                self._data.update(user_input)
                return await self.async_step_geometry()

        return self.async_show_form(
            step_id="calibration",
            data_schema=vol.Schema({
                vol.Required(CONF_CAL_VOLTAGE_LOW,  default=DEFAULT_CAL_VOLTAGE_LOW):  vol.Coerce(float),
                vol.Required(CONF_CAL_DEPTH_LOW,    default=DEFAULT_CAL_DEPTH_LOW):    vol.Coerce(float),
                vol.Required(CONF_CAL_VOLTAGE_HIGH, default=1.0):                      vol.Coerce(float),
                vol.Required(CONF_CAL_DEPTH_HIGH,   default=5.0):                      vol.Coerce(float),
            }),
            errors=errors,
        )

    # Step 3: well geometry and polling interval
    async def async_step_geometry(self, user_input=None):
        errors = {}
        if user_input is not None:
            if user_input[CONF_WELL_DIAMETER_MM] <= 0:
                errors[CONF_WELL_DIAMETER_MM] = "diameter_must_be_positive"
            else:
                self._data.update(user_input)
                lpm = _litres_per_metre(user_input[CONF_WELL_DIAMETER_MM])
                d_high = self._data[CONF_CAL_DEPTH_HIGH]
                title = (
                    f"Well Monitor "
                    f"({user_input[CONF_WELL_DIAMETER_MM]:.0f} mm bore, "
                    f"{lpm * d_high:.0f} L max)"
                )
                return self.async_create_entry(title=title, data=self._data)

        return self.async_show_form(
            step_id="geometry",
            data_schema=vol.Schema({
                vol.Required(CONF_WELL_DIAMETER_MM, default=DEFAULT_WELL_DIAMETER_MM): vol.Coerce(float),
                vol.Required(CONF_EMA_TAU, default=DEFAULT_EMA_TAU): vol.All(vol.Coerce(float), vol.Range(min=10, max=3600)),
            }),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return WellMonitorOptionsFlow()


# ── Options flow (reconfiguration after setup) ─────────────────────────────────

class WellMonitorOptionsFlow(config_entries.OptionsFlow):
    """Allow the user to tweak calibration and geometry without re-adding the device."""

    async def async_step_init(self, user_input=None):
        # Merge data + existing options for current defaults
        cfg = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            v_low  = user_input[CONF_CAL_VOLTAGE_LOW]
            v_high = user_input[CONF_CAL_VOLTAGE_HIGH]
            d_low  = user_input[CONF_CAL_DEPTH_LOW]
            d_high = user_input[CONF_CAL_DEPTH_HIGH]
            errors = {}

            if v_high <= v_low:
                errors[CONF_CAL_VOLTAGE_HIGH] = "voltage_high_must_exceed_low"
            elif d_high <= d_low:
                errors[CONF_CAL_DEPTH_HIGH] = "depth_high_must_exceed_low"
            elif user_input[CONF_WELL_DIAMETER_MM] <= 0:
                errors[CONF_WELL_DIAMETER_MM] = "diameter_must_be_positive"
            else:
                return self.async_create_entry(title="", data=user_input)


            return self.async_show_form(
                step_id="init",
                data_schema=self._schema(user_input),
                errors=errors,
            )

        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(cfg),
        )

    def _schema(self, defaults: dict) -> vol.Schema:
        return vol.Schema({
            vol.Required(CONF_CAL_VOLTAGE_LOW,     default=defaults.get(CONF_CAL_VOLTAGE_LOW,   DEFAULT_CAL_VOLTAGE_LOW)):     vol.Coerce(float),
            vol.Required(CONF_CAL_DEPTH_LOW,       default=defaults.get(CONF_CAL_DEPTH_LOW,     DEFAULT_CAL_DEPTH_LOW)):       vol.Coerce(float),
            vol.Required(CONF_CAL_VOLTAGE_HIGH,    default=defaults.get(CONF_CAL_VOLTAGE_HIGH,  1.0)):                         vol.Coerce(float),
            vol.Required(CONF_CAL_DEPTH_HIGH,      default=defaults.get(CONF_CAL_DEPTH_HIGH,    5.0)):                         vol.Coerce(float),
            vol.Required(CONF_WELL_DIAMETER_MM,    default=defaults.get(CONF_WELL_DIAMETER_MM,  DEFAULT_WELL_DIAMETER_MM)):    vol.Coerce(float),
            vol.Required(CONF_EMA_TAU,             default=defaults.get(CONF_EMA_TAU,           DEFAULT_EMA_TAU)):             vol.All(vol.Coerce(float), vol.Range(min=10, max=3600)),
            vol.Optional(CONF_MAX_RECHARGE_RATE,   default=defaults.get(CONF_MAX_RECHARGE_RATE, DEFAULT_MAX_RECHARGE_RATE)):   vol.All(vol.Coerce(float), vol.Range(min=1, max=36000)),
            vol.Optional(CONF_MAX_DISCHARGE_RATE,  default=defaults.get(CONF_MAX_DISCHARGE_RATE, DEFAULT_MAX_DISCHARGE_RATE)): vol.All(vol.Coerce(float), vol.Range(min=1, max=36000)),
            vol.Optional(CONF_LONG_RATE_WINDOW,    default=defaults.get(CONF_LONG_RATE_WINDOW,  DEFAULT_LONG_RATE_WINDOW)):    vol.All(vol.Coerce(float), vol.Range(min=3600, max=604800)),
            vol.Optional(CONF_WATER_TABLE_WINDOW,  default=defaults.get(CONF_WATER_TABLE_WINDOW, DEFAULT_WATER_TABLE_WINDOW)):  vol.All(vol.Coerce(float), vol.Range(min=86400, max=2592000)),
        })
