"""DataUpdateCoordinator for Well Monitor."""
import logging
import math
import time
from collections import deque
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    DOMAIN,
    CONF_VOLTAGE_ENTITY,
    CONF_CAL_VOLTAGE_LOW,
    CONF_CAL_DEPTH_LOW,
    CONF_CAL_VOLTAGE_HIGH,
    CONF_CAL_DEPTH_HIGH,
    CONF_WELL_DIAMETER_MM,
    CONF_EMA_TAU,
    DEFAULT_EMA_TAU,
    RATE_WINDOW_SECONDS,
)

_LOGGER = logging.getLogger(__name__)


class WellMonitorCoordinator(DataUpdateCoordinator):
    """Derives well depth, volume, and change rate from a voltage sensor."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        # Merge data + options so either can hold any key; options win on overlap.
        cfg = {**entry.data, **entry.options}

        self._voltage_entity: str = cfg[CONF_VOLTAGE_ENTITY]

        # ── Two-point linear calibration ──────────────────────────────────────
        # depth(V) = (V - V_zero) * depth_scale
        v_low  = float(cfg[CONF_CAL_VOLTAGE_LOW])
        d_low  = float(cfg[CONF_CAL_DEPTH_LOW])
        v_high = float(cfg[CONF_CAL_VOLTAGE_HIGH])
        d_high = float(cfg[CONF_CAL_DEPTH_HIGH])

        self._depth_scale: float  = (d_high - d_low) / (v_high - v_low)   # m/V
        self._voltage_zero: float = v_low - d_low / self._depth_scale       # V at depth=0

        # ── Well geometry (cylindrical borehole) ──────────────────────────────
        diameter_mm = float(cfg[CONF_WELL_DIAMETER_MM])
        radius_m = (diameter_mm / 1000.0) / 2.0
        self._litres_per_metre: float = math.pi * radius_m ** 2 * 1000.0
        self._max_depth_m: float = d_high

        # ── Time-weighted EMA smoothing ───────────────────────────────────────
        # alpha_t = 1 - exp(-dt / tau)
        # Long gap → alpha_t → 1.0 (trust the new reading fully).
        # Short gap → alpha_t → 0   (suppress noise).
        self._ema_tau: float = float(cfg.get(CONF_EMA_TAU, DEFAULT_EMA_TAU))
        self._ema_voltage: float | None = None
        self._last_voltage_time: float | None = None  # monotonic seconds

        # ── Rolling history for rate computation ──────────────────────────────
        self._history: deque = deque(maxlen=120)
        self._last_data_time: float = 0.0   # monotonic; 0 = no data yet

        # ── Published sensor values ───────────────────────────────────────────
        self.voltage:          float | None = None
        self.depth_m:          float | None = None
        self.volume_litres:    float | None = None
        self.level_pct:        float | None = None
        self.change_rate_lph:  float | None = None  # L/h; +ve = filling, -ve = draining

        # No background poll — driven by state-change events.
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=None,
        )

    # ──────────────────────────────────────────────────────────────────────────

    def async_setup_listeners(self, entry: ConfigEntry) -> None:
        """Subscribe to state changes and register the stale-rate timer."""
        entry.async_on_unload(
            async_track_state_change_event(
                self.hass,
                [self._voltage_entity],
                self._handle_source_update,
            )
        )
        # If the sensor goes silent (stable well), zero the rate after one window.
        entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._check_stale_rate,
                timedelta(seconds=RATE_WINDOW_SECONDS),
            )
        )

    async def _handle_source_update(self, event: Event) -> None:
        await self.async_request_refresh()

    async def _check_stale_rate(self, _now=None) -> None:
        """Zero the change rate if no reading has arrived within the rate window."""
        if self._last_data_time == 0.0:
            return  # never had a reading yet
        if time.monotonic() - self._last_data_time > RATE_WINDOW_SECONDS:
            if self.change_rate_lph != 0:
                self.change_rate_lph = 0
                if self.data is not None:
                    self.async_set_updated_data(
                        {**self.data, "change_rate_lph": 0}
                    )
                    _LOGGER.debug("Well: no update in >%ds — rate zeroed", RATE_WINDOW_SECONDS)

    # ──────────────────────────────────────────────────────────────────────────

    async def _async_update_data(self) -> dict:
        state = self.hass.states.get(self._voltage_entity)
        if state is None or state.state in ("unknown", "unavailable", ""):
            raise UpdateFailed(
                f"Voltage entity '{self._voltage_entity}' is unavailable"
            )

        try:
            raw_voltage = float(state.state)
        except (ValueError, TypeError) as exc:
            raise UpdateFailed(
                f"Cannot parse voltage value '{state.state}'"
            ) from exc

        # Time-weighted EMA: seed on first reading; weight by elapsed time after that.
        now_mono = time.monotonic()
        if self._ema_voltage is None or self._last_voltage_time is None:
            self._ema_voltage = raw_voltage
        else:
            dt = now_mono - self._last_voltage_time
            alpha_t = 1.0 - math.exp(-dt / self._ema_tau)
            self._ema_voltage = alpha_t * raw_voltage + (1.0 - alpha_t) * self._ema_voltage
        self._last_voltage_time = now_mono
        self._last_data_time = now_mono

        voltage = self._ema_voltage

        # Clamp depth to zero — sensor noise can produce slightly negative values.
        depth = max(0.0, (voltage - self._voltage_zero) * self._depth_scale)
        volume = depth * self._litres_per_metre
        level_pct = min(100.0, depth / self._max_depth_m * 100.0) if self._max_depth_m > 0 else None

        self.voltage       = round(voltage, 3)
        self.depth_m       = round(depth, 3)
        self.volume_litres = round(volume, 1)
        self.level_pct     = round(level_pct, 1) if level_pct is not None else None

        self._history.append((now_mono, self.volume_litres))
        self.change_rate_lph = self._compute_rate(now_mono)

        _LOGGER.debug(
            "Well: raw=%.3fV ema=%.3fV → %.3fm, %.1fL (%.1f%%), rate %.1f L/h",
            raw_voltage, voltage, self.depth_m,
            self.volume_litres, self.level_pct or 0, self.change_rate_lph or 0,
        )

        return {
            "voltage":         self.voltage,
            "depth_m":         self.depth_m,
            "volume_litres":   self.volume_litres,
            "level_pct":       self.level_pct,
            "change_rate_lph": self.change_rate_lph,
        }

    def _compute_rate(self, now: float) -> float | None:
        """L/h over the rolling RATE_WINDOW_SECONDS window."""
        cutoff = now - RATE_WINDOW_SECONDS
        window = [(t, v) for t, v in self._history if t >= cutoff]
        if len(window) < 2:
            return None
        elapsed_hours = (window[-1][0] - window[0][0]) / 3600.0
        if elapsed_hours < 1e-6:
            return None
        delta = window[-1][1] - window[0][1]
        return round(delta / elapsed_hours, 1)

    # ── Convenience properties used by fill-control automations ───────────────

    @property
    def is_filling(self) -> bool | None:
        if self.change_rate_lph is None:
            return None
        return self.change_rate_lph > 0.5

    @property
    def is_draining(self) -> bool | None:
        if self.change_rate_lph is None:
            return None
        return self.change_rate_lph < -0.5
