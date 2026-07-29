"""DataUpdateCoordinator for Well Monitor."""
import json
import logging
import math
import time
from collections import deque
from datetime import timedelta
from pathlib import Path

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
    CONSECUTIVE_OUTLIER_LIMIT,
    RATE_WINDOW_SECONDS,
    RECHARGE_WINDOW_SECONDS,
    CONF_LONG_RATE_WINDOW,
    CONF_WATER_TABLE_WINDOW,
    CONF_MAX_RECHARGE_RATE,
    CONF_MAX_DISCHARGE_RATE,
    DEFAULT_LONG_RATE_WINDOW,
    DEFAULT_WATER_TABLE_WINDOW,
    DEFAULT_MAX_RECHARGE_RATE,
    DEFAULT_MAX_DISCHARGE_RATE,
)

_LOGGER = logging.getLogger(__name__)

# How often to persist history to disk (seconds)
PERSIST_INTERVAL = 300  # 5 minutes


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

        # ── Outlier rejection limits (L/h) ───────────────────────────────────
        self._max_recharge_rate: float = float(cfg.get(CONF_MAX_RECHARGE_RATE, DEFAULT_MAX_RECHARGE_RATE))
        self._max_discharge_rate: float = float(cfg.get(CONF_MAX_DISCHARGE_RATE, DEFAULT_MAX_DISCHARGE_RATE))

        # ── Long-term windows ────────────────────────────────────────────────
        self._long_rate_window: float = float(cfg.get(CONF_LONG_RATE_WINDOW, DEFAULT_LONG_RATE_WINDOW))
        self._water_table_window: float = float(cfg.get(CONF_WATER_TABLE_WINDOW, DEFAULT_WATER_TABLE_WINDOW))
        # Cap volume history at ~7 days of 1-min data
        max_vol_entries = int(self._water_table_window / 60) + 100
        self._volume_history: deque = deque(maxlen=max_vol_entries)  # (time, volume_litres, depth_m)

        # ── Rolling history for rate computation ──────────────────────────────
        self._history: deque = deque(maxlen=120)
        self._last_data_time: float = 0.0   # monotonic; 0 = no data yet

        # ── Recharge rate tracking ────────────────────────────────────────────
        self._recharge_history: deque = deque(maxlen=2000)  # (time, volume, rate)
        self.recharge_rate_lph: float | None = None  # max recharge rate in window

        # ── Persistence ───────────────────────────────────────────────────────
        self._history_file = Path(hass.config.path(f"{DOMAIN}_history.json"))
        self._last_persist_time: float = 0.0
        self._history_loaded: bool = False

        # ── Outlier rejection ──────────────────────────────────────────────────
        self._consecutive_outliers: int = 0

        # ── Published sensor values ───────────────────────────────────────────
        self.voltage:            float | None = None
        self.depth_m:            float | None = None
        self.volume_litres:      float | None = None
        self.level_pct:          float | None = None
        self.change_rate_lph:    float | None = None  # L/h; short-term rolling
        self.long_term_rate_lph: float | None = None  # L/h; persists when idle
        self.water_table_volume: float | None = None  # L; rolling max volume
        self.water_table_depth:  float | None = None  # m; depth at max volume

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
        """Zero the short-term rate if no reading has arrived within the rate window.

        The long-term rate is preserved so it does not decay during idle periods.
        """
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
        if not self._history_loaded:
            await self._load_history()
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

        # Time-weighted EMA with rate-based outlier rejection.
        # Computes the implied rate of change from the voltage delta and
        # calibration; rejects if it exceeds the configured max for that
        # direction (filling or draining).  If the deviation persists for
        # CONSECUTIVE_OUTLIER_LIMIT readings, it is accepted as a real
        # change (handles recalibration or pump modification).
        now_mono = time.monotonic()
        if self._ema_voltage is None or self._last_voltage_time is None:
            self._ema_voltage = raw_voltage
            self._consecutive_outliers = 0
            self._last_voltage_time = now_mono
            self._last_data_time = now_mono
        else:
            dt = now_mono - self._last_voltage_time
            delta_v = raw_voltage - self._ema_voltage
            # implied_rate = delta_V × m/V × L/m ÷ (dt / 3600)
            implied_rate = delta_v * self._depth_scale * self._litres_per_metre / (dt / 3600.0)
            max_rate = self._max_recharge_rate if implied_rate > 0 else self._max_discharge_rate
            accept = True
            if abs(implied_rate) > max_rate:
                self._consecutive_outliers += 1
                if self._consecutive_outliers >= CONSECUTIVE_OUTLIER_LIMIT:
                    self._consecutive_outliers = 0
                    _LOGGER.debug(
                        "Well: persistent %.0f L/h accepted after %d outliers",
                        implied_rate, CONSECUTIVE_OUTLIER_LIMIT,
                    )
                else:
                    accept = False
                    _LOGGER.debug(
                        "Well: outlier %.0f L/h rejected (max %d, outlier #%d)",
                        implied_rate, int(max_rate), self._consecutive_outliers,
                    )
            else:
                self._consecutive_outliers = 0

            if accept:
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

        now_ts = time.time()
        self._history.append((now_ts, self.volume_litres))
        self.change_rate_lph = self._compute_rate(now_ts)

        # Track positive rates for recharge rate computation
        if self.change_rate_lph is not None and self.change_rate_lph > 0:
            self._recharge_history.append(
                (now_ts, self.volume_litres, self.change_rate_lph)
            )
        self.recharge_rate_lph = self._compute_recharge_rate(now_ts)

        # ── Long-term volume history ──────────────────────────────────────────
        self._volume_history.append((now_ts, self.volume_litres, self.depth_m))
        self.long_term_rate_lph = self._compute_long_term_rate(now_ts)
        self.water_table_volume, self.water_table_depth = self._compute_water_table(now_ts)

        await self._save_history()

        _LOGGER.debug(
            "Well: raw=%.3fV ema=%.3fV → %.3fm, %.1fL (%.1f%%), "
            "rate=%.1f L/h long=%.1f L/h recharge=%.1f L/h table=%.1fL",
            raw_voltage, voltage, self.depth_m,
            self.volume_litres, self.level_pct or 0,
            self.change_rate_lph or 0, self.long_term_rate_lph or 0,
            self.recharge_rate_lph or 0, self.water_table_volume or 0,
        )

        return {
            "voltage":            self.voltage,
            "depth_m":            self.depth_m,
            "volume_litres":      self.volume_litres,
            "level_pct":          self.level_pct,
            "change_rate_lph":    self.change_rate_lph,
            "recharge_rate_lph":  self.recharge_rate_lph,
            "long_term_rate_lph": self.long_term_rate_lph,
            "water_table_volume": self.water_table_volume,
            "water_table_depth":  self.water_table_depth,
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

    def _compute_recharge_rate(self, now: float) -> float | None:
        """Maximum positive recharge rate over the last RECHARGE_WINDOW_SECONDS.

        Only samples where rate > 0 are considered (natural recovery periods).
        Returns the maximum observed rate, which represents the fastest
        recharge when the well is lowest.
        """
        cutoff = now - RECHARGE_WINDOW_SECONDS
        positive = [r for t, _, r in self._recharge_history if t >= cutoff and r > 0]
        if not positive:
            return None
        return round(max(positive), 1)

    def _compute_long_term_rate(self, now: float) -> float | None:
        """L/h over the long-term window.

        Unlike the short-term rate, this keeps its last known value when the
        window has too few data points (well idle), so it does not decay.
        """
        cutoff = now - self._long_rate_window
        window = [(t, v) for t, v in self._history if t >= cutoff]
        if len(window) >= 2:
            elapsed_hours = (window[-1][0] - window[0][0]) / 3600.0
            if elapsed_hours >= 1e-6:
                delta = window[-1][1] - window[0][1]
                self.long_term_rate_lph = round(delta / elapsed_hours, 1)
        return self.long_term_rate_lph

    def _compute_water_table(self, now: float) -> tuple[float | None, float | None]:
        """Rolling maximum volume and its corresponding depth over the water-table window.

        This reflects the natural groundwater level: the highest the water has
        been during the window (typically when the well is at rest).
        """
        cutoff = now - self._water_table_window
        max_vol: float | None = None
        max_depth: float | None = None
        while self._volume_history:
            t, v, d = self._volume_history[0]
            if t >= cutoff:
                break
            self._volume_history.popleft()
        for _, v, d in self._volume_history:
            if max_vol is None or v > max_vol:
                max_vol = v
                max_depth = d
        return (max_vol, max_depth)

    # ── Persistence ────────────────────────────────────────

    async def _load_history(self) -> None:
        """Load history deques from disk."""
        def _read():
            if not self._history_file.exists():
                return None
            return json.loads(self._history_file.read_text())

        try:
            data = await self.hass.async_add_executor_job(_read)
        except Exception as exc:
            _LOGGER.warning("Well: failed to load history: %s", exc)
            self._history_loaded = True
            return

        if data is None:
            self._history_loaded = True
            return

        now = time.time()
        # Only load data within the retention windows
        self._history = deque(
            [(t, v) for t, v in data.get("history", []) if now - t < RATE_WINDOW_SECONDS],
            maxlen=120,
        )
        self._recharge_history = deque(
            [(t, v, r) for t, v, r in data.get("recharge_history", []) if now - t < RECHARGE_WINDOW_SECONDS],
            maxlen=2000,
        )
        self._volume_history = deque(
            [(t, v, d) for t, v, d in data.get("volume_history", []) if now - t < self._water_table_window],
            maxlen=self._volume_history.maxlen,
        )
        self._history_loaded = True
        _LOGGER.debug(
            "Well: loaded %d history, %d recharge, %d volume samples",
            len(self._history), len(self._recharge_history), len(self._volume_history),
        )

    async def _save_history(self) -> None:
        """Persist history deques to disk."""
        now = time.time()
        if now - self._last_persist_time < PERSIST_INTERVAL:
            return
        self._last_persist_time = now
        data = {
            "history": list(self._history),
            "recharge_history": list(self._recharge_history),
            "volume_history": list(self._volume_history),
        }

        def _write():
            self._history_file.write_text(json.dumps(data))

        try:
            await self.hass.async_add_executor_job(_write)
            _LOGGER.debug("Well: persisted history to disk")
        except Exception as exc:
            _LOGGER.warning("Well: failed to persist history: %s", exc)

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
