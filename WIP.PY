# ================================================================
# PART 1 — IMPORTS, GLOBALS, REST FETCH, WEBSOCKET, INDICATORS, STRATEGY
# ================================================================

import sys, os, json, time, threading, random, hmac, hashlib, math
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import psutil

import requests
import websocket

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QDockWidget, QPushButton, QComboBox, QLineEdit,
    QTableWidget, QTableWidgetItem, QFileDialog, QSplitter, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer, Signal, QObject, QThread
from PySide6.QtGui import QPalette, QColor

from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.dates as mdates

# Disable SSL warnings (not recommended for production)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ================================================================
# GLOBAL CONSTANTS
# ================================================================

BINANCE_REST = "https://api.binance.com"
DEFAULT_SYMBOL = "SOLUSDT"
DEFAULT_INTERVAL = "15m"
EMA = "EMA"
SMA = "SMA"
WMA = "WMA"

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

APP_VERSION = "TSI Trading App v1.0"

# ================================================================
# SAFE REST JSON FETCH
# ================================================================

def safe_get_json(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("[REST ERROR]", e)
        return None

def fetch_all_symbols():
    url = BINANCE_REST + "/api/v3/exchangeInfo"
    data = safe_get_json(url)
    if not data:
        return ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    symbols = [s["symbol"] for s in data.get("symbols", []) if s.get("status")=="TRADING" and s.get("quoteAsset")=="USDT"]
    symbols.sort()
    return symbols

def fetch_historical_klines(symbol=DEFAULT_SYMBOL, interval=DEFAULT_INTERVAL, days=30):
    url = BINANCE_REST + "/api/v3/klines"
    params = {"symbol": symbol.upper(), "interval": interval, "limit": 1500}
    raw = safe_get_json(url, params)
    if not raw:
        raise RuntimeError("Failed to fetch klines")
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "qav", "num_trades", "tbbav", "tbqav", "ignore"
    ])
    for col in ["open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("time", inplace=True)
    return df[["open","high","low","close","volume"]]

# ================================================================
# WEBSOCKET CANDLE STREAM
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
                k = json.loads(msg).get("k",{})
                candle = {"time": pd.to_datetime(k["t"], unit="ms"),
                          "open": float(k["o"]),
                          "high": float(k["h"]),
                          "low": float(k["l"]),
                          "close": float(k["c"]),
                          "volume": float(k["v"]),
                          "is_closed": bool(k["x"])}
                self.new_candle.emit(candle)
            except Exception as e:
                print("[WS ERROR]", e)

        def on_error(ws, err): print("[WS ERROR]", err)
        def on_close(ws,a,b):
            print("[WS] Closed — reconnecting...")
            if self.running: time.sleep(2); self.start()
        def on_open(ws): print(f"[WS] Connected → {self.symbol.upper()} {self.interval}")

        self.ws = websocket.WebSocketApp(url,
                                         on_open=on_open,
                                         on_error=on_error,
                                         on_close=on_close,
                                         on_message=on_message)
        self.ws.run_forever(ping_interval=15, ping_timeout=5)

    def start(self):
        if self.running: return
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.ws:
            try: self.ws.close()
            except: pass

# ================================================================
# INDICATORS & TSI STRATEGY
# ================================================================

def ema(series, length): return series.ewm(span=length, adjust=False).mean()
def sma(series, length): return series.rolling(length).mean()
def wma(series, length):
    w = np.arange(1,length+1)
    return series.rolling(length).apply(lambda x: np.dot(x,w)/w.sum(), raw=True)

def moving_average(series, length, ma_type):
    ma_type = ma_type.upper()
    if ma_type=="EMA": return ema(series,length)
    if ma_type=="SMA": return sma(series,length)
    if ma_type=="WMA": return wma(series,length)
    raise ValueError(f"Unknown MA type: {ma_type}")

def double_smooth(series,long_len,short_len): return ema(ema(series,long_len), short_len)
def compute_tsi(df,long_len,short_len,signal_len):
    pc = df["close"].diff()
    double_pc = double_smooth(pc,long_len,short_len)
    double_abs = double_smooth(pc.abs(),long_len,short_len)
    tsi = 100*(double_pc/double_abs)
    tsi_signal = ema(tsi, signal_len)
    return tsi, tsi_signal

def compute_trend_filter(df, ma_length, ma_type, slope_lookback, min_slope):
    trend_ma = moving_average(df["close"], ma_length, ma_type)
    slope_pct = 100*(trend_ma - trend_ma.shift(slope_lookback))/trend_ma.shift(slope_lookback)
    is_up = slope_pct>min_slope
    return trend_ma, slope_pct, is_up

def compute_indicators(df, longLen=25, shortLen=13, signalLen=13,
                       maType="EMA", maLength=50, trendSlopeLen=5, minSlope=0.01):
    tsi, tsi_signal = compute_tsi(df, longLen, shortLen, signalLen)
    trend_ma, slope, is_up = compute_trend_filter(df, maLength, maType, trendSlopeLen, minSlope)
    return {"TSI": tsi, "TSI_SIGNAL": tsi_signal, "TREND_MA": trend_ma, "SLOPE": slope, "IS_UP": is_up}

def run_tsi_strategy(df,
                     longLen=25, shortLen=13, signalLen=13,
                     tp_percent=0.004, sl_percent=0.002,
                     maxTradeDays=7,
                     maType="EMA", maLength=50,
                     trendSlopeLen=5, minSlope=0.01):
    ind = compute_indicators(df,longLen,shortLen,signalLen,maType,maLength,trendSlopeLen,minSlope)
    for key in ind: df[key] = ind[key]
    trades = []; in_position=False; entry_price=None; last_day=None; trade_days=0
    for i in range(1,len(df)):
        row=df.iloc[i]; prev=df.iloc[i-1]
        if pd.isna(row["TSI"]) or pd.isna(row["TREND_MA"]): continue
        if not row["IS_UP"]: continue
        longCond = row["TSI"]>row["TSI_SIGNAL"] and prev["TSI"]<=prev["TSI_SIGNAL"]
        date_key=(row.name.year,row.name.month,row.name.day)
        if longCond and date_key!=last_day: trade_days+=1; last_day=date_key
        if trade_days>maxTradeDays: continue
        if longCond and not in_position:
            entry_price=row["close"]; in_position=True
            trades.append({"type":"BUY","time":row.name,"price":entry_price})
        if in_position:
            tp_price=entry_price*(1+tp_percent)
            sl_price=entry_price*(1-sl_percent)
            if row["close"]>=tp_price: in_position=False; trades.append({"type":"SELL","time":row.name,"price":row["close"],"reason":"TP"})
            elif row["close"]<=sl_price: in_position=False; trades.append({"type":"SELL","time":row.name,"price":row["close"],"reason":"SL"})
    return trades, df

# ------------------------------------------------------------
# Placeholder for Binance order sending
# ------------------------------------------------------------
def binance_send_order(symbol, side, quantity, test=True):
    if test: print(f"[TEST ORDER] {side} {quantity} {symbol}")
    else: print(f"[REAL ORDER] {side} {quantity} {symbol}")

# ================================================================
# PART 2 — CONFIG MANAGER, STRATEGY EDITOR, SYMBOL SELECTOR, CHART WIDGETS
# ================================================================

# ================================================================
# CONFIG MANAGER
# ================================================================
class ConfigManager:
    """Handles loading/saving strategy configs and defaults."""

    def __init__(self, path="config.json"):
        self.path = path
        self.cfg = self.load_config()

    def load_config(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return self.default_config()

    def save_config(self, cfg=None):
        cfg = cfg or self.cfg
        with open(self.path, "w") as f:
            json.dump(cfg, f, indent=4)

    def default_config(self):
        return {
            "symbol": DEFAULT_SYMBOL,
            "interval": DEFAULT_INTERVAL,
            "ma_type": "EMA",
            "tsi_long_len": 47,
            "tsi_short_len": 17,
            "tsi_signal_len": 14,
            "take_profit_pct": 0.008,
            "stop_loss_pct": 0.0058,
            "max_trade_days": 7,
            "trend_ma_len": 14,
            "slope_lookback": 2,
            "min_slope_pct": 0.0119,
            "live_qty": 10
        }


# ================================================================
# STRATEGY EDITOR PANEL
# ================================================================
class StrategyEditor(QWidget):
    """Left panel with TSI strategy parameters."""

    config_changed = Signal(dict)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        # Inputs
        self.inputs = {}
        for label, key in [
            ("Symbol:", "symbol"),
            ("Interval:", "interval"),
            ("MA Type:", "ma_type"),
            ("TSI Long Length:", "tsi_long_len"),
            ("TSI Short Length:", "tsi_short_len"),
            ("TSI Signal Length:", "tsi_signal_len"),
            ("Take Profit (%):", "take_profit_pct"),
            ("Stop Loss (%):", "stop_loss_pct"),
            ("Max Trade Days:", "max_trade_days"),
            ("Trend MA Length:", "trend_ma_len"),
            ("Slope Lookback Bars:", "slope_lookback"),
            ("Min Slope (%):", "min_slope_pct"),
            ("Live Trade Qty (base or $):", "live_qty")
        ]:
            hbox = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setFixedWidth(160)
            edit = QLineEdit(str(self.cfg.get(key, "")))
            edit.setFixedWidth(120)
            edit.editingFinished.connect(lambda k=key, e=edit: self.update_cfg(k, e))
            hbox.addWidget(lbl)
            hbox.addWidget(edit)
            layout.addLayout(hbox)
            self.inputs[key] = edit

        layout.addStretch(1)

    def update_cfg(self, key, edit):
        val = edit.text()
        try:
            if key not in ["symbol", "interval", "ma_type"]:
                val = float(val)
        except ValueError:
            val = self.cfg.get(key)
        self.cfg[key] = val
        self.config_changed.emit(self.cfg)


# ================================================================
# SYMBOL SELECTOR PANEL
# ================================================================
class SymbolSelector(QWidget):
    """Searchable symbol list panel."""

    symbol_selected = Signal(str)

    def __init__(self, symbols):
        super().__init__()
        self.symbols = symbols
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search Symbol…")
        self.search.textChanged.connect(self.filter_symbols)
        layout.addWidget(self.search)

        self.table = QTableWidget(0, 1)
        self.table.setHorizontalHeaderLabels(["Symbol"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.cellClicked.connect(self.select_symbol)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.table)

        self.load_symbols(self.symbols)

    def load_symbols(self, symbols):
        self.table.setRowCount(0)
        for s in symbols:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(s))

    def filter_symbols(self, text):
        text = text.upper()
        filtered = [s for s in self.symbols if text in s]
        self.load_symbols(filtered)

    def select_symbol(self, row, col):
        sym = self.table.item(row, 0).text()
        self.symbol_selected.emit(sym)


# ================================================================
# CHART WIDGET (CANDLE + TSI PANEL)
# ================================================================
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.dates as mdates

class ChartWidget(QWidget):
    """Candlestick chart + TSI panel"""

    def __init__(self, df=None):
        super().__init__()
        self.df = df
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        self.canvas = FigureCanvas(Figure(figsize=(6, 6)))
        layout.addWidget(self.canvas)

        self.ax_candle = self.canvas.figure.add_axes([0.05, 0.35, 0.9, 0.6])
        self.ax_tsi = self.canvas.figure.add_axes([0.05, 0.05, 0.9, 0.25], sharex=self.ax_candle)

        self.ax_candle.set_title("Candlestick Chart")
        self.ax_tsi.set_title("TSI Indicator")


    def plot(self, df):
        self.df = df.copy()
        self.ax_candle.clear()
        self.ax_tsi.clear()

        # Candles
        ohlc = df[["open", "high", "low", "close"]].copy()
        ohlc["time"] = mdates.date2num(df.index.to_pydatetime())
        for idx, row in ohlc.iterrows():
            color = "green" if row["close"] >= row["open"] else "red"
            self.ax_candle.plot([row["time"], row["time"]], [row["low"], row["high"]], color=color)
            self.ax_candle.plot([row["time"], row["time"]], [row["open"], row["close"]], color=color, linewidth=3)

        self.ax_candle.xaxis_date()
        self.ax_candle.grid(True)

        # TSI
        if "TSI" in df and "TSI_SIGNAL" in df:
            self.ax_tsi.plot(df.index, df["TSI"], label="TSI", color="blue")
            self.ax_tsi.plot(df.index, df["TSI_SIGNAL"], label="Signal", color="magenta")
        self.ax_tsi.axhline(0, color="black", linewidth=0.5)
        self.ax_tsi.grid(True)
        self.ax_tsi.legend()

        self.canvas.draw()
# ================================================================
# PART 2 — CONFIG MANAGER, STRATEGY EDITOR, SYMBOL SELECTOR, CHART WIDGETS
# ================================================================

# ================================================================
# CONFIG MANAGER
# ================================================================
class ConfigManager:
    """Handles loading/saving strategy configs and defaults."""

    def __init__(self, path="config.json"):
        self.path = path
        self.cfg = self.load_config()

    def load_config(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return self.default_config()

    def save_config(self, cfg=None):
        cfg = cfg or self.cfg
        with open(self.path, "w") as f:
            json.dump(cfg, f, indent=4)

    def default_config(self):
        return {
            "symbol": DEFAULT_SYMBOL,
            "interval": DEFAULT_INTERVAL,
            "ma_type": "EMA",
            "tsi_long_len": 47,
            "tsi_short_len": 17,
            "tsi_signal_len": 14,
            "take_profit_pct": 0.008,
            "stop_loss_pct": 0.0058,
            "max_trade_days": 7,
            "trend_ma_len": 14,
            "slope_lookback": 2,
            "min_slope_pct": 0.0119,
            "live_qty": 10
        }

# ================================================================
# STRATEGY EDITOR PANEL
# ================================================================
class StrategyEditor(QWidget):
    """Left panel with TSI strategy parameters."""

    config_changed = Signal(dict)
    backtest_requested = Signal()
    apply_strategy_requested = Signal(dict)

    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        # Inputs
        self.inputs = {}
        for label, key in [
            ("Symbol:", "symbol"),
            ("Interval:", "interval"),
            ("MA Type:", "ma_type"),
            ("TSI Long Length:", "tsi_long_len"),
            ("TSI Short Length:", "tsi_short_len"),
            ("TSI Signal Length:", "tsi_signal_len"),
            ("Take Profit (%):", "take_profit_pct"),
            ("Stop Loss (%):", "stop_loss_pct"),
            ("Max Trade Days:", "max_trade_days"),
            ("Trend MA Length:", "trend_ma_len"),
            ("Slope Lookback Bars:", "slope_lookback"),
            ("Min Slope (%):", "min_slope_pct"),
            ("Live Trade Qty (base or $):", "live_qty")
        ]:
            hbox = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setFixedWidth(160)
            edit = QLineEdit(str(self.cfg.get(key, "")))
            edit.setFixedWidth(120)
            edit.editingFinished.connect(lambda k=key, e=edit: self.update_cfg(k, e))
            hbox.addWidget(lbl)
            hbox.addWidget(edit)
            layout.addLayout(hbox)
            self.inputs[key] = edit

        # Buttons
        btn_backtest = QPushButton("Run Backtest")
        btn_backtest.clicked.connect(lambda: self.backtest_requested.emit())
        btn_apply = QPushButton("Apply Strategy")
        btn_apply.clicked.connect(lambda: self.apply_strategy_requested.emit(self.cfg))

        layout.addWidget(btn_backtest)
        layout.addWidget(btn_apply)
        layout.addStretch(1)

    def update_cfg(self, key, edit):
        val = edit.text()
        try:
            if key not in ["symbol", "interval", "ma_type"]:
                val = float(val)
        except ValueError:
            val = self.cfg.get(key)
        self.cfg[key] = val
        self.config_changed.emit(self.cfg)


# ================================================================
# SYMBOL SELECTOR PANEL
# ================================================================
class SymbolSelector(QWidget):
    """Searchable symbol list panel."""

    symbol_selected = Signal(str)

    def __init__(self, symbols=None):
        super().__init__()
        self.symbols = symbols or []
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search Symbol…")
        self.search.textChanged.connect(self.filter_symbols)
        layout.addWidget(self.search)

        self.table = QTableWidget(0, 1)
        self.table.setHorizontalHeaderLabels(["Symbol"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.cellClicked.connect(self.select_symbol)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.table)

        self.load_symbols(self.symbols)

    def load_symbols(self, symbols):
        self.table.setRowCount(0)
        for s in symbols:
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(s))

    def filter_symbols(self, text):
        text = text.upper()
        filtered = [s for s in self.symbols if text in s]
        self.load_symbols(filtered)

    def select_symbol(self, row, col):
        sym = self.table.item(row, 0).text()
        self.symbol_selected.emit(sym)


# ================================================================
# CHART WIDGET (CANDLE + TSI PANEL)
# ================================================================
class ChartWidget(QWidget):
    """Candlestick chart + TSI panel"""

    def __init__(self, df=None):
        super().__init__()
        self.df = df
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout(self)
        self.canvas = FigureCanvas(Figure(figsize=(6, 6)))
        layout.addWidget(self.canvas)

        self.ax_candle = self.canvas.figure.add_axes([0.05, 0.35, 0.9, 0.6])
        self.ax_tsi = self.canvas.figure.add_axes([0.05, 0.05, 0.9, 0.25], sharex=self.ax_candle)

        self.ax_candle.set_title("Candlestick Chart")
        self.ax_tsi.set_title("TSI Indicator")


    def plot_dataframe(self, df):
        """Plot OHLC + TSI"""
        self.df = df.copy()
        self.ax_candle.clear()
        self.ax_tsi.clear()

        # Candlestick
        ohlc = df[["open","high","low","close"]].copy()
        ohlc["time"] = mdates.date2num(df.index.to_pydatetime())
        for idx, row in ohlc.iterrows():
            color = "green" if row["close"] >= row["open"] else "red"
            self.ax_candle.plot([row["time"], row["time"]], [row["low"], row["high"]], color=color)
            self.ax_candle.plot([row["time"], row["time"]], [row["open"], row["close"]], color=color, linewidth=3)

        self.ax_candle.xaxis_date()
        self.ax_candle.grid(True)

        # TSI
        if "TSI" in df and "TSI_SIGNAL" in df:
            self.ax_tsi.plot(df.index, df["TSI"], label="TSI", color="blue")
            self.ax_tsi.plot(df.index, df["TSI_SIGNAL"], label="Signal", color="magenta")
        self.ax_tsi.axhline(0, color="black", linewidth=0.5)
        self.ax_tsi.grid(True)
        self.ax_tsi.legend()
        self.canvas.draw()

    def update_live_candle(self, candle):
        # Optional: update last candle for live streaming
        pass

    def clear_indicators(self):
        self.ax_tsi.clear()
        self.canvas.draw()

    def plot_indicator(self, series, color="#ffaa00"):
        self.ax_candle.plot(self.df.index, series, color=color)
        self.canvas.draw()

    def plot_trades(self, trades):
        for t in trades:
            color = "green" if t["type"] == "BUY" else "red"
            self.ax_candle.scatter(mdates.date2num(t["time"]), t["price"], color=color, marker="^" if t["type"]=="BUY" else "v")
        self.canvas.draw()
# ================================================================
# PART 3 — TRADE TABLE, TRADING CONTROLS, INDICATOR PANEL,
#          PERFORMANCE PANEL, BACKTEST CONTROLS
# ================================================================


# ================================================================
# INDICATOR PANEL (TSI Display for Live Updates)
# ================================================================
class IndicatorPanel(QWidget):
    """Shows live TSI + Signal values updated every candle."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        self.lbl_tsi = QLabel("TSI: 0.00")
        self.lbl_signal = QLabel("Signal: 0.00")

        self.lbl_tsi.setStyleSheet("font-size: 14px;")
        self.lbl_signal.setStyleSheet("font-size: 14px; color: #8844ff;")

        layout.addWidget(self.lbl_tsi)
        layout.addWidget(self.lbl_signal)
        layout.addStretch()

    def plot_tsi(self, df):
        """Initial load — show last TSI values."""
        if df is None or "TSI" not in df:
            return

        last = df.iloc[-1]
        self.lbl_tsi.setText(f"TSI: {last['TSI']:.2f}")
        self.lbl_signal.setText(f"Signal: {last['TSI_SIGNAL']:.2f}")

    def update_live_tsi(self, vals):
        """Live update from MainWindow during websocket stream."""
        tsi = vals.get("TSI", 0)
        sig = vals.get("TSI_SIGNAL", 0)
        self.lbl_tsi.setText(f"TSI: {tsi:.2f}")
        self.lbl_signal.setText(f"Signal: {sig:.2f}")
# ================================================================
# TRADE TABLE PANEL
# ================================================================
class TradeTable(QWidget):
    """Displays executed trades with PnL and exit reason."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Type", "Time", "Price", "PNL %", "Exit Reason"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.table)

    def add_trade(self, trade):
        row = self.table.rowCount()
        self.table.insertRow(row)

        self.table.setItem(row, 0, QTableWidgetItem(trade.get("type", "")))
        self.table.setItem(row, 1, QTableWidgetItem(str(trade.get("time", ""))))
        self.table.setItem(row, 2, QTableWidgetItem(f"{trade.get('price', 0):.4f}"))
        self.table.setItem(row, 3, QTableWidgetItem(str(trade.get("pnl_pct", ""))))
        self.table.setItem(row, 4, QTableWidgetItem(trade.get("exit_reason", "")))
# ================================================================
# TRADING CONTROLS PANEL (3 BUTTONS PER ROW)
# ================================================================
class TradingControls(QWidget):
    live_start = Signal()
    live_stop = Signal()
    paper_start = Signal()
    paper_stop = Signal()
    optimise_start = Signal()
    test_buy = Signal()
    test_sell = Signal()

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        # ---------------- ROW 1 ----------------
        row1 = QHBoxLayout()
        self.live_btn = QPushButton("Start Live Trading")
        self.live_stop_btn = QPushButton("Stop Live Trading")
        self.paper_btn = QPushButton("Start Paper Trading")
        row1.addWidget(self.live_btn)
        row1.addWidget(self.live_stop_btn)
        row1.addWidget(self.paper_btn)
        layout.addLayout(row1)

        # ---------------- ROW 2 ----------------
        row2 = QHBoxLayout()
        self.paper_stop_btn = QPushButton("Stop Paper Trading")
        self.optimise_btn = QPushButton("Optimise Strategy")
        self.test_buy_btn = QPushButton("Test Buy")
        row2.addWidget(self.paper_stop_btn)
        row2.addWidget(self.optimise_btn)
        row2.addWidget(self.test_buy_btn)
        layout.addLayout(row2)

        # ---------------- ROW 3 ----------------
        row3 = QHBoxLayout()
        self.test_sell_btn = QPushButton("Test Sell")
        row3.addWidget(self.test_sell_btn)
        row3.addStretch(1)
        layout.addLayout(row3)

        # Emit signals
        self.live_btn.clicked.connect(self.live_start)
        self.live_stop_btn.clicked.connect(self.live_stop)
        self.paper_btn.clicked.connect(self.paper_start)
        self.paper_stop_btn.clicked.connect(self.paper_stop)
        self.optimise_btn.clicked.connect(self.optimise_start)
        self.test_buy_btn.clicked.connect(self.test_buy)
        self.test_sell_btn.clicked.connect(self.test_sell)

        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
# ================================================================
# PERFORMANCE PANEL (CPU/MEM USAGE + PnL SUMMARY)
# ================================================================
class PerformancePanel(QWidget):
    """Displays total PnL, win rate, drawdown, trades and system resource usage."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        self.lbl_pnl = QLabel("Total PnL: 0.00%")
        self.lbl_win = QLabel("Win Rate: 0.00%")
        self.lbl_drawdown = QLabel("Max Drawdown: 0.00%")
        self.lbl_trades = QLabel("Trades: 0")
        self.lbl_tp = QLabel("Take Profit Exits: 0")
        self.lbl_sl = QLabel("Stop Loss Exits: 0")
        self.lbl_cpu = QLabel("CPU: 0%")
        self.lbl_mem = QLabel("Memory: 0 MB")

        for lbl in [
            self.lbl_pnl, self.lbl_win, self.lbl_drawdown, self.lbl_trades,
            self.lbl_tp, self.lbl_sl, self.lbl_cpu, self.lbl_mem
        ]:
            lbl.setStyleSheet("font-size: 13px;")
            layout.addWidget(lbl)

        # Timer to update CPU/Mem
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_usage)
        self.timer.start(1000)

    def update_summary(self, summary):
        self.lbl_pnl.setText(f"Total PnL: {summary.get('pnl', 0):.2f}%")
        self.lbl_win.setText(f"Win Rate: {summary.get('win_rate', 0):.2f}%")
        self.lbl_drawdown.setText(f"Max Drawdown: {summary.get('max_drawdown', 0):.2f}%")
        self.lbl_trades.setText(f"Trades: {summary.get('trades', 0)}")
        self.lbl_tp.setText(f"Take Profit Exits: {summary.get('tp', 0)}")
        self.lbl_sl.setText(f"Stop Loss Exits: {summary.get('sl', 0)}")

    def update_usage(self):
        cpu = psutil.cpu_percent()
        mem = psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
        self.lbl_cpu.setText(f"CPU: {cpu:.0f}%")
        self.lbl_mem.setText(f"Memory: {mem:.1f} MB")

# ================================================================
# PART 4 — MAIN WINDOW + LIVE/PAPER TRADING ENGINES
# ================================================================


# ================================================================
# PAPER TRADER (LIVE SIMULATION ON LIVE CANDLES)
# ================================================================
class LivePaperTrader:
    def __init__(self, cfg):
        self.cfg = cfg
        self.position = None
        self.entry_price = None

    def update_params(self, cfg):
        self.cfg = cfg

    def start(self):
        print("[PAPER] Paper trading started.")

    def stop(self):
        print("[PAPER] Paper trading stopped.")
        self.position = None
        self.entry_price = None

    def process_live_tick(self, df):
        """Executes the TSI strategy on the latest candle in LIVE mode."""
        if df is None or len(df) < 10:
            return

        row = df.iloc[-1]
        prev = df.iloc[-2]

        # Entry conditions
        long_cond = row["TSI"] > row["TSI_SIGNAL"] and prev["TSI"] <= prev["TSI_SIGNAL"]
        trend_ok = row["IS_UP"]

        if self.position is None:
            # BUY if entry conditions met
            if long_cond and trend_ok:
                self.position = "LONG"
                self.entry_price = row["close"]
                print(f"[PAPER BUY] {self.entry_price:.4f}")
                return

        # Manage active position
        if self.position == "LONG":
            tp = self.entry_price * (1 + self.cfg["take_profit_pct"])
            sl = self.entry_price * (1 - self.cfg["stop_loss_pct"])

            if row["close"] >= tp:
                print(f"[PAPER SELL TP] {row['close']:.4f}")
                self.position = None

            elif row["close"] <= sl:
                print(f"[PAPER SELL SL] {row['close']:.4f}")
                self.position = None
# ================================================================
# LIVE BINANCE TRADER (REAL ORDERS)
# ================================================================
class LiveBinanceTrader:
    def __init__(self, cfg, symbol):
        self.cfg = cfg
        self.symbol = symbol
        self.running = False
        self.position = None
        self.entry_price = None

    def update_params(self, cfg):
        self.cfg = cfg

    def start(self):
        self.running = True
        print("[LIVE] Live trading STARTED.")

    def stop(self):
        self.running = False
        print("[LIVE] Live trading STOPPED.")

    def process_live_tick(self, df):
        """Executes strategy in live mode (REAL or TEST mode)."""
        if not self.running:
            return

        if df is None or len(df) < 10:
            return

        row = df.iloc[-1]
        prev = df.iloc[-2]

        long_cond = row["TSI"] > row["TSI_SIGNAL"] and prev["TSI"] <= prev["TSI_SIGNAL"]
        trend_ok = row["IS_UP"]

        if self.position is None:
            if long_cond and trend_ok:
                self.position = "LONG"
                self.entry_price = row["close"]
                print(f"[LIVE BUY] {self.entry_price:.4f}")
                binance_send_order(self.symbol, "BUY", self.cfg["live_qty"], test=False)
                return

        if self.position == "LONG":
            tp = self.entry_price * (1 + self.cfg["take_profit_pct"])
            sl = self.entry_price * (1 - self.cfg["stop_loss_pct"])

            if row["close"] >= tp:
                print(f"[LIVE SELL TP] {row['close']:.4f}")
                binance_send_order(self.symbol, "SELL", self.cfg["live_qty"], test=False)
                self.position = None

            elif row["close"] <= sl:
                print(f"[LIVE SELL SL] {row['close']:.4f}")
                binance_send_order(self.symbol, "SELL", self.cfg["live_qty"], test=False)
                self.position = None
# ================================================================
# MAIN WINDOW
# ================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("TSI Trading App")
        self.resize(1900, 1050)

        # ---------------- LOAD CONFIG ----------------
        self.cfgmgr = ConfigManager()
        self.cfg = self.cfgmgr.cfg

        # ---------------- CENTRAL WIDGET ----------------
        central = QWidget()
        main_layout = QVBoxLayout(central)
        self.setCentralWidget(central)

        # ============================================================
        # TOP HALF — CHART + INDICATOR PANEL
        # ============================================================
        chart_container = QWidget()
        chart_layout = QVBoxLayout(chart_container)
        chart_layout.setContentsMargins(0, 0, 0, 0)

        self.chart_widget = ChartWidget()
        self.indicator_panel = IndicatorPanel()

        chart_layout.addWidget(self.chart_widget, stretch=6)
        chart_layout.addWidget(self.indicator_panel, stretch=1)

        main_layout.addWidget(chart_container, stretch=8)

        # ============================================================
        # BOTTOM HALF — TRADE TABLE / TRADING CONTROLS / PERFORMANCE
        # ============================================================
        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 0, 0, 0)

        self.trade_table = TradeTable()
        self.trading_controls = TradingControls()
        self.performance_panel = PerformancePanel()

        bottom_layout.addWidget(self.trade_table, stretch=4)
        bottom_layout.addWidget(self.trading_controls, stretch=2)
        bottom_layout.addWidget(self.performance_panel, stretch=2)

        main_layout.addWidget(bottom, stretch=3)



        # ============================================================
        # SIDE PANELS: Strategy Editor + Symbol Selector
        # ============================================================
        self.symbol_list = fetch_all_symbols()

        self.symbol_panel = SymbolSelector(self.symbol_list)
        self.strategy_panel = StrategyEditor(self.cfg)

        dock_symbols = QDockWidget("Symbols")
        dock_symbols.setWidget(self.symbol_panel)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock_symbols)

        dock_strategy = QDockWidget("Strategy")
        dock_strategy.setWidget(self.strategy_panel)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock_strategy)

        # ============================================================
        # TRADERS
        # ============================================================
        self.paper_trader = LivePaperTrader(self.cfg)
        self.live_trader = LiveBinanceTrader(self.cfg, self.cfg["symbol"])

        self.paper_trading_active = False
        self.live_trading_active = False

        # ============================================================
        # SIGNALS
        # ============================================================
        self.symbol_panel.symbol_selected.connect(self.on_symbol_changed)

        self.strategy_panel.backtest_requested.connect(self.run_backtest)
        self.strategy_panel.apply_strategy_requested.connect(self.apply_strategy)

        self.trading_controls.live_start.connect(self.start_live_trading)
        self.trading_controls.live_stop.connect(self.stop_live_trading)
        self.trading_controls.paper_start.connect(self.start_paper_trading)
        self.trading_controls.paper_stop.connect(self.stop_paper_trading)
        self.trading_controls.optimise_start.connect(self.run_backtest)
        self.trading_controls.test_buy.connect(self.test_buy)
        self.trading_controls.test_sell.connect(self.test_sell)


        # ============================================================
        # INITIAL LOAD
        # ============================================================
        self.df = None
        self.stream = None
        self.load_symbol(self.cfg["symbol"], self.cfg["interval"])

    # ============================================================
    # SYMBOL LOADING
    # ============================================================
    def on_symbol_changed(self, sym):
        self.cfg["symbol"] = sym
        self.cfgmgr.save_config(self.cfg)
        self.load_symbol(sym, self.cfg["interval"])

    def load_symbol(self, symbol, interval):
        self.df = fetch_historical_klines(symbol, interval)
        trades, self.df = run_tsi_strategy(self.df,
            longLen=self.cfg["tsi_long_len"],
            shortLen=self.cfg["tsi_short_len"],
            signalLen=self.cfg["tsi_signal_len"],
            tp_percent=self.cfg["take_profit_pct"],
            sl_percent=self.cfg["stop_loss_pct"],
            maxTradeDays=self.cfg["max_trade_days"],
            maType=self.cfg["ma_type"],
            maLength=self.cfg["trend_ma_len"],
            trendSlopeLen=self.cfg["slope_lookback"],
            minSlope=self.cfg["min_slope_pct"]
        )

        self.chart_widget.plot_dataframe(self.df)
        self.indicator_panel.plot_tsi(self.df)
        self.start_stream(symbol, interval)

    # ============================================================
    # STREAMING / LIVE TICKS
    # ============================================================
    def start_stream(self, symbol, interval):
        if self.stream:
            self.stream.stop()

        self.stream = CandleStream(symbol, interval)
        self.stream.new_candle.connect(self.on_live_candle)
        self.stream.start()

    def on_live_candle(self, candle):
        ts = pd.to_datetime(candle["time"]).replace(second=0, microsecond=0)
        o, h, l, c_, v = candle["open"], candle["high"], candle["low"], candle["close"], candle["volume"]

        if ts in self.df.index:
            self.df.loc[ts, ["open", "high", "low", "close", "volume"]] = [o, h, l, c_, v]

        else:
            self.df.loc[ts, ["open", "high", "low", "close", "volume"]] = [o, h, l, c_, v]

            self.df.sort_index(inplace=True)

        # Recompute indicators for last section of data
        recent = self.df.iloc[-200:]
        ind = compute_indicators(
            recent,
            self.cfg["tsi_long_len"],
            self.cfg["tsi_short_len"],
            self.cfg["tsi_signal_len"],
            self.cfg["ma_type"],
            self.cfg["trend_ma_len"],
            self.cfg["slope_lookback"],
            self.cfg["min_slope_pct"]
        )
        for key in ind:
            if key not in self.df.columns:
                # create column with NaNs first so iloc has a valid location
                self.df[key] = np.nan

            col_idx = self.df.columns.get_loc(key)
            vals = ind[key].values

            # assign only matching lengths
            assign_len = min(200, len(self.df), len(vals))
            self.df.iloc[-assign_len:, col_idx] = vals[-assign_len:]


        self.chart_widget.plot_dataframe(self.df)
        self.indicator_panel.update_live_tsi({
            "TSI": self.df["TSI"].iloc[-1],
            "TSI_SIGNAL": self.df["TSI_SIGNAL"].iloc[-1]
        })

        # Route to active trader
        if self.live_trading_active:
            self.live_trader.process_live_tick(self.df)
        elif self.paper_trading_active:
            self.paper_trader.process_live_tick(self.df)
    # ============================================================
    # STRATEGY APPLY / BACKTEST
    # ============================================================
    def apply_strategy(self, params):
        self.cfg.update(params)
        self.cfgmgr.save_config(self.cfg)

        trades, df = run_tsi_strategy(
            self.df,
            longLen=self.cfg["tsi_long_len"],
            shortLen=self.cfg["tsi_short_len"],
            signalLen=self.cfg["tsi_signal_len"],
            tp_percent=self.cfg["take_profit_pct"],
            sl_percent=self.cfg["stop_loss_pct"],
            maxTradeDays=self.cfg["max_trade_days"],
            maType=self.cfg["ma_type"],
            maLength=self.cfg["trend_ma_len"],
            trendSlopeLen=self.cfg["slope_lookback"],
            minSlope=self.cfg["min_slope_pct"]
        )

        self.df = df
        self.chart_widget.plot_dataframe(df)
        self.indicator_panel.plot_tsi(df)

        self.trade_table.table.setRowCount(0)
        for t in trades:
            self.trade_table.add_trade({
                "type": t["type"],
                "time": t["time"],
                "price": t["price"],
                "pnl_pct": "",
                "exit_reason": t.get("reason", "")
            })

    def run_backtest(self):
        print("[BACKTEST] Running full backtest…")
        self.apply_strategy(self.cfg)
    # ============================================================
    # TRADING MODES
    # ============================================================
    def start_live_trading(self):
        self.stop_paper_trading()
        self.live_trader.update_params(self.cfg)
        self.live_trading_active = True
        self.live_trader.start()

    def stop_live_trading(self):
        self.live_trader.stop()
        self.live_trading_active = False

    def start_paper_trading(self):
        self.stop_live_trading()
        self.paper_trader.update_params(self.cfg)
        self.paper_trading_active = True
        self.paper_trader.start()

    def stop_paper_trading(self):
        self.paper_trading_active = False
        self.paper_trader.stop()


    # ============================================================
    # TEST BUY / SELL BUTTONS
    # ============================================================
    def test_buy(self):
        qty = self.cfg["live_qty"]
        sym = self.cfg["symbol"]
        print(f"[TEST BUY] {qty} {sym}")
        binance_send_order(sym, "BUY", qty, test=True)

    def test_sell(self):
        qty = self.cfg["live_qty"]
        sym = self.cfg["symbol"]
        print(f"[TEST SELL] {qty} {sym}")
        binance_send_order(sym, "SELL", qty, test=True)



# ================================================================
# APPLICATION ENTRY POINT
# ================================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)

    window = MainWindow()
    window.show()

    sys.exit(app.exec())
