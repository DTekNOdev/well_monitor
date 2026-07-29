"""Constants for Well Monitor integration."""
DOMAIN = "well_monitor"

# Config / options keys
CONF_VOLTAGE_ENTITY    = "voltage_entity"
CONF_CAL_VOLTAGE_LOW   = "cal_voltage_low"
CONF_CAL_DEPTH_LOW     = "cal_depth_low"
CONF_CAL_VOLTAGE_HIGH  = "cal_voltage_high"
CONF_CAL_DEPTH_HIGH    = "cal_depth_high"
CONF_WELL_DIAMETER_MM  = "well_diameter_mm"
CONF_EMA_TAU           = "ema_tau_seconds"
CONF_LONG_RATE_WINDOW  = "long_rate_window_seconds"
CONF_WATER_TABLE_WINDOW = "water_table_window_seconds"

# Defaults
DEFAULT_CAL_VOLTAGE_LOW  = 0.0
DEFAULT_CAL_DEPTH_LOW    = 0.0
DEFAULT_WELL_DIAMETER_MM = 110.0   # mm  (a common 4-inch borehole liner is ~110 mm ID)
DEFAULT_EMA_TAU          = 300.0   # seconds — time constant for voltage smoothing

# Device metadata
DEVICE_MANUFACTURER = "DTekNO"
DEVICE_MODEL        = "Borehole Depth Sensor"

# Rolling window used to compute fill / drain rate
RATE_WINDOW_SECONDS = 1800   # 30 minutes

# Window for computing natural recharge rate (longer for smoothing)
RECHARGE_WINDOW_SECONDS = 86400   # 24 hours

# Default windows for long-term rate and water table
DEFAULT_LONG_RATE_WINDOW    = 86400     # 24 hours
DEFAULT_WATER_TABLE_WINDOW  = 604800    # 7 days

# Outlier rejection — max voltage change per update considered physically plausible
# At 10 L/min discharge → 0.1 V/min. A 0.02V jump in 1 min is noise.
MAX_VOLTAGE_JUMP = 0.015   # volts — reject single spikes above this

# If N consecutive readings all exceed the jump threshold, accept the change
# as a real persistent shift (e.g. after recalibration).
CONSECUTIVE_OUTLIER_LIMIT = 5   # ~5 minutes of persistent deviation
