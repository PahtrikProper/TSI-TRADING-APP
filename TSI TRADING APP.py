# ================================================================
# SECTION 1 — IMPORTS + GLOBAL SETTINGS + APPLICATION SKELETON
# ================================================================

import sys
import json
import time
import threading
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QDockWidget, QPushButton, QComboBox, QLineEdit,
    QTableWidget, QTableWidgetItem, QFileDialog, QSplitter
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QPalette, QColor

import websocket
import requests

# Disable SSL warnings for Binance
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ================================================================
# GLOBALS
# ================================================================

BINANCE_REST = "https://api.binance.com"
DEFAULT_SYMBOL = "SOLUSDT"
DEFAULT_INTERVAL = "15m"
EMA = "EMA"
SMA = "SMA"
WMA = "WMA"


# Convert Binance interval strings → milliseconds
INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}


# ================================================================
# SECTION 2 — BINANCE REST DATA FETCHER (HISTORICAL + SYMBOL LIST)
# ================================================================

# ... [other imports/constants above, if present] ...

def safe_get_json(url, params=None):
    """Fetch JSON from Binance safely (no crashes on bad responses)."""
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("REST API error:", e)
        return None

def fetch_all_symbols():
    """
    Returns a clean list of all active USDT trading pairs.
    Filters:
        ✔ spot only
        ✔ trading status = TRADING
        ✔ quote asset = USDT
    """
    url = BINANCE_REST + "/api/v3/exchangeInfo"
    data = safe_get_json(url)
    if data is None:
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

    symbols = []
    for s in data.get("symbols", []):
        if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
            symbols.append(s["symbol"])

    symbols.sort()
    return symbols


def fetch_historical_klines(symbol=DEFAULT_SYMBOL, interval=DEFAULT_INTERVAL, days=30):
    """
    Fetch the most recent available candles for the interval (up to Binance's limit, e.g., 1500).
    """
    print(f"[REST] Fetching {symbol} {interval} recent history (limit 1500)…")

    url = BINANCE_REST + "/api/v3/klines"
    params = {
        "symbol": symbol.upper(),
        "interval": interval,
        "limit": 1500  # max allowed, fetches latest 1500
    }
    raw = safe_get_json(url, params)
    if not raw:
        raise RuntimeError("Failed to fetch klines")

    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "num_trades", "tbbav", "tbqav", "ignore"
    ])

    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("time", inplace=True)
    df = df[["open", "high", "low", "close", "volume"]]

    print("Last historical candle:", df.index[-1])
    print("Current UTC time:      ", datetime.utcnow())
    diff = datetime.utcnow() - df.index[-1].to_pydatetime()
    print("Age of last candle:", diff)

    if diff.total_seconds() > (INTERVAL_MS[interval] / 1000) * 2:
        print("\n[WARNING] Historical data is older than expected!")
        print("This means Binance did not return the most recent candle.\n")

    return df




# ================================================================
# SECTION 3 — REAL-TIME WEBSOCKET CANDLE STREAM (BINANCE) — UPDATED
# ================================================================

class CandleStream(QObject):
    new_candle = Signal(dict)

    def __init__(self, symbol=DEFAULT_SYMBOL, interval=DEFAULT_INTERVAL):
        super().__init__()
        self.symbol = symbol.lower()
        self.interval = interval
        self.ws = None
        self.thread = None
        self.running = False

    def _run(self):
        url = f"wss://stream.binance.com:9443/ws/{self.symbol}@kline_{self.interval}"

        def on_message(ws, msg):
            try:
                k = json.loads(msg).get("k", {})

                candle = {
                    "time": pd.to_datetime(k["t"], unit="ms"),
                    "open": float(k["o"]),
                    "high": float(k["h"]),
                    "low": float(k["l"]),
                    "close": float(k["c"]),
                    "volume": float(k["v"]),
                    "is_closed": bool(k["x"])
                }

                self.new_candle.emit(candle)

            except Exception as e:
                print("WS parse error:", e)

        def on_error(ws, err):
            print("WebSocket error:", err)

        def on_close(ws, a, b):
            print("WebSocket closed — reconnecting...")
            if self.running:
                time.sleep(2)
                self.start()

        def on_open(ws):
            print(f"[WS] Connected → {self.symbol.upper()} {self.interval}")

        self.ws = websocket.WebSocketApp(
            url,
            on_open=on_open,
            on_error=on_error,
            on_close=on_close,
            on_message=on_message
        )

        self.ws.run_forever(ping_interval=15, ping_timeout=5)

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        try:
            if self.ws:
                self.ws.close()
        except:
            pass






# ================================================================
# SECTION 4 — INDICATORS (EMA, SMA, WMA, TSI, TREND FILTER)
# ================================================================

def ema(series, length):
    return series.ewm(span=length, adjust=False).mean()


def sma(series, length):
    return series.rolling(length).mean()


def wma(series, length):
    w = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)


def moving_average(series, length, ma_type):
    ma_type = ma_type.upper()
    if ma_type == "EMA":
        return ema(series, length)
    elif ma_type == "SMA":
        return sma(series, length)
    elif ma_type == "WMA":
        return wma(series, length)
    else:
        raise ValueError(f"Unknown MA type: {ma_type}")


# ---------- TSI SUPPORT FUNCTIONS ----------

def double_smooth(series, long_len, short_len):
    s1 = ema(series, long_len)
    s2 = ema(s1, short_len)
    return s2


def compute_tsi(df, long_len, short_len, signal_len):
    """
    Returns:
        tsi_series, tsi_signal_series
    Matches TradingView's TSI 100% exactly.
    """
    pc = df["close"].diff()
    double_pc = double_smooth(pc, long_len, short_len)
    double_abs = double_smooth(pc.abs(), long_len, short_len)

    tsi = 100 * (double_pc / double_abs)
    tsi_signal = ema(tsi, signal_len)

    return tsi, tsi_signal


def compute_trend_filter(df, ma_length, ma_type, slope_lookback, min_slope):
    """
    Returns:
        trend_ma
        slope_percent
        is_trending_up (boolean Series)
    """

    trend_ma = moving_average(df["close"], ma_length, ma_type)

    slope_percent = 100 * (
        (trend_ma - trend_ma.shift(slope_lookback)) /
        trend_ma.shift(slope_lookback)
    )

    is_up = slope_percent > min_slope

    return trend_ma, slope_percent, is_up


# ---------- COMBINED INDICATOR ENGINE ----------

def compute_indicators(
        df,
        longLen=25,
        shortLen=13,
        signalLen=13,
        maType="EMA",
        maLength=50,
        trendSlopeLen=5,
        minSlope=0.01):

    """
    Returns a dict of all calculated indicators:
        "TSI"
        "TSI_SIGNAL"
        "TREND_MA"
        "SLOPE"
        "IS_TRENDING_UP"
    """

    tsi, tsi_signal = compute_tsi(df, longLen, shortLen, signalLen)

    trend_ma, slope, is_up = compute_trend_filter(
        df,
        maLength,
        maType,
        trendSlopeLen,
        minSlope
    )

    return {
        "TSI": tsi,
        "TSI_SIGNAL": tsi_signal,
        "TREND_MA": trend_ma,
        "SLOPE": slope,
        "IS_UP": is_up
    }




# ================================================================
# SECTION 5 — STRATEGY ENGINE (TSI STRATEGY + TRADE LOGIC) — UPDATED WITH STOP LOSS
# ================================================================

def run_tsi_strategy(
        df,
        longLen=25,
        shortLen=13,
        signalLen=13,
        tp_percent=0.004,     # 0.4% = 0.004
        sl_percent=0.002,     # 0.2% = 0.002  <<< NEW STOP LOSS
        maxTradeDays=7,
        maType="EMA",
        maLength=50,
        trendSlopeLen=5,
        minSlope=0.01):

    """
    FULL STOP LOSS VERSION

    - Adds stop loss exit
    - Adds "reason": "TP" or "SL"
    - Used for ALL backtests, optimizer trials, and strategy apply
    """

    # --- Calculate Indicators ---
    ind = compute_indicators(
        df,
        longLen,
        shortLen,
        signalLen,
        maType,
        maLength,
        trendSlopeLen,
        minSlope
    )

    df["TSI"] = ind["TSI"]
    df["TSI_SIGNAL"] = ind["TSI_SIGNAL"]
    df["TREND_MA"] = ind["TREND_MA"]
    df["SLOPE"] = ind["SLOPE"]
    df["IS_UP"] = ind["IS_UP"]

    trades = []
    in_position = False
    entry_price = None
    last_day = None
    trade_days = 0

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i - 1]

        # Skip until indicators are ready
        if pd.isna(row["TSI"]) or pd.isna(row["TREND_MA"]):
            continue

        # Trend filter
        if not row["IS_UP"]:
            continue

        # TSI crossover entry signal
        longCondition = (row["TSI"] > row["TSI_SIGNAL"]) and \
                        (prev["TSI"] <= prev["TSI_SIGNAL"])

        # Day limiter logic
        d = row.name
        date_key = (d.year, d.month, d.day)

        if longCondition and date_key != last_day:
            trade_days += 1
            last_day = date_key

        if trade_days > maxTradeDays:
            continue

        # ENTRY
        if longCondition and not in_position:
            entry_price = row["close"]
            in_position = True
            trades.append({
                "type": "BUY",
                "time": row.name,
                "price": entry_price
            })

        # EXIT CONDITIONS (TP or SL)
        if in_position:
            tp_price = entry_price * (1 + tp_percent)
            sl_price = entry_price * (1 - sl_percent)

            # TAKE PROFIT
            if row["close"] >= tp_price:
                in_position = False
                trades.append({
                    "type": "SELL",
                    "time": row.name,
                    "price": row["close"],
                    "reason": "TP"
                })

            # STOP LOSS
            elif row["close"] <= sl_price:
                in_position = False
                trades.append({
                    "type": "SELL",
                    "time": row.name,
                    "price": row["close"],
                    "reason": "SL"
                })

    return trades, df


# ================================================================
# SECTION 6 — BACKTESTER (PNL, WIN RATE, MAX DRAWDOWN)
# ================================================================

def compute_backtest_metrics(trades, df):
    """
    Compute:
        - Total PnL
        - Win Rate
        - Max Drawdown
        - Equity Curve
    """

    if not trades:
        return {
            "total_pnl": 0.0,
            "win_rate": 0.0,
            "max_drawdown": 0.0,
            "num_trades": 0
        }

    pnl_list = []
    equity = [1.0]  # start with normalized equity
    wins = 0

    # Pair BUY with SELL
    i = 0
    while i < len(trades):
        t = trades[i]

        if t["type"] == "BUY" and i + 1 < len(trades) and trades[i + 1]["type"] == "SELL":
            entry = t["price"]
            exit_ = trades[i + 1]["price"]

            pnl_pct = (exit_ - entry) / entry
            pnl_list.append(pnl_pct)

            # Win count
            if pnl_pct > 0:
                wins += 1

            # build equity curve
            equity.append(equity[-1] * (1 + pnl_pct))

            i += 2
        else:
            i += 1

    # Total PNL
    total_pnl_pct = (equity[-1] - 1.0) * 100

    # Win Rate
    if len(pnl_list) > 0:
        win_rate = (wins / len(pnl_list)) * 100
    else:
        win_rate = 0.0

    # Max Drawdown
    max_dd = 0
    peak = equity[0]

    for value in equity:
        if value > peak:
            peak = value
        dd = (peak - value) / peak
        if dd > max_dd:
            max_dd = dd

    max_dd_pct = max_dd * 100

    return {
        "total_pnl": total_pnl_pct,
        "win_rate": win_rate,
        "max_drawdown": max_dd_pct,
        "num_trades": len(pnl_list)
    }

def backtest_strategy(df, strategy_params=None):
    """
    Runs the TSI strategy over historical data and returns:
        trades: list of buy/sell dicts
        df: dataframe with indicators
        metrics: pnl, win rate, mdd
    """
    if strategy_params is None:
        strategy_params = {}

    trades, df = run_tsi_strategy(
        df,
        longLen=strategy_params.get("longLen", 25),
        shortLen=strategy_params.get("shortLen", 13),
        signalLen=strategy_params.get("signalLen", 13),
        tp_percent=strategy_params.get("tp_percent", 0.004),
        sl_percent=strategy_params.get("sl_percent", 0.002),   # NEW!
        maxTradeDays=strategy_params.get("maxTradeDays", 7),
        maType=strategy_params.get("maType", "EMA"),
        maLength=strategy_params.get("maLength", 50),
        trendSlopeLen=strategy_params.get("trendSlopeLen", 5),
        minSlope=strategy_params.get("minSlope", 0.01)
    )

    metrics = compute_backtest_metrics(trades, df)

    return trades, df, metrics

# ================================================================
# SECTION 7 — ORDER EXECUTION SIMULATOR
# ================================================================

class OrderSimulator:
    """
    Simulates realistic order execution conditions:
        - Slippage
        - Spread
        - Partial fills
        - Latency
        - Maker/taker fees

    All parameters can be tuned live from a Strategy/Settings panel.
    """

    def __init__(
            self,
            slippage_pct=0.0002,    # 0.02%
            fee_pct=0.0004,         # 0.04% taker fee (Binance spot ~0.1% default)
            partial_fill=True,
            partial_fill_steps=3,
            latency_ms=150):

        self.slippage_pct = slippage_pct
        self.fee_pct = fee_pct
        self.partial_fill = partial_fill
        self.partial_fill_steps = partial_fill_steps
        self.latency_ms = latency_ms

    # ----------------------------------------------
    # Core Fill Logic
    # ----------------------------------------------

    def _apply_slippage(self, price, side):
        """Apply slippage depending on order side."""
        if side == "BUY":
            return price * (1 + self.slippage_pct)
        else:
            return price * (1 - self.slippage_pct)

    def _apply_spread(self, bid, ask, side):
        """Simulate bid/ask spread."""
        if side == "BUY":
            return ask
        else:
            return bid

    def _apply_fee(self, qty, price):
        """Return fee in quote currency."""
        return qty * price * self.fee_pct

    # ----------------------------------------------
    # Main Execution Method
    # ----------------------------------------------

    def execute(
            self,
            side,              # "BUY" or "SELL"
            qty,               # base asset quantity
            bid, ask,          # current orderbook prices
            timestamp=None):

        """
        Returns:
            {
                "time": timestamp,
                "side": "BUY"|"SELL",
                "avg_price": ...,
                "qty_filled": ...,
                "fee_paid": ...,
            }
        """

        timestamp = timestamp or datetime.utcnow()

        # MARKET ORDER — use bid/ask spread
        base_price = self._apply_spread(bid, ask, side)

        # Apply slippage
        slipped_price = self._apply_slippage(base_price, side)

        # Simulate latency
        if self.latency_ms > 0:
            time.sleep(self.latency_ms / 1000)

        # PARTIAL FILLS
        if not self.partial_fill:
            fee = self._apply_fee(qty, slipped_price)
            return {
                "time": timestamp,
                "side": side,
                "avg_price": slipped_price,
                "qty_filled": qty,
                "fee_paid": fee,
            }

        # Partial fill system
        fill_step_qty = qty / self.partial_fill_steps
        total_cost = 0.0
        total_qty = 0.0

        for _ in range(self.partial_fill_steps):
            price = slipped_price  # could simulate movement step-by-step
            fill_qty = fill_step_qty
            cost = price * fill_qty
            total_cost += cost
            total_qty += fill_qty

            # Fill delay per step
            time.sleep(self.latency_ms / (1000 * self.partial_fill_steps))

        avg_price = total_cost / total_qty
        fee = self._apply_fee(total_qty, avg_price)

        return {
            "time": timestamp,
            "side": side,
            "avg_price": avg_price,
            "qty_filled": total_qty,
            "fee_paid": fee,
        }



# ================================================================
# SECTION 8 — CONFIG SAVE / LOAD (JSON SETTINGS SYSTEM) — FIXED
# ================================================================

class ConfigManager:
    """
    JSON-based config storage with automatic key backfilling.
    """

    CONFIG_FILE = "strategy_config.json"

    # Master defaults — ALL required keys go here.
    DEFAULTS = {
        "symbol": DEFAULT_SYMBOL,
        "interval": DEFAULT_INTERVAL,
        "longLen": 25,
        "shortLen": 13,
        "signalLen": 13,
        "tp_percent": 0.004,
        "sl_percent": 0.002,        # << REQUIRED: STOP LOSS
        "maxTradeDays": 7,
        "maType": "EMA",
        "maLength": 50,
        "trendSlopeLen": 5,
        "minSlope": 0.01,
        "slippage_pct": 0.0002,
        "fee_pct": 0.0004,
        "partial_fill": True,
        "partial_fill_steps": 3,
        "latency_ms": 150,
    }

    @staticmethod
    def load():
        """
        Loads config and automatically inserts ANY missing keys.
        Ensures KeyError can NEVER occur.
        """
        try:
            with open(ConfigManager.CONFIG_FILE, "r") as f:
                cfg = json.load(f)
        except:
            # If file missing or corrupted → start fresh
            cfg = {}

        # ---- ALWAYS ENSURE ALL KEYS EXIST ----
        for k, v in ConfigManager.DEFAULTS.items():
            if k not in cfg:
                cfg[k] = v

        # ---- Save repaired config ----
        ConfigManager.save(cfg)

        return cfg

    @staticmethod
    def save(cfg: dict):
        try:
            with open(ConfigManager.CONFIG_FILE, "w") as f:
                json.dump(cfg, f, indent=4)
            return True
        except Exception as e:
            print("Config save error:", e)
            return False






# ================================================================
# SECTION 9 — TRADINGVIEW-STYLE CHART WIDGET (BULLETPROOF LIVE)
# ================================================================

import pyqtgraph as pg
from PySide6.QtWidgets import QWidget, QVBoxLayout
import numpy as np
import pandas as pd

class CandlestickItem(pg.GraphicsObject):
    def __init__(self, data):
        super().__init__()
        self.data = data  # numpy array: [i, open, close, low, high]
        self.generate_picture()

    def generate_picture(self):
        self.picture = pg.QtGui.QPicture()
        p = pg.QtGui.QPainter(self.picture)
        w = 0.35
        for (i, open_, close_, low_, high_) in self.data:
            pen = pg.mkPen('#00ff00') if close_ >= open_ else pg.mkPen('#ff0000')
            brush = pg.mkBrush('#00ff00') if close_ >= open_ else pg.mkBrush('#ff0000')
            p.setPen(pen)
            p.setBrush(brush)
            p.drawLine(pg.QtCore.QPointF(i, low_), pg.QtCore.QPointF(i, high_))
            p.drawRect(pg.QtCore.QRectF(i - w, open_, w * 2, close_ - open_))
        p.end()

    def paint(self, p, *args):
        p.drawPicture(0, 0, self.picture)
    def boundingRect(self):
        return self.picture.boundingRect()

class ChartWidget(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.plot = pg.PlotWidget()
        self.plot.setBackground('#111')
        self.plot.showGrid(x=True, y=True)
        layout.addWidget(self.plot)
        self.df = None
        self.candle_item = None
        self.indicator_items = []
        self.trade_markers = []
        self.autoscroll = True

    def plot_dataframe(self, df):
        # Only plot candles, do not overwrite indicator columns
        self.df = df.copy()
        arr = np.column_stack([
            np.arange(len(df)),
            df["open"].values,
            df["close"].values,
            df["low"].values,
            df["high"].values
        ])
        if self.candle_item:
            self.plot.removeItem(self.candle_item)
        self.candle_item = CandlestickItem(arr)
        self.plot.addItem(self.candle_item)
        self.plot.enableAutoRange(axis='x', enable=True)
        self.plot.enableAutoRange(axis='y', enable=True)

    def update_live_candle(self, candle):
        if self.df is None or len(self.df) == 0:
            return
        # --------- TIMESTAMP ALIGNMENT FIX ----------
        ts = candle["time"]
        if not isinstance(ts, pd.Timestamp):
            ts = pd.to_datetime(ts)
        # Floor to minute for 1m, or floor to interval as needed!
        ts = ts.replace(second=0, microsecond=0)
        o = float(candle["open"])
        h = float(candle["high"])
        l = float(candle["low"])
        c = float(candle["close"])
        v = float(candle["volume"])
        # Only update OHLCV cols for this row!
        raw_cols = ["open", "high", "low", "close", "volume"]
        if ts in self.df.index:
            self.df.loc[ts, raw_cols] = [o, h, l, c, v]
        else:
            # Append new row without killing indicator columns!
            extra_cols = [col for col in self.df.columns if col not in raw_cols]
            new_row = dict(zip(raw_cols, [o, h, l, c, v]))
            for col in extra_cols:
                new_row[col] = np.nan
            self.df.loc[ts] = new_row
            self.df = self.df.sort_index()
        # Print for debugging:
        # print(self.df.tail(5))
        arr = np.column_stack([
            np.arange(len(self.df)),
            self.df["open"].values,
            self.df["close"].values,
            self.df["low"].values,
            self.df["high"].values
        ])
        self.candle_item.data = arr
        self.candle_item.generate_picture()
        if self.autoscroll:
            self.plot.enableAutoRange(axis='x', enable=True)
            self.plot.enableAutoRange(axis='y', enable=True)

    def plot_indicator(self, series, color="#ffaa00", width=2):
        if series is None or len(series) == 0:
            return
        x = np.arange(len(series))
        y = series.values
        item = self.plot.plot(x, y, pen=pg.mkPen(color, width=width))
        self.indicator_items.append(item)

    def clear_indicators(self):
        for item in self.indicator_items:
            try:
                self.plot.removeItem(item)
            except:
                pass
        self.indicator_items = []

    def plot_trades(self, trades):
        for m in self.trade_markers:
            try:
                self.plot.removeItem(m)
            except:
                pass
        self.trade_markers = []
        if not trades:
            return
        for t in trades:
            idx = self.df.index.get_loc(t["time"])
            price = t["price"]
            if t["type"] == "BUY":
                marker = self.plot.plot(
                    [idx], [price],
                    pen=None,
                    symbol="t1",
                    symbolSize=12,
                    symbolBrush="#00ff00"
                )
            else:
                marker = self.plot.plot(
                    [idx], [price],
                    pen=None,
                    symbol="t",
                    symbolSize=12,
                    symbolBrush="#ff0000"
                )
            self.trade_markers.append(marker)


# ================================================================
# SECTION 10 — TSI INDICATOR PANEL (SUBPANEL UNDER PRICE CHART)
# ================================================================

class IndicatorPanel(QWidget):
    """
    A TradingView-style indicator panel.
    Default: TSI indicator panel.
    """

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.plot = pg.PlotWidget()
        self.plot.setBackground('#111111')
        self.plot.showGrid(x=True, y=True)
        layout.addWidget(self.plot)

        self.tsi_item = None
        self.signal_item = None
        self.zero_line = None

        self.df = None

    # -------------------------------------------------------------
    # DRAW TSI FOR ENTIRE HISTORY
    # -------------------------------------------------------------
    def plot_tsi(self, df):
        """Draw entire TSI history."""
        self.df = df.copy()

        tsi = df["TSI"]
        sig = df["TSI_SIGNAL"]

        if self.tsi_item:
            self.plot.removeItem(self.tsi_item)
        if self.signal_item:
            self.plot.removeItem(self.signal_item)
        if self.zero_line:
            self.plot.removeItem(self.zero_line)

        x = np.arange(len(df))

        self.tsi_item = self.plot.plot(
            x, tsi.values,
            pen=pg.mkPen('#2962FF', width=2)
        )

        self.signal_item = self.plot.plot(
            x, sig.values,
            pen=pg.mkPen('#E91E63', width=2)
        )

        self.zero_line = self.plot.addLine(
            y=0,
            pen=pg.mkPen('#787B86', width=1)
        )

        self.plot.enableAutoRange()

    # -------------------------------------------------------------
    # LIVE TSI UPDATE FROM WEBSOCKET
    # -------------------------------------------------------------
    def update_live_tsi(self, indicators_dict):
        """
        Update the most recent TSI point.
        Called from MainWindow after candle update.
        """
        if self.df is None:
            return

        tsi = indicators_dict["TSI"]
        sig = indicators_dict["TSI_SIGNAL"]

        # update the df
        self.df.iloc[-1, self.df.columns.get_loc("TSI")] = tsi
        self.df.iloc[-1, self.df.columns.get_loc("TSI_SIGNAL")] = sig

        x = np.arange(len(self.df))

        # Update plot data
        if self.tsi_item:
            self.tsi_item.setData(x, self.df["TSI"].values)

        if self.signal_item:
            self.signal_item.setData(x, self.df["TSI_SIGNAL"].values)




# ================================================================
# SECTION 11 — STRATEGY EDITOR PANEL (USER CONFIG CONTROLS, STOP LOSS, FIXED ORDER)
# ================================================================

class StrategyEditor(QWidget):
    """
    A panel for editing strategy parameters.
    Loads config from ConfigManager, saves on change.
    Emits signals when user requests backtest or strategy update.
    """

    backtest_requested = Signal()
    apply_strategy_requested = Signal(dict)

    # ---------- SAVE CONFIG (must be defined before __init__) ----------
    def _save(self):
        self.cfg["symbol"] = self.symbol_box.text().upper()
        self.cfg["interval"] = self.interval_box.text()
        self.cfg["maType"] = self.ma_type_box.currentText()

        self.cfg["longLen"] = int(float(self.longLen_box.text()))
        self.cfg["shortLen"] = int(float(self.shortLen_box.text()))
        self.cfg["signalLen"] = int(float(self.signalLen_box.text()))

        # Convert TP % from "0.4" to 0.004
        tp_val = float(self.tp_box.text())
        if tp_val > 1:
            tp_val = tp_val / 100
        self.cfg["tp_percent"] = tp_val

        # Convert SL % from "0.2" to 0.002
        sl_val = float(self.sl_box.text())
        if sl_val > 1:
            sl_val = sl_val / 100
        self.cfg["sl_percent"] = sl_val

        self.cfg["maxTradeDays"] = int(float(self.days_box.text()))
        self.cfg["maLength"] = int(float(self.maLen_box.text()))
        self.cfg["trendSlopeLen"] = int(float(self.slopeLen_box.text()))
        self.cfg["minSlope"] = float(self.minSlope_box.text())

        # Execution settings
        self.cfg["slippage_pct"] = float(self.slip_box.text())
        self.cfg["fee_pct"] = float(self.fee_box.text())
        self.cfg["partial_fill_steps"] = int(float(self.partial_steps_box.text()))
        self.cfg["latency_ms"] = int(float(self.latency_box.text()))

        ConfigManager.save(self.cfg)

    # ---------- INIT UI ----------
    def __init__(self):
        super().__init__()

        self.cfg = ConfigManager.load()

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        # --- SYMBOL ---
        layout.addWidget(QLabel("Symbol:"))
        self.symbol_box = QLineEdit(self.cfg.get("symbol", DEFAULT_SYMBOL))
        layout.addWidget(self.symbol_box)
        self.symbol_box.editingFinished.connect(self._save)

        # --- INTERVAL ---
        layout.addWidget(QLabel("Interval:"))
        self.interval_box = QLineEdit(self.cfg.get("interval", DEFAULT_INTERVAL))
        layout.addWidget(self.interval_box)
        self.interval_box.editingFinished.connect(self._save)

        # --- MA TYPE ---
        layout.addWidget(QLabel("MA Type:"))
        self.ma_type_box = QComboBox()
        self.ma_type_box.addItems(["EMA", "SMA", "WMA"])
        self.ma_type_box.setCurrentText(self.cfg.get("maType", "EMA"))
        layout.addWidget(self.ma_type_box)
        self.ma_type_box.currentIndexChanged.connect(self._save)

        # --- NUMERIC INPUT HELPERS ---
        def num_input(label, key):
            layout.addWidget(QLabel(label))
            box = QLineEdit(str(self.cfg.get(key)))
            layout.addWidget(box)
            box.editingFinished.connect(self._save)
            return box

        # Strategy parameters
        self.longLen_box = num_input("TSI Long Length:", "longLen")
        self.shortLen_box = num_input("TSI Short Length:", "shortLen")
        self.signalLen_box = num_input("TSI Signal Length:", "signalLen")
        self.tp_box = num_input("Take Profit (%):", "tp_percent")
        self.sl_box = num_input("Stop Loss (%):", "sl_percent")    # <<< NEW FIELD
        self.days_box = num_input("Max Trade Days:", "maxTradeDays")
        self.maLen_box = num_input("Trend MA Length:", "maLength")
        self.slopeLen_box = num_input("Slope Lookback Bars:", "trendSlopeLen")
        self.minSlope_box = num_input("Min Slope (%):", "minSlope")

        # Execution Parameters
        layout.addWidget(QLabel("Slippage % (0.0002 = 0.02%):"))
        self.slip_box = QLineEdit(str(self.cfg.get("slippage_pct")))
        layout.addWidget(self.slip_box)
        self.slip_box.editingFinished.connect(self._save)

        layout.addWidget(QLabel("Fee % (0.0004 = 0.04%):"))
        self.fee_box = QLineEdit(str(self.cfg.get("fee_pct")))
        layout.addWidget(self.fee_box)
        self.fee_box.editingFinished.connect(self._save)

        layout.addWidget(QLabel("Partial Fill Steps:"))
        self.partial_steps_box = QLineEdit(str(self.cfg.get("partial_fill_steps")))
        layout.addWidget(self.partial_steps_box)
        self.partial_steps_box.editingFinished.connect(self._save)

        layout.addWidget(QLabel("Latency (ms):"))
        self.latency_box = QLineEdit(str(self.cfg.get("latency_ms")))
        layout.addWidget(self.latency_box)
        self.latency_box.editingFinished.connect(self._save)

        # Buttons
        self.btn_backtest = QPushButton("Run Backtest")
        self.btn_apply = QPushButton("Apply Strategy")
        layout.addWidget(self.btn_backtest)
        layout.addWidget(self.btn_apply)

        # Emit signals
        self.btn_backtest.clicked.connect(lambda: self.backtest_requested.emit())
        self.btn_apply.clicked.connect(self._emit_apply)

    # ---------- EXTRACT CURRENT STRATEGY PARAM SET ----------
    def get_params(self):
        return self.cfg.copy()

    # ---------- SIGNAL EMITTER ----------
    def _emit_apply(self):
        """Emits the strategy parameters to MainWindow."""
        self._save()
        self.apply_strategy_requested.emit(self.get_params())







# ================================================================
# SECTION 12 — SYMBOL SELECTOR PANEL (MARKET LIST + SEARCH)
# ================================================================

class SymbolSelector(QWidget):
    """
    A TradingView-style symbol selector:
        ✔ Shows list of USDT pairs
        ✔ Search/filter bar
        ✔ Click symbol → chart reload & websocket restart
    """

    symbol_selected = Signal(str)

    def __init__(self):
        super().__init__()

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        # --- Search bar ---
        layout.addWidget(QLabel("Search Symbol:"))
        self.search_box = QLineEdit()
        layout.addWidget(self.search_box)

        # Live filtering
        self.search_box.textChanged.connect(self._filter_symbols)

        # --- Symbol list (table) ---
        self.table = QTableWidget()
        self.table.setColumnCount(1)
        self.table.setHorizontalHeaderLabels(["Symbol"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        # Load symbols
        self.all_symbols = fetch_all_symbols()
        self._populate(self.all_symbols)

        # Click handler
        self.table.cellClicked.connect(self._clicked)

    # -------------------------------------------------------------
    # Populate table
    # -------------------------------------------------------------
    def _populate(self, symbols):
        self.table.setRowCount(len(symbols))
        for i, sym in enumerate(symbols):
            self.table.setItem(i, 0, QTableWidgetItem(sym))

    # -------------------------------------------------------------
    # Filter list
    # -------------------------------------------------------------
    def _filter_symbols(self, text):
        text = text.upper().strip()
        filtered = [s for s in self.all_symbols if text in s]
        self._populate(filtered)

    # -------------------------------------------------------------
    # Row clicked
    # -------------------------------------------------------------
    def _clicked(self, row, col):
        symbol = self.table.item(row, 0).text()
        self.symbol_selected.emit(symbol)







# ================================================================
# SECTION 13 — BACKTEST RESULTS PANEL + PARAMETER GENERATOR
# ================================================================

import random

class BacktestResults(QWidget):
    """
    Displays:
        ✔ Total PnL
        ✔ Win Rate
        ✔ Max Drawdown
        ✔ Number of Trades
        ✔ Trade List (table)
    """

    def __init__(self):
        super().__init__()

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        # --- METRIC LABELS ---
        self.pnl_label = QLabel("Total PnL: -")
        self.winrate_label = QLabel("Win Rate: -")
        self.mdd_label = QLabel("Max Drawdown: -")
        self.trades_label = QLabel("Trades: -")

        for lbl in [self.pnl_label, self.winrate_label, self.mdd_label, self.trades_label]:
            lbl.setStyleSheet("font-size: 14px; color: white;")
            layout.addWidget(lbl)

        # --- TRADE TABLE ---
        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Type", "Time", "Price", "PNL %"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

    # -------------------------------------------------------------
    # UPDATE PANEL WITH NEW RESULTS
    # -------------------------------------------------------------
    def update_results(self, metrics, trades):
        pnl = metrics["total_pnl"]
        winrate = metrics["win_rate"]
        mdd = metrics["max_drawdown"]
        num = metrics["num_trades"]

        self.pnl_label.setText(f"Total PnL: {pnl:.2f}%")
        self.winrate_label.setText(f"Win Rate: {winrate:.2f}%")
        self.mdd_label.setText(f"Max Drawdown: {mdd:.2f}%")
        self.trades_label.setText(f"Trades: {num}")

        rows = []
        for t in trades:
            if t["type"] == "BUY":
                rows.append([t["type"], t["time"], t["price"], ""])
            else:
                for r in reversed(rows):
                    if r[0] == "BUY" and r[3] == "":
                        entry = r[2]
                        exit_ = t["price"]
                        pnl_pct = ((exit_ - entry) / entry) * 100
                        r[3] = f"{pnl_pct:.2f}%"
                rows.append([t["type"], t["time"], t["price"], ""])

        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for j in range(4):
                self.table.setItem(i, j, QTableWidgetItem(str(r[j])))


# ================================================================
# RANDOM PARAMETER GENERATOR
# ================================================================

def random_params():
    """Return a single randomized strategy parameter set."""
    return {
        "longLen": random.randint(5, 60),
        "shortLen": random.randint(3, 40),
        "signalLen": random.randint(5, 30),
        "tp_percent": random.uniform(0.001, 0.02),   # 0.1% → 2%
        "sl_percent": random.uniform(0.001, 0.01),   # 0.1% → 1%  # NEW!
        "maxTradeDays": random.randint(1, 10),
        "maType": random.choice(["EMA", "SMA", "WMA"]),
        "maLength": random.randint(10, 300),
        "trendSlopeLen": random.randint(2, 20),
        "minSlope": random.uniform(0.001, 0.1),
    }


# ================================================================
# SECTION 16 — LIVE PAPER TRADER (NEW, WITH STOP LOSS SUPPORT)
# ================================================================

class LivePaperTrader:
    def __init__(self, params):
        self.position = None
        self.entry = None
        self.trades = []
        self.params = params.copy()  # Always up-to-date param set

    def update_params(self, params):
        self.params = params.copy()

    def process_live_tick(self, df):
        if len(df) < 3:
            return

        row = df.iloc[-1]
        prev = df.iloc[-2]

        # TSI crossover
        long_signal = (row["TSI"] > row["TSI_SIGNAL"]) and \
                      (prev["TSI"] <= prev["TSI_SIGNAL"])

        tp = self.params.get("tp_percent", 0.004)
        sl = self.params.get("sl_percent", 0.002)

        if self.position is None and long_signal:
            # BUY
            self.position = "LONG"
            self.entry = row["close"]
            self.trades.append({
                "type": "BUY",
                "time": row.name,
                "price": self.entry
            })
            print("[PAPER] BUY", self.entry)

        elif self.position == "LONG":
            tp_price = self.entry * (1 + tp)
            sl_price = self.entry * (1 - sl)
            if row["close"] >= tp_price:
                self.position = None
                self.trades.append({
                    "type": "SELL",
                    "time": row.name,
                    "price": row["close"],
                    "reason": "TP"
                })
                print("[PAPER] SELL (TP)", row["close"])
            elif row["close"] <= sl_price:
                self.position = None
                self.trades.append({
                    "type": "SELL",
                    "time": row.name,
                    "price": row["close"],
                    "reason": "SL"
                })
                print("[PAPER] SELL (SL)", row["close"])









# ================================================================
# GLOBAL MULTIPROCESS WORKER (MUST BE TOP-LEVEL FOR PICKLING)
# ================================================================
def optimizer_trial(args):
    df, _ = args  # second arg unused placeholder
    params = random_params()
    trades, outdf, metrics = backtest_strategy(df.copy(), params)
    return params, trades, metrics


# ================================================================
# SECTION 14 — MAIN WINDOW (FULL WITH AUTO-APPLY OPTIMIZER)
# ================================================================

import os
import concurrent.futures
from PySide6.QtCore import QThread, QObject, Signal


# ================================================================
# OPTIMIZER WORKER THREAD
# ================================================================
class OptimizerWorker(QObject):
    finished = Signal(dict, list, dict)   # (best_params, best_trades, best_metrics)
    progress = Signal(int)

    def __init__(self, df, trials=10000):
        super().__init__()
        self.df = df
        self.trials = trials
        self._abort = False

    def stop(self):
        self._abort = True

    def run(self):
        max_workers = max(1, int(os.cpu_count() * 0.9))
        print(f"[OPTIMIZER] Using {max_workers}/{os.cpu_count()} CPU cores")

        best_params = None
        best_metrics = None
        best_trades = None

        args_list = [(self.df, i) for i in range(self.trials)]

        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(optimizer_trial, args) for args in args_list]

            for i, f in enumerate(concurrent.futures.as_completed(futures), start=1):
                if self._abort:
                    print("[OPTIMIZER] Aborted.")
                    return

                params, trades, metrics = f.result()

                if best_metrics is None or metrics["total_pnl"] > best_metrics["total_pnl"]:
                    best_metrics = metrics
                    best_params = params
                    best_trades = trades

                if i % 200 == 0:
                    print(f"[OPTIMIZER] Completed {i}/{self.trials}")
                    self.progress.emit(i)

        self.finished.emit(best_params, best_trades, best_metrics)


# ================================================================
# MAIN WINDOW
# ================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("TradingView-Lite Terminal")
        self.resize(1800, 1000)

        self.cfg = ConfigManager.load()

        central = QWidget()
        main_layout = QVBoxLayout(central)
        self.setCentralWidget(central)

        chart_stack = QVBoxLayout()
        self.chart = ChartWidget()
        self.indicator_panel = IndicatorPanel()
        chart_stack.addWidget(self.chart, stretch=3)
        chart_stack.addWidget(self.indicator_panel, stretch=1)

        chart_container = QWidget()
        chart_container.setLayout(chart_stack)
        main_layout.addWidget(chart_container)

        self.results_panel = BacktestResults()
        results_dock = QDockWidget("Backtest Results", self)
        results_dock.setWidget(self.results_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, results_dock)

        self.symbol_panel = SymbolSelector()
        symbol_dock = QDockWidget("Market Symbols", self)
        symbol_dock.setWidget(self.symbol_panel)
        self.addDockWidget(Qt.LeftDockWidgetArea, symbol_dock)

        self.strategy_panel = StrategyEditor()
        strategy_dock = QDockWidget("Strategy Editor", self)
        strategy_dock.setWidget(self.strategy_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, strategy_dock)

        self.symbol_panel.symbol_selected.connect(self.on_symbol_changed)
        self.strategy_panel.backtest_requested.connect(self.run_backtest)
        self.strategy_panel.apply_strategy_requested.connect(self.apply_strategy)

        self.stream = None
        self.df = None

        self.load_symbol(self.cfg["symbol"], self.cfg["interval"])

    # ============================================================
    # PARAM EXTRACTOR
    # ============================================================
    def extract_strategy_params(self):
        return {
            "longLen": self.cfg["longLen"],
            "shortLen": self.cfg["shortLen"],
            "signalLen": self.cfg["signalLen"],
            "tp_percent": self.cfg["tp_percent"],
            "sl_percent": self.cfg["sl_percent"],      # NEW!
            "maxTradeDays": self.cfg["maxTradeDays"],
            "maType": self.cfg["maType"],
            "maLength": self.cfg["maLength"],
            "trendSlopeLen": self.cfg["trendSlopeLen"],
            "minSlope": self.cfg["minSlope"]
        }






    # ============================================================
    # LOAD SYMBOL
    # ============================================================
    def load_symbol(self, symbol, interval):
        print(f"[LOAD] {symbol} {interval}")

        df = fetch_historical_klines(symbol, interval)

        df = df.astype({
            "open": float, "high": float, "low": float,
            "close": float, "volume": float
        })

        _, df = run_tsi_strategy(df, **self.extract_strategy_params())
        self.df = df

        self.chart.plot_dataframe(df)
        self.indicator_panel.plot_tsi(df)
        self.start_stream(symbol, interval)

    # ============================================================
    # APPLY STRATEGY (MANUAL BUTTON)
    # ============================================================
    def apply_strategy(self, params):
        self.cfg = params
        ConfigManager.save(self.cfg)

        trades, df = run_tsi_strategy(self.df, **self.extract_strategy_params())
        self.df = df

        self.chart.clear_indicators()
        self.chart.plot_indicator(df["TREND_MA"], "#ffaa00")
        self.indicator_panel.plot_tsi(df)
        self.chart.plot_trades(trades)

    # ============================================================
    # SYMBOL SWITCH (LEFT PANEL)
    # ============================================================
    def on_symbol_changed(self, symbol):
        self.cfg["symbol"] = symbol
        ConfigManager.save(self.cfg)
        self.load_symbol(symbol, self.cfg["interval"])

    # ============================================================
    # RUN OPTIMIZER (BACKTEST BUTTON)
    # ============================================================
    def run_backtest(self):
        print("\n[OPTIMIZER] Starting multiprocessing optimizer...")

        self.opt_thread = QThread()
        self.opt_worker = OptimizerWorker(self.df.copy(), trials=10000)
        self.opt_worker.moveToThread(self.opt_thread)

        self.opt_thread.started.connect(self.opt_worker.run)
        self.opt_worker.finished.connect(self.optimizer_finished)
        self.opt_worker.progress.connect(self.optimizer_progress)

        self.opt_worker.finished.connect(self.opt_thread.quit)
        self.opt_worker.finished.connect(self.opt_worker.deleteLater)
        self.opt_thread.finished.connect(self.opt_thread.deleteLater)

        self.opt_thread.start()

    # ============================================================
    # OPTIMIZER PROGRESS
    # ============================================================
    def optimizer_progress(self, n):
        print(f"[OPTIMIZER] Completed {n}/10000")

    # ============================================================
    # OPTIMIZER FINISHED — AUTO-APPLY & MAKE DEFAULTS
    # ============================================================
    def optimizer_finished(self, params, trades, metrics):
        print("[OPTIMIZER] COMPLETE")
        print("Best Params:", params)
        print(f"Best PnL: {metrics['total_pnl']:.2f}%")

        # 1 — Update config in RAM
        self.cfg.update(params)

        # 2 — Save to JSON permanently (new defaults)
        ConfigManager.save(self.cfg)

        # 3 — FULL AUTO-APPLY TO LIVE CHART
        print("[OPTIMIZER] Applying optimized parameters...")
        self.apply_strategy(self.cfg)

        # 4 — Update results panel + trade markers
        self.results_panel.update_results(metrics, trades)
        self.chart.plot_trades(trades)

        print("[OPTIMIZER] Optimized parameters now ACTIVE + DEFAULT.")

    # ============================================================
    # START WEBSOCKET
    # ============================================================
    def start_stream(self, symbol, interval):
        if self.stream:
            self.stream.stop()

        self.stream = CandleStream(symbol, interval)
        self.stream.new_candle.connect(self.on_live_candle)
        self.stream.start()

    # ============================================================
    # LIVE CANDLE HANDLER
    # ============================================================
    def on_live_candle(self, candle):
        ts = pd.to_datetime(candle["time"]).replace(second=0, microsecond=0)
        o = float(candle["open"])
        h = float(candle["high"])
        l = float(candle["low"])
        c_ = float(candle["close"])
        v = float(candle["volume"])

        raw = ["open", "high", "low", "close", "volume"]

        if ts in self.df.index:
            self.df.loc[ts, raw] = [o, h, l, c_, v]
        else:
            new_row = {col: np.nan for col in self.df.columns}
            for col, val in zip(raw, [o, h, l, c_, v]):
                new_row[col] = val
            self.df.loc[ts] = new_row
            self.df = self.df.sort_index()

        lookback = max(100, self.cfg["maLength"] * 2)
        start = max(len(self.df) - lookback, 0)

        recalc_df = self.df.iloc[start:].copy()

        ind = compute_indicators(
            recalc_df,
            self.cfg["longLen"],
            self.cfg["shortLen"],
            self.cfg["signalLen"],
            self.cfg["maType"],
            self.cfg["maLength"],
            self.cfg["trendSlopeLen"],
            self.cfg["minSlope"]
        )

        for key in ind:
            self.df.iloc[start:, self.df.columns.get_loc(key)] = ind[key].values

        plot_df = self.df.dropna(subset=[
            "open", "high", "low", "close", "volume",
            "TSI", "TSI_SIGNAL"
        ])

        self.chart.plot_dataframe(plot_df)
        self.chart.update_live_candle({
            "time": ts, "open": o, "high": h, "low": l,
            "close": c_, "volume": v,
            "is_closed": candle.get("is_closed", True)
        })

        if len(plot_df) > 0:
            self.indicator_panel.update_live_tsi({
                "TSI": plot_df["TSI"].iloc[-1],
                "TSI_SIGNAL": plot_df["TSI_SIGNAL"].iloc[-1]
            })







# ================================================================
# SECTION 15 — APPLICATION ENTRY POINT
# ================================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())
