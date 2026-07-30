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
    RATE_WINDOW_SECONDS,
    RECHARGE_WINDOW_SECONDS,
    CONF_LONG_RATE_WINDOW,
    CONF_WATER_TABLE_WINDOW,
    DEFAULT_LONG_RATE_WINDOW,
    DEFAULT_WATER_TABLE_WINDOW,
    FILTER_TICK_SECONDS,
    CONF_FILTER_METHOD,
    DEFAULT_FILTER_METHOD,
    FILTER_METHOD_MODEL,
)
from .filter import DutyDecoder, OutputSmoother
from .ladder import LadderEstimator

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

        # ── Level estimator (see filter.py / ladder.py) ───────────────────────
        # Uses wall-clock time so filter state survives restarts via the
        # persisted history file.  The method is selectable per entry, so two
        # entries on the same input voltage can run one method each.
        self._filter_method: str = cfg.get(CONF_FILTER_METHOD, DEFAULT_FILTER_METHOD)
        self._decoder = DutyDecoder()
        self._smoother = OutputSmoother()
        self._ladder = LadderEstimator()

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
        # Per-entry persistence so multiple entries (e.g. one per filter
        # method on the same input) don't overwrite each other's state.
        self._history_file = Path(
            hass.config.path(f"{DOMAIN}_history_{entry.entry_id}.json")
        )
        # First entry upgrade path: adopt the legacy shared file if present.
        self._legacy_history_file = Path(hass.config.path(f"{DOMAIN}_history.json"))
        self._last_persist_time: float = 0.0
        self._history_loaded: bool = False

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
        # Periodic filter tick: the source entity emits nothing while its value
        # is unchanged, but the duty decoder integrates dwell-time evidence —
        # advance it with the held reading so long dwells register.
        entry.async_on_unload(
            async_track_time_interval(
                self.hass,
                self._filter_tick,
                timedelta(seconds=FILTER_TICK_SECONDS),
            )
        )

    async def _handle_source_update(self, event: Event) -> None:
        await self.async_request_refresh()

    async def _filter_tick(self, _now=None) -> None:
        """Advance the filter with the held value when no events arrive."""
        state = self.hass.states.get(self._voltage_entity)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return  # don't spam UpdateFailed while the source is away
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

        # Level estimation (wall-clock time base so the filter state persisted
        # in the history file stays valid on restart).  Both estimators are
        # advanced every update so switching method in the options never
        # starts from cold — only the published value changes.
        now_wall = time.time()
        decoded = self._decoder.update(now_wall, raw_voltage)
        quiet = now_wall - self._decoder.last_anchor_t
        duty_voltage = self._smoother.update(now_wall, decoded, quiet=quiet)
        model_voltage = self._ladder.update(now_wall, raw_voltage)
        voltage = (
            model_voltage if self._filter_method == FILTER_METHOD_MODEL
            else duty_voltage
        )
        self._last_data_time = time.monotonic()

        # Clamp depth to zero — sensor noise can produce slightly negative values.
        depth = max(0.0, (voltage - self._voltage_zero) * self._depth_scale)
        volume = depth * self._litres_per_metre
        level_pct = min(100.0, depth / self._max_depth_m * 100.0) if self._max_depth_m > 0 else None

        self.filter_method = self._filter_method
        self.filter_rung   = self._ladder.rung
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
            "Well: raw=%.3fV filt=%.3fV → %.3fm, %.1fL (%.1f%%), "
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
        """Load history deques from disk.

        The legacy shared file (pre-multi-entry installs) is adopted at most
        once: the first entry to find it takes the data over and the file is
        retired immediately.  Without that, every newly added entry — e.g. a
        second device for the experimental estimator — would silently inherit
        the first device's learned rates and recharge history.
        """
        def _read():
            if self._history_file.exists():
                return json.loads(self._history_file.read_text())
            if self._legacy_history_file.exists():
                data = json.loads(self._legacy_history_file.read_text())
                # claim it: retire the legacy file so no later entry adopts it
                self._legacy_history_file.rename(
                    self._legacy_history_file.with_suffix(".json.migrated")
                )
                return data
            return None

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
        # Restore filter state so a restart doesn't snap the level back to the
        # quantized raw value (wall-clock time base makes this valid).
        if "decoder_state" in data:
            self._decoder.restore(data["decoder_state"])
        if "smoother_state" in data:
            self._smoother.restore(data["smoother_state"])
        if "ladder_state" in data:
            self._ladder.restore(data["ladder_state"])
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
            "decoder_state": self._decoder.to_dict(),
            "smoother_state": self._smoother.to_dict(),
            "ladder_state": self._ladder.to_dict(),
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
