"""
Central thresholds for the systematic memecoin shorting research pipeline.
All numbers here are research/config choices, not derived constants -- keeping them
in one place makes the framework auditable and easy to retune.
"""

BASE_DIR = "crypto csv data"

# ---- Step 1: history sufficiency -------------------------------------------------
MIN_DAILY_BARS = 30
MIN_MINUTE_DAYS = 7
MIN_MINUTE_ROWS = 2000          # coverage floor so 7 sparse days don't pass

# ---- Step 1: cleaning -------------------------------------------------------------
WICK_RATIO_MULT = 8.0           # Close vs rolling median ratio considered a candidate spike
WICK_VOL_CONFIRM_FRAC = 0.10    # bar volume must be < this * rolling median volume to be "unconfirmed"
WICK_REVERT_FRAC = 0.5          # next bar must revert by >= 50% of the move to call it a bad tick
ROLLING_WINDOW = 11             # bars, centered, for rolling median price/volume in wick detection

# ---- Step 2: descriptive stats ----------------------------------------------------
REALIZED_VOL_WINDOWS = (7, 30)  # days
ANNUALIZATION = 365
MAE_HORIZONS = (1, 3, 5, 10)    # days, naive-short MAE proxy

# ---- Step 3: spike / momentum / volume diagnostics --------------------------------
SPIKE_STD_MULT = 2.0            # close > rolling_mean + X*rolling_std
SPIKE_MEDIAN_MULT = 2.0         # close > Y * rolling_median
SPIKE_ROLL_WINDOW = 20          # trailing days, excludes current bar (shifted)
SPIKE_FORWARD_HORIZONS = (1, 3, 5, 10)
SPIKE_PRIMARY_HORIZON = 5
MIN_SPIKE_EVENTS = 8

MOMENTUM_WINDOW = 10            # days
MOMENTUM_PCTL = 0.85            # symbol-relative extreme-momentum percentile
MIN_MOMENTUM_EVENTS = 8

VOLUME_PCTL = 0.90
MIN_VOLUME_EVENTS = 8

# significance / hit-rate gates used by the edge classifier
SIG_TSTAT = 1.5                 # |t| considered "significant enough" at small-n research scale
SPIKE_HITRATE_MIN = 0.55
GRIND_MIN_DAILY_BARS = 60

# ---- Step 3.4: optional intraday check ---------------------------------------------
INTRADAY_MOVE_STD_MULT = 4.0
INTRADAY_FORWARD_MIN = 15
INTRADAY_MIN_EVENTS = 30
INTRADAY_HITRATE_MIN = 0.60
INTRADAY_SIG_TSTAT = 2.0

# ---- Step 4: risk / leverage --------------------------------------------------------
MAE_SL_PERCENTILE = 0.925        # SL set so historical MAE exceeds it ~7.5% of the time
SL_FLOOR_PCT = 12.0              # never propose a stop tighter than this (noise/outlier protection)
SL_CAP_PCT = 80.0                # never propose a stop this wide; outlier-dominated -> cap & flag
TP_RR_MIN = 1.0                  # take-profit at least 1:1 vs stop
TP_RR_MAX = 2.0                  # and never more ambitious than 2:1 for mean-reversion shorts

VOL_CAP_THRESHOLD_ANNUAL = 1.50  # 150% annualized realized vol
VOL_CAP_LEVERAGE = 5.0
PUMP_CAP_THRESHOLD = 2.00        # historical single-day pump > 200%
PUMP_CAP_LEVERAGE = 3.0

LIQ_SL_BUFFER_MULT = 1.25        # SL must sit at <= liq_move / 1.25 (25% buffer before liquidation)
LIQ_TAIL_SIGMA = 4.0             # liq distance must also clear a 4-sigma move over the holding horizon
MAX_LEVERAGE_HARD_CAP = 5.0
MIN_LEVERAGE = 1.0

# ---- Step 5: backtest ---------------------------------------------------------------
HOLD_DAYS_SPIKE = 5
HOLD_DAYS_GRIND = 7
GRIND_ENTRY_WINDOW = 10           # local N-day-high entries for grind-down bounces

MIN_TRADES_FOR_CONFIDENCE = 5
MIN_HIT_RATE = 0.40
MAX_DD_DOWNGRADE_PCT = -50.0      # cumulative simple-sum equity drawdown, in % units
DOMINANCE_FRAC = 0.50             # if one trade > 50% of total positive pnl -> fragile
