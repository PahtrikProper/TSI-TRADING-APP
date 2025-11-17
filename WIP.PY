# ================================================================
# SECTION 1 — IMPORTS + GLOBAL SETTINGS + APPLICATION SKELETON
# ================================================================

import sys
import json
import time
import threading
from datetime import datetime
import numpy as np
import pandas as pd
import requests
import websocket
import urllib3
import os
import concurrent.futures
import random

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox, QLineEdit, QTableWidget,
    QTableWidgetItem, QSplitter
)


from PySide6.QtCore import Qt, Signal, QObject, QThread, QEvent, QSize
from PySide6.QtGui import QDoubleValidator, QIntValidator

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BINANCE_REST = "https://api.binance.com"
DEFAULT_SYMBOL = "SOLUSDT"
DEFAULT_INTERVAL = "15m"
EMA = "EMA"
SMA = "SMA"
WMA = "WMA"

INTERVAL_MS = {
    "1m": 60000,
    "3m": 180000,
    "5m": 300000,
    "15m": 900000,
    "30m": 1800000,
    "1h": 3600000,
    "2h": 7200000,
    "4h": 14400000,
    "6h": 21600000,
    "8h": 28800000,
    "12h": 43200000,
    "1d": 86400000,
    "3d": 259200000,
    "1w": 604800000,
}

TRADINGVIEW_QSS = """
QWidget {
    background-color: #030712;
    color: #D7DEEE;
    font-family: 'Inter', 'Segoe UI', sans-serif;
    font-size: 13px;
}
QLabel {
    color: #8F9BB3;
}
QLabel#HeaderSymbol {
    color: #F4F7FF;
    font-size: 24px;
    font-weight: 700;
}
QLabel#HeaderInterval {
    font-size: 16px;
    font-weight: 600;
    color: #7DC1FF;
}
QLabel#HeaderPrice {
    font-size: 28px;
    font-weight: 700;
    color: #FFFFFF;
}
QLabel#HeaderChange {
    font-size: 16px;
}
QLabel#HeaderStatus {
    color: #7E8DA8;
    font-size: 12px;
    letter-spacing: 0.5px;
}
QLineEdit, QComboBox, QTableWidget, QTableView {
    background-color: #0A1422;
    border: 1px solid #172235;
    border-radius: 4px;
    padding: 4px 6px;
    color: #E3E9F5;
}
QLineEdit[locked="true"] {
    color: #6D768C;
}
QPushButton {
    background-color: #1A2537;
    border: 1px solid #24324B;
    border-radius: 4px;
    padding: 8px 10px;
    color: #EFF2FF;
    font-weight: 600;
}
QPushButton:hover {
    background-color: #273656;
}
QPushButton:pressed {
    background-color: #1C283D;
}
QPushButton:disabled {
    background-color: #111827;
    color: #576074;
    border-color: #1d2535;
}
QTableWidget {
    gridline-color: #172235;
}
QHeaderView::section {
    background-color: #0F1928;
    color: #A8B3C9;
    border: none;
    padding: 4px 6px;
}
QSplitter::handle {
    background-color: #101726;
    margin: 2px;
}
"""

# ================================================================
# SECTION 2 — BINANCE REST FETCH
# ================================================================

def safe_get_json(url, params=None):
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("REST API error:", e)
        return None

def fetch_historical_klines(symbol=DEFAULT_SYMBOL, interval=DEFAULT_INTERVAL):
    print(f"[REST] Fetching {symbol} {interval} (1500 limit)")

    url = BINANCE_REST + "/api/v3/klines"
    raw = safe_get_json(url, {"symbol": symbol.upper(), "interval": interval, "limit": 1500})
    if not raw:
        raise RuntimeError("Failed to fetch klines")

    df = pd.DataFrame(raw, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qav","num_trades","tbbav","tbqav","ignore"
    ])

    for col in ["open","high","low","close","volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["time"] = pd.to_datetime(df["open_time"], unit="ms")
    df.set_index("time", inplace=True)

    df = df[["open","high","low","close","volume"]]
    print("Last candle:", df.index[-1])
    return df

# ================================================================
# SECTION 3 — REAL-TIME WEBSOCKET STREAM
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
                self.new_candle.emit({
                    "time": pd.to_datetime(k["t"], unit="ms"),
                    "open": float(k["o"]),
                    "high": float(k["h"]),
                    "low": float(k["l"]),
                    "close": float(k["c"]),
                    "volume": float(k["v"]),
                    "is_closed": bool(k["x"])
                })
            except Exception as e:
                print("WS parse error:", e)

        def on_error(ws, err):
            print("WS error:", err)

        def on_close(ws, a, b):
            print("WS closed — reconnecting")
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
        if self.ws:
            try: self.ws.close()
            except: pass

# ================================================================
# SECTION 4 — INDICATORS
# ================================================================

def ema(series, length): return series.ewm(span=length, adjust=False).mean()
def sma(series, length): return series.rolling(length).mean()

def wma(series, length):
    w = np.arange(1, length + 1)
    return series.rolling(length).apply(lambda x: np.dot(x, w) / w.sum(), raw=True)

def moving_average(series, length, ma_type):
    t = ma_type.upper()
    if t == "EMA": return ema(series, length)
    if t == "SMA": return sma(series, length)
    if t == "WMA": return wma(series, length)
    raise ValueError("Invalid MA type")

def double_smooth(series, long_len, short_len):
    return ema(ema(series, long_len), short_len)

def compute_tsi(df, long_len, short_len, signal_len):
    pc = df["close"].diff()
    tsi = 100 * (double_smooth(pc, long_len, short_len) /
                 double_smooth(pc.abs(), long_len, short_len))
    sig = ema(tsi, signal_len)
    return tsi, sig

def compute_trend_filter(df, ma_length, ma_type, slope_lookback, min_slope):
    ma = moving_average(df["close"], ma_length, ma_type)
    slope = 100 * ((ma - ma.shift(slope_lookback)) / ma.shift(slope_lookback))
    up = slope > min_slope
    return ma, slope, up

def compute_indicators(df, longLen=25, shortLen=13, signalLen=13,
                       maType="EMA", maLength=50,
                       trendSlopeLen=5, minSlope=0.01):

    tsi, sig = compute_tsi(df, longLen, shortLen, signalLen)
    ma, slope, up = compute_trend_filter(df, maLength, maType, trendSlopeLen, minSlope)

    return {
        "TSI": tsi,
        "TSI_SIGNAL": sig,
        "TREND_MA": ma,
        "SLOPE": slope,
        "IS_UP": up
    }

# ================================================================
# SECTION 5 — STRATEGY ENGINE (WITH STOP LOSS)
# ================================================================

def run_tsi_strategy(
        df,
        longLen=25,
        shortLen=13,
        signalLen=13,
        tp_percent=0.004,
        sl_percent=0.002,
        maxTradeDays=7,
        maType="EMA",
        maLength=50,
        trendSlopeLen=5,
        minSlope=0.01):

    ind = compute_indicators(
        df,longLen,shortLen,signalLen,
        maType,maLength,trendSlopeLen,minSlope
    )

    for k,v in ind.items(): df[k] = v

    trades = []
    in_pos = False
    entry = None
    last_day = None
    day_count = 0

    for i in range(1, len(df)):
        row = df.iloc[i]; prev = df.iloc[i - 1]

        if pd.isna(row["TSI"]) or pd.isna(row["TREND_MA"]):
            continue
        if not row["IS_UP"]:
            continue

        longSignal = (row["TSI"] > row["TSI_SIGNAL"]) and (prev["TSI"] <= prev["TSI_SIGNAL"])

        d = row.name
        dk = (d.year, d.month, d.day)
        if longSignal and dk != last_day:
            day_count += 1
            last_day = dk
        if day_count > maxTradeDays:
            continue

        if longSignal and not in_pos:
            entry = row["close"]
            in_pos = True
            trades.append({"type":"BUY","time":row.name,"price":entry})

        if in_pos:
            tp = entry * (1 + tp_percent)
            sl = entry * (1 - sl_percent)

            exit_reason = None
            exit_price = None

            if row["low"] <= sl:
                exit_reason = "SL"
                exit_price = sl
            elif row["high"] >= tp:
                exit_reason = "TP"
                exit_price = tp

            if exit_reason:
                in_pos = False
                trades.append({
                    "type": "SELL",
                    "time": row.name,
                    "price": exit_price,
                    "reason": exit_reason
                })

    return trades, df

# ================================================================
# SECTION 6 — BACKTEST METRICS
# ================================================================

def compute_backtest_metrics(trades, df):
    if not trades:
        return {"total_pnl":0,"win_rate":0,"max_drawdown":0,"num_trades":0}

    pnl_list=[]; wins=0; eq=[1.0]
    i=0
    while i < len(trades):
        if trades[i]["type"]=="BUY" and i+1<len(trades) and trades[i+1]["type"]=="SELL":
            e=trades[i]["price"]; x=trades[i+1]["price"]
            p=(x-e)/e
            pnl_list.append(p)
            if p>0: wins+=1
            eq.append(eq[-1]*(1+p))
            i+=2
        else: i+=1

    total=(eq[-1]-1)*100
    win=(wins/len(pnl_list))*100 if pnl_list else 0

    maxdd=0; peak=eq[0]
    for v in eq:
        if v>peak: peak=v
        dd=(peak-v)/peak
        if dd>maxdd: maxdd=dd

    return {
        "total_pnl": total,
        "win_rate": win,
        "max_drawdown": maxdd*100,
        "num_trades": len(pnl_list)
    }

def backtest_strategy(df, params=None):
    if params is None: params={}
    trades, df = run_tsi_strategy(
        df,
        longLen=params.get("longLen",25),
        shortLen=params.get("shortLen",13),
        signalLen=params.get("signalLen",13),
        tp_percent=params.get("tp_percent",0.004),
        sl_percent=params.get("sl_percent",0.002),
        maxTradeDays=params.get("maxTradeDays",7),
        maType=params.get("maType","EMA"),
        maLength=params.get("maLength",50),
        trendSlopeLen=params.get("trendSlopeLen",5),
        minSlope=params.get("minSlope",0.01)
    )
    return trades, df, compute_backtest_metrics(trades, df)

# ================================================================
# SECTION 7 — ORDER EXECUTION SIMULATOR
# ================================================================

class OrderSimulator:
    def __init__(self,
                 slippage_pct=0.0002,
                 fee_pct=0.0004,
                 partial_fill=True,
                 partial_fill_steps=3,
                 latency_ms=150):

        self.slippage_pct=slippage_pct
        self.fee_pct=fee_pct
        self.partial_fill=partial_fill
        self.partial_fill_steps=partial_fill_steps
        self.latency_ms=latency_ms

    def _apply_slippage(self, price, side):
        return price*(1+self.slippage_pct) if side=="BUY" else price*(1-self.slippage_pct)

    def _apply_spread(self, bid, ask, side):
        return ask if side=="BUY" else bid

    def _apply_fee(self, qty, price):
        return qty*price*self.fee_pct

    def execute(self, side, qty, bid, ask, timestamp=None):
        timestamp = timestamp or datetime.utcnow()

        base = self._apply_spread(bid,ask,side)
        price = self._apply_slippage(base,side)

        if self.latency_ms>0:
            time.sleep(self.latency_ms/1000)

        if not self.partial_fill:
            fee=self._apply_fee(qty,price)
            return {"time":timestamp,"side":side,"avg_price":price,"qty_filled":qty,"fee_paid":fee}

        step=qty/self.partial_fill_steps
        tot_cost=0; tot_qty=0
        for _ in range(self.partial_fill_steps):
            cost=price*step
            tot_cost+=cost
            tot_qty+=step
            time.sleep(self.latency_ms/(1000*self.partial_fill_steps))

        avg=tot_cost/tot_qty
        fee=self._apply_fee(tot_qty,avg)
        return {"time":timestamp,"side":side,"avg_price":avg,"qty_filled":tot_qty,"fee_paid":fee}

# ================================================================
# SECTION 8 — CONFIG MANAGER
# ================================================================

class ConfigManager:
    CONFIG_FILE="strategy_config.json"

    DEFAULTS={
        "symbol":DEFAULT_SYMBOL,
        "interval":DEFAULT_INTERVAL,
        "longLen":25,
        "shortLen":13,
        "signalLen":13,
        "tp_percent":0.004,
        "sl_percent":0.002,
        "maxTradeDays":7,
        "maType":"EMA",
        "maLength":50,
        "trendSlopeLen":5,
        "minSlope":0.01,
        "slippage_pct":0.0002,
        "fee_pct":0.0004,
        "partial_fill_steps":3,
        "latency_ms":150,
    }

    @staticmethod
    def load():
        try:
            with open(ConfigManager.CONFIG_FILE,"r") as f:
                cfg=json.load(f)
        except:
            cfg={}

        for k,v in ConfigManager.DEFAULTS.items():
            if k not in cfg:
                cfg[k]=v
        cfg["symbol"] = DEFAULT_SYMBOL
        cfg["interval"] = DEFAULT_INTERVAL
        ConfigManager.save(cfg)
        return cfg

    @staticmethod
    def save(cfg):
        try:
            with open(ConfigManager.CONFIG_FILE,"w") as f:
                json.dump(cfg,f,indent=4)
        except Exception as e:
            print("Config save error:",e)

# ================================================================
# SECTION 9 — TRADINGVIEW-STYLE CHART (PYQTGRAPH)
# ================================================================

import pyqtgraph as pg

class CandlestickItem(pg.GraphicsObject):
    def __init__(self, data):
        super().__init__()
        self.data=data
        self.generate_picture()

    def generate_picture(self):
        self.picture=pg.QtGui.QPicture()
        p=pg.QtGui.QPainter(self.picture)
        w=0.35
        for (i,o,c,l,h) in self.data:
            pen = pg.mkPen('#00ff00') if c>=o else pg.mkPen('#ff0000')
            brush = pg.mkBrush('#00ff00') if c>=o else pg.mkBrush('#ff0000')
            p.setPen(pen); p.setBrush(brush)
            p.drawLine(pg.QtCore.QPointF(i,l), pg.QtCore.QPointF(i,h))
            p.drawRect(pg.QtCore.QRectF(i-w,o,w*2,c-o))
        p.end()

    def paint(self, p, *args):
        p.drawPicture(0,0,self.picture)

    def boundingRect(self):
        return self.picture.boundingRect()

class ChartWidget(QWidget):
    def __init__(self):
        super().__init__()
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)

        self.plot=pg.PlotWidget()
        self.plot.setBackground('#050A12')
        self.plot.showGrid(x=True,y=True,alpha=0.15)
        self.plot.getAxis('left').setPen(pg.mkPen('#394252'))
        self.plot.getAxis('bottom').setPen(pg.mkPen('#394252'))
        self.plot.getAxis('left').setTextPen(pg.mkPen('#A7B4C6'))
        self.plot.getAxis('bottom').setTextPen(pg.mkPen('#A7B4C6'))
        layout.addWidget(self.plot)

        self.df=None
        self.candle_item=None
        self.indicator_items=[]
        self.trade_markers=[]
        self.autoscroll=True

    def plot_dataframe(self, df):
        self.df=df.copy()
        arr=np.column_stack([
            np.arange(len(df)),
            df["open"].values,
            df["close"].values,
            df["low"].values,
            df["high"].values
        ])
        if self.candle_item:
            self.plot.removeItem(self.candle_item)
        self.candle_item=CandlestickItem(arr)
        self.plot.addItem(self.candle_item)
        self._clear_trade_markers()
        self.plot.enableAutoRange(axis='x',enable=True)
        self.plot.enableAutoRange(axis='y',enable=True)

    def update_live_candle(self, candle):
        if self.df is None: return

        ts=candle["time"]
        if not isinstance(ts,pd.Timestamp):
            ts=pd.to_datetime(ts)
        ts=ts.replace(second=0,microsecond=0)

        o=float(candle["open"]); h=float(candle["high"])
        l=float(candle["low"]);  c=float(candle["close"])
        v=float(candle["volume"])
        raw=["open","high","low","close","volume"]

        if ts in self.df.index:
            self.df.loc[ts,raw]=[o,h,l,c,v]
        else:
            new_row={col:np.nan for col in self.df.columns}
            for col,val in zip(raw,[o,h,l,c,v]): new_row[col]=val
            self.df.loc[ts]=new_row
            self.df=self.df.sort_index()

        arr=np.column_stack([
            np.arange(len(self.df)),
            self.df["open"].values,
            self.df["close"].values,
            self.df["low"].values,
            self.df["high"].values
        ])
        self.candle_item.data=arr
        self.candle_item.generate_picture()

        if self.autoscroll:
            self.plot.enableAutoRange(axis='x',enable=True)
            self.plot.enableAutoRange(axis='y',enable=True)

    def _clear_trade_markers(self):
        for marker in self.trade_markers:
            try:
                self.plot.removeItem(marker)
            except Exception:
                pass
        self.trade_markers=[]

    def plot_trades(self, trades):
        self._clear_trade_markers()

        if self.df is None or not trades:
            return

        index_lookup={ts:idx for idx,ts in enumerate(self.df.index)}

        for trade in trades:
            timestamp=trade.get("time")
            if timestamp is None:
                continue

            if isinstance(timestamp,str):
                timestamp=pd.to_datetime(timestamp)
            elif not isinstance(timestamp,pd.Timestamp):
                timestamp=pd.Timestamp(timestamp)

            timestamp=timestamp.replace(second=0,microsecond=0)
            idx=index_lookup.get(timestamp)
            if idx is None:
                continue

            price=float(trade.get("price", float('nan')))
            if np.isnan(price):
                continue

            is_buy=trade.get("type")=="BUY"
            symbol="t1" if is_buy else "t"
            color="#00E676" if is_buy else "#FF5252"

            marker=self.plot.plot(
                [idx],[price],
                pen=None,
                symbol=symbol,
                symbolBrush=color,
                symbolPen=pg.mkPen(color),
                symbolSize=12
            )
            self.trade_markers.append(marker)

    def plot_indicator(self, series, color="#ffaa00", width=2):
        if series is None or len(series)==0: return
        x=np.arange(len(series))
        y=series.values
        item=self.plot.plot(x,y,pen=pg.mkPen(color,width=width))
        self.indicator_items.append(item)

    def clear_indicators(self):
        for it in self.indicator_items:
            try: self.plot.removeItem(it)
            except: pass
        self.indicator_items=[]
# ================================================================
# SECTION 10 — INSTRUMENT HEADER (TRADINGVIEW LOOK)
# ================================================================

class InstrumentHeader(QWidget):
    def __init__(self, symbol=DEFAULT_SYMBOL, interval=DEFAULT_INTERVAL):
        super().__init__()
        self.setObjectName("InstrumentHeader")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 16, 24, 12)
        layout.setSpacing(18)

        self.symbol_label = QLabel(symbol)
        self.symbol_label.setObjectName("HeaderSymbol")

        self.interval_label = QLabel(interval.upper())
        self.interval_label.setObjectName("HeaderInterval")

        self.price_label = QLabel("-")
        self.price_label.setObjectName("HeaderPrice")

        self.change_label = QLabel("0.00%")
        self.change_label.setObjectName("HeaderChange")

        self.status_label = QLabel("Waiting for data…")
        self.status_label.setObjectName("HeaderStatus")

        layout.addWidget(self.symbol_label)
        layout.addWidget(self.interval_label)
        layout.addWidget(self.price_label)
        layout.addWidget(self.change_label)
        layout.addStretch(1)
        layout.addWidget(self.status_label)

    def update_quote(self, price=None, change_pct=None, interval=None):
        if price is not None:
            self.price_label.setText(f"{price:,.3f}")
        if change_pct is not None:
            sign_color = "#26a69a" if change_pct >= 0 else "#ef5350"
            self.change_label.setStyleSheet(f"color: {sign_color}; font-weight: 600;")
            self.change_label.setText(f"{change_pct:+.2f}%")
        if interval:
            self.interval_label.setText(interval.upper())

    def set_status(self, text):
        self.status_label.setText(text)

# ================================================================
# SECTION 11 — TSI INDICATOR PANEL
# ================================================================

class IndicatorPanel(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.plot = pg.PlotWidget()
        self.plot.setBackground('#050A12')
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.getAxis('left').setPen(pg.mkPen('#394252'))
        self.plot.getAxis('bottom').setPen(pg.mkPen('#394252'))
        self.plot.getAxis('left').setTextPen(pg.mkPen('#A7B4C6'))
        self.plot.getAxis('bottom').setTextPen(pg.mkPen('#A7B4C6'))
        layout.addWidget(self.plot)

        self.tsi_item = None
        self.signal_item = None
        self.zero_line = None

        self.df = None

    def plot_tsi(self, df):
        self.df = df.copy()

        if self.tsi_item:
            self.plot.removeItem(self.tsi_item)
        if self.signal_item:
            self.plot.removeItem(self.signal_item)
        if self.zero_line:
            self.plot.removeItem(self.zero_line)

        x = np.arange(len(df))

        self.tsi_item = self.plot.plot(
            x, df["TSI"].values,
            pen=pg.mkPen('#2962FF', width=2)
        )

        self.signal_item = self.plot.plot(
            x, df["TSI_SIGNAL"].values,
            pen=pg.mkPen('#E91E63', width=2)
        )

        self.zero_line = self.plot.addLine(
            y=0,
            pen=pg.mkPen('#787B86', width=1)
        )

        self.plot.enableAutoRange()

    def update_live_tsi(self, ind):
        if self.df is None:
            return

        self.df.iloc[-1, self.df.columns.get_loc("TSI")] = ind["TSI"]
        self.df.iloc[-1, self.df.columns.get_loc("TSI_SIGNAL")] = ind["TSI_SIGNAL"]

        x = np.arange(len(self.df))

        if self.tsi_item:
            self.tsi_item.setData(x, self.df["TSI"].values)

        if self.signal_item:
            self.signal_item.setData(x, self.df["TSI_SIGNAL"].values)

# ================================================================
# SECTION 11 — STRATEGY EDITOR PANEL
# ================================================================

class StrategyEditor(QWidget):
    backtest_requested = Signal()
    apply_strategy_requested = Signal(dict)
    start_paper_requested = Signal(dict)
    stop_paper_requested = Signal()

    def _format_float(self, value):
        text = f"{value:.6f}".rstrip('0').rstrip('.')
        return text or "0"

    def _consume_float(self, box, fallback, minimum=None, maximum=None):
        text = box.text().strip()
        try:
            value = float(text)
        except ValueError:
            value = fallback
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        box.setText(self._format_float(value))
        return value

    def _consume_int(self, box, fallback, minimum=1, maximum=None):
        text = box.text().strip()
        try:
            value = float(text)
        except ValueError:
            value = fallback
        value = int(round(value))
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        box.setText(str(value))
        return value

    def _save(self):
        current = self.cfg.copy()
        self.cfg["symbol"] = DEFAULT_SYMBOL
        self.cfg["interval"] = DEFAULT_INTERVAL
        self.cfg["maType"] = self.ma_type_box.currentText()

        self.cfg["longLen"] = self._consume_int(self.longLen_box, current["longLen"], 1)
        self.cfg["shortLen"] = self._consume_int(self.shortLen_box, current["shortLen"], 1)
        self.cfg["signalLen"] = self._consume_int(self.signalLen_box, current["signalLen"], 1)

        tp_val = self._consume_float(self.tp_box, current["tp_percent"], minimum=0)
        if tp_val > 1:
            tp_val /= 100
        self.cfg["tp_percent"] = tp_val
        self.tp_box.setText(self._format_float(tp_val))

        sl_val = self._consume_float(self.sl_box, current["sl_percent"], minimum=0)
        if sl_val > 1:
            sl_val /= 100
        self.cfg["sl_percent"] = sl_val
        self.sl_box.setText(self._format_float(sl_val))

        self.cfg["maxTradeDays"] = self._consume_int(self.days_box, current["maxTradeDays"], 1)
        self.cfg["maLength"] = self._consume_int(self.maLen_box, current["maLength"], 1)
        self.cfg["trendSlopeLen"] = self._consume_int(self.slopeLen_box, current["trendSlopeLen"], 1)
        self.cfg["minSlope"] = self._consume_float(self.minSlope_box, current["minSlope"], minimum=0)

        self.cfg["slippage_pct"] = self._consume_float(self.slip_box, current["slippage_pct"], minimum=0)
        self.cfg["fee_pct"] = self._consume_float(self.fee_box, current["fee_pct"], minimum=0)
        self.cfg["partial_fill_steps"] = self._consume_int(self.partial_steps_box, current["partial_fill_steps"], 1)
        self.cfg["latency_ms"] = self._consume_int(self.latency_box, current["latency_ms"], 0)

        ConfigManager.save(self.cfg)

    def __init__(self):
        super().__init__()
        self.cfg = ConfigManager.load()

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)
        layout.setSpacing(10)
        self.setMinimumWidth(320)

        layout.addWidget(QLabel("Symbol (locked):"))
        self.symbol_box = QLineEdit(DEFAULT_SYMBOL)
        self.symbol_box.setReadOnly(True)
        self.symbol_box.setEnabled(False)
        self.symbol_box.setProperty("locked", "true")
        layout.addWidget(self.symbol_box)

        layout.addWidget(QLabel("Interval (locked):"))
        self.interval_box = QLineEdit(DEFAULT_INTERVAL)
        self.interval_box.setReadOnly(True)
        self.interval_box.setEnabled(False)
        self.interval_box.setProperty("locked", "true")
        layout.addWidget(self.interval_box)

        layout.addWidget(QLabel("MA Type:"))
        self.ma_type_box = QComboBox()
        self.ma_type_box.addItems(["EMA", "SMA", "WMA"])
        self.ma_type_box.setCurrentText(self.cfg["maType"])
        layout.addWidget(self.ma_type_box)
        self.ma_type_box.currentIndexChanged.connect(self._save)

        def make_double_validator(minimum=0.0, maximum=1000.0, decimals=6):
            validator = QDoubleValidator(minimum, maximum, decimals, self)
            validator.setNotation(QDoubleValidator.StandardNotation)
            return validator

        def make_int_validator(minimum=1, maximum=10000):
            return QIntValidator(int(minimum), int(maximum), self)

        def num_input(label, key, validator=None, as_float=False):
            layout.addWidget(QLabel(label))
            initial = self._format_float(self.cfg[key]) if as_float else str(self.cfg[key])
            box = QLineEdit(initial)
            box.setAlignment(Qt.AlignRight)
            if validator:
                box.setValidator(validator)
            layout.addWidget(box)
            box.editingFinished.connect(self._save)
            return box

        self.longLen_box = num_input("TSI Long Length:", "longLen", make_int_validator(1, 500))
        self.shortLen_box = num_input("TSI Short Length:", "shortLen", make_int_validator(1, 500))
        self.signalLen_box = num_input("TSI Signal Length:", "signalLen", make_int_validator(1, 500))
        self.tp_box = num_input("Take Profit %:", "tp_percent", make_double_validator(0.0, 100.0), as_float=True)
        self.sl_box = num_input("Stop Loss %:", "sl_percent", make_double_validator(0.0, 100.0), as_float=True)
        self.days_box = num_input("Max Trade Days:", "maxTradeDays", make_int_validator(1, 60))
        self.maLen_box = num_input("Trend MA Length:", "maLength", make_int_validator(1, 500))
        self.slopeLen_box = num_input("Slope Lookback:", "trendSlopeLen", make_int_validator(1, 200))
        self.minSlope_box = num_input("Min Slope:", "minSlope", make_double_validator(0.0, 100.0), as_float=True)

        self.slip_box = num_input("Slippage %:", "slippage_pct", make_double_validator(0.0, 5.0), as_float=True)
        self.fee_box = num_input("Fee %:", "fee_pct", make_double_validator(0.0, 5.0), as_float=True)
        self.partial_steps_box = num_input("Partial Fill Steps:", "partial_fill_steps", make_int_validator(1, 20))
        self.latency_box = num_input("Latency (ms):", "latency_ms", make_int_validator(0, 20000))

        self.btn_backtest = QPushButton("Run Backtest")
        self.btn_apply = QPushButton("Apply Strategy")
        self.btn_start_paper = QPushButton("Start Paper Trading")
        self.btn_stop_paper = QPushButton("Stop Paper Trading")
        self.btn_stop_paper.setEnabled(False)

        layout.addWidget(self.btn_backtest)
        layout.addWidget(self.btn_apply)
        layout.addWidget(self.btn_start_paper)
        layout.addWidget(self.btn_stop_paper)

        self.btn_backtest.clicked.connect(lambda: self.backtest_requested.emit())
        self.btn_apply.clicked.connect(self._emit_apply)
        self.btn_start_paper.clicked.connect(self._emit_start_paper)
        self.btn_stop_paper.clicked.connect(self._emit_stop_paper)

    def get_params(self):
        return self.cfg.copy()

    def set_paper_buttons_state(self, running):
        self.btn_start_paper.setEnabled(not running)
        self.btn_stop_paper.setEnabled(running)

    def _emit_start_paper(self):
        self._save()
        self.start_paper_requested.emit(self.get_params())

    def _emit_stop_paper(self):
        self.stop_paper_requested.emit()

    def _emit_apply(self):
        self._save()
        self.apply_strategy_requested.emit(self.get_params())

# ================================================================
# SECTION 13 — BACKTEST RESULTS PANEL
# ================================================================

class BacktestResults(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignTop)

        self.pnl_label = QLabel("Total PnL: -")
        self.winrate_label = QLabel("Win Rate: -")
        self.mdd_label = QLabel("Max Drawdown: -")
        self.trades_label = QLabel("Trades: -")
        self.tp_label = QLabel("TP Exits: -")
        self.sl_label = QLabel("SL Exits: -")

        for lbl in [
            self.pnl_label, self.winrate_label, self.mdd_label,
            self.trades_label, self.tp_label, self.sl_label
        ]:
            lbl.setStyleSheet("color: white; font-size: 13px;")
            layout.addWidget(lbl)

        self.table = QTableWidget()
        self.table.setColumnCount(5)
        self.table.setHorizontalHeaderLabels(
            ["Type", "Time", "Price", "PNL %", "Exit Reason"]
        )
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        self.status_label = QLabel("Status: Idle")
        self.status_label.setStyleSheet(
            "font-size: 13px; color: #A0A8C2; padding-top: 6px;"
        )
        layout.addWidget(self.status_label)

    def update_results(self, metrics, trades):
        self.pnl_label.setText(f"Total PnL: {metrics['total_pnl']:.2f}%")
        self.winrate_label.setText(f"Win Rate: {metrics['win_rate']:.2f}%")
        self.mdd_label.setText(f"Max Drawdown: {metrics['max_drawdown']:.2f}%")
        self.trades_label.setText(f"Trades: {metrics['num_trades']}")

        tp_count = sum(1 for t in trades if t.get("reason") == "TP")
        sl_count = sum(1 for t in trades if t.get("reason") == "SL")
        self.tp_label.setText(f"TP Exits: {tp_count}")
        self.sl_label.setText(f"SL Exits: {sl_count}")

        rows=[]
        entry_price=None

        for t in trades:
            typ=t["type"]
            tm=str(t["time"])
            pr=t["price"]
            ex=t.get("reason","")

            if typ=="BUY":
                entry_price=pr
                rows.append({"type":typ,"time":tm,"price":pr,"pnl":"","reason":""})

            elif typ=="SELL":
                pnl_text=""
                if entry_price is not None and entry_price != 0:
                    pnl_pct=((pr - entry_price) / entry_price)*100
                    pnl_text=f"{pnl_pct:.2f}%"
                    entry_price=None
                rows.append({"type":typ,"time":tm,"price":pr,"pnl":pnl_text,"reason":ex})

        self.table.setRowCount(len(rows))
        for i,r in enumerate(rows):
            for j,key in enumerate(["type","time","price","pnl","reason"]):
                self.table.setItem(i,j,QTableWidgetItem(str(r.get(key,""))))

    def set_status(self, text):
        self.status_label.setText(text)

# ================================================================
# RANDOM PARAM GENERATOR (USED IN OPTIMIZER)
# ================================================================

def random_params():
    return {
        "longLen": np.random.randint(5, 60),
        "shortLen": np.random.randint(3, 40),
        "signalLen": np.random.randint(5, 30),
        "tp_percent": np.random.uniform(0.001, 0.02),
        "sl_percent": np.random.uniform(0.001, 0.01),
        "maxTradeDays": np.random.randint(1, 10),
        "maType": np.random.choice(["EMA","SMA","WMA"]),
        "maLength": np.random.randint(10, 300),
        "trendSlopeLen": np.random.randint(2, 20),
        "minSlope": np.random.uniform(0.001, 0.1),
    }

# ================================================================
# SECTION 16 — LIVE PAPER TRADER
# ================================================================

class LivePaperTrader:
    def __init__(self, params):
        self.trades=[]
        self.params=params.copy()
        self.reset()

    def update_params(self, p):
        self.params=p.copy()

    def reset(self):
        self.position=None
        self.entry=None

    def process_live_tick(self, df):
        if len(df)<3: return

        row=df.iloc[-1]; prev=df.iloc[-2]
        long_signal = (row["TSI"]>row["TSI_SIGNAL"]) and (prev["TSI"]<=prev["TSI_SIGNAL"])

        tp=self.params["tp_percent"]
        sl=self.params["sl_percent"]

        if self.position is None and long_signal:
            self.position="LONG"
            self.entry=row["close"]
            self.trades.append({
                "type":"BUY","time":row.name,"price":self.entry
            })
            return

        if self.position=="LONG":
            tp_price=self.entry*(1+tp)
            sl_price=self.entry*(1-sl)

            exit_reason=None
            exit_price=None

            if row["low"]<=sl_price:
                exit_reason="SL"
                exit_price=sl_price
            elif row["high"]>=tp_price:
                exit_reason="TP"
                exit_price=tp_price

            if exit_reason:
                self.position=None
                self.entry=None
                self.trades.append({
                    "type":"SELL",
                    "time":row.name,
                    "price":exit_price,
                    "reason":exit_reason
                })

# ================================================================
# OPTIMIZER — MULTIPROCESS
# ================================================================

def optimizer_trial(args):
    df,_=args
    p=random_params()
    trades,_,metrics=backtest_strategy(df.copy(),p)
    return p,trades,metrics

class OptimizerWorker(QObject):
    finished=Signal(dict,list,dict)
    progress=Signal(int)

    def __init__(self, df, trials=2000):
        super().__init__()
        self.df=df
        self.trials=trials
        self._abort=False

    def stop(self):
        self._abort=True

    def run(self):
        maxw=max(1,int(os.cpu_count()*0.9))
        print(f"[OPTIMIZER] Using {maxw}/{os.cpu_count()} cores")

        best_params=None
        best_metrics=None
        best_trades=None

        args=[(self.df,i) for i in range(self.trials)]

        with concurrent.futures.ProcessPoolExecutor(max_workers=maxw) as pool:
            futures=[pool.submit(optimizer_trial,a) for a in args]

            for i,f in enumerate(concurrent.futures.as_completed(futures),start=1):
                if self._abort:
                    print("[OPT] Aborted")
                    return

                p,tr,m=f.result()
                if best_metrics is None or m["total_pnl"]>best_metrics["total_pnl"]:
                    best_params=p; best_metrics=m; best_trades=tr

                self.progress.emit(i)

        self.finished.emit(best_params,best_trades,best_metrics)

# ================================================================
# SECTION 14 — MAIN WINDOW (SPLITTER-BASED FULL RESIZE)
# ================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("TradingView-Lite Terminal")

        screen = QApplication.primaryScreen()
        if screen:
            geometry = screen.availableGeometry()
            width_limit = max(1, int(geometry.width() * 0.66))
            height_limit = max(1, int(geometry.height() * 0.66))
        else:
            width_limit = 1280
            height_limit = 800

        self._normal_max_size = QSize(width_limit, height_limit)
        self._normal_min_size = QSize(min(1180, width_limit), min(720, height_limit))
        self.setMinimumSize(self._normal_min_size)
        self.setMaximumSize(self._normal_max_size)
        self.resize(self._normal_max_size)

        self.cfg=ConfigManager.load()
        self.setStyleSheet(TRADINGVIEW_QSS)

        central=QWidget()
        self.setCentralWidget(central)
        root=QVBoxLayout(central)
        root.setContentsMargins(0,0,0,0)
        root.setSpacing(0)

        self.header = InstrumentHeader(DEFAULT_SYMBOL, DEFAULT_INTERVAL)

        self.chart = ChartWidget()
        self.indicator_panel = IndicatorPanel()
        self.strategy_panel = StrategyEditor()
        self.results_panel = BacktestResults()

        chart_split = QSplitter(Qt.Vertical)
        chart_split.addWidget(self.chart)
        chart_split.addWidget(self.indicator_panel)
        chart_split.setSizes([780,220])
        chart_split.setChildrenCollapsible(False)

        side_split = QSplitter(Qt.Vertical)
        side_split.addWidget(self.strategy_panel)
        side_split.addWidget(self.results_panel)
        side_split.setSizes([600,400])
        side_split.setChildrenCollapsible(False)

        content_split = QSplitter(Qt.Horizontal)
        content_split.addWidget(chart_split)
        content_split.addWidget(side_split)
        content_split.setStretchFactor(0,3)
        content_split.setStretchFactor(1,1)
        content_split.setChildrenCollapsible(False)

        root.addWidget(self.header)
        root.addWidget(content_split, 1)

        self.strategy_panel.backtest_requested.connect(self.run_backtest)
        self.strategy_panel.apply_strategy_requested.connect(self.apply_strategy)
        self.strategy_panel.start_paper_requested.connect(self.start_paper_trading)
        self.strategy_panel.stop_paper_requested.connect(self.stop_paper_trading)

        self.stream=None
        self.df=None
        self.paper_trader=None
        self.paper_trading_active=False
        self.optimizer_trials = 2000

        self.load_symbol(DEFAULT_SYMBOL, DEFAULT_INTERVAL)
    # ============================================================
    # PARAM EXTRACTOR
    # ============================================================
    def extract_strategy_params(self):
        return {
            "longLen": self.cfg["longLen"],
            "shortLen": self.cfg["shortLen"],
            "signalLen": self.cfg["signalLen"],
            "tp_percent": self.cfg["tp_percent"],
            "sl_percent": self.cfg["sl_percent"],
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
        self.header.set_status("Loading SOL candles…")
        df = fetch_historical_klines(symbol, interval)

        df = df.astype({
            "open": float, "high": float, "low": float,
            "close": float, "volume": float
        })

        _, df = run_tsi_strategy(df, **self.extract_strategy_params())
        self.df = df

        last_close = df["close"].iloc[-1]
        prev_close = df["close"].iloc[-2] if len(df) > 1 else last_close
        change_pct = ((last_close - prev_close) / prev_close * 100) if prev_close else 0
        self.header.update_quote(last_close, change_pct, interval)

        self.chart.plot_dataframe(df)
        self.indicator_panel.plot_tsi(df)
        self.start_stream(symbol, interval)

    # ============================================================
    # APPLY STRATEGY
    # ============================================================
    def apply_strategy(self, params=None):
        if params is not None:
            self.cfg.update(params)
            ConfigManager.save(self.cfg)

        if self.df is None or self.df.empty:
            return

        trades, df = run_tsi_strategy(self.df.copy(), **self.extract_strategy_params())
        self.df = df

        self.chart.clear_indicators()
        self.chart.plot_indicator(df["TREND_MA"], "#ffaa00")
        self.indicator_panel.plot_tsi(df)
        if not self.paper_trading_active:
            self.chart.plot_trades(trades)

        if self.paper_trader:
            self.paper_trader.update_params(self.extract_strategy_params())

    # ============================================================
    # RUN OPTIMIZER
    # ============================================================
    def run_backtest(self):
        if self.df is None or self.df.empty:
            self.results_panel.set_status("Status: No data to backtest")
            return

        print("[OPT] Starting optimizer...")
        self.results_panel.set_status("Status: Running backtest…")

        self.opt_thread = QThread()
        self.opt_worker = OptimizerWorker(self.df.copy(), trials=self.optimizer_trials)
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
        total = getattr(self.opt_worker, "trials", self.optimizer_trials)
        self.results_panel.set_status(
            f"Status: Running backtest… {min(n, total)}/{total}"
        )
        print(f"[OPT] Completed {n}/{total}")

    # ============================================================
    # OPTIMIZER COMPLETE
    # ============================================================
    def optimizer_finished(self, params, trades, metrics):
        print("[OPT] COMPLETE")

        if not params or not metrics:
            self.results_panel.set_status("Status: Backtest failed")
            return

        print("Best Params:", params)
        print(f"Best PnL: {metrics['total_pnl']:.2f}%")

        self.cfg.update(params)
        ConfigManager.save(self.cfg)

        print("[OPT] Applying optimized parameters...")
        self.apply_strategy(self.cfg)

        trades = trades or []
        self.results_panel.update_results(metrics, trades)
        self.chart.plot_trades(trades)
        self.results_panel.set_status("Status: Backtest complete")

        print("[OPT] Optimized parameters ACTIVE")

    # ============================================================
    # START WEBSOCKET
    # ============================================================
    def start_stream(self, symbol, interval):
        if self.stream:
            self.stream.stop()

        self.stream = CandleStream(symbol, interval)
        self.stream.new_candle.connect(self.on_live_candle)
        self.stream.start()
        self.header.set_status("Connecting to Binance stream…")

    def start_paper_trading(self, params):
        if params:
            self.cfg.update(params)
            ConfigManager.save(self.cfg)

        trading_params = self.extract_strategy_params()

        if self.paper_trader is None:
            self.paper_trader = LivePaperTrader(trading_params)
        else:
            self.paper_trader.update_params(trading_params)
            self.paper_trader.reset()
            self.paper_trader.trades.clear()

        self.paper_trading_active = True
        self.strategy_panel.set_paper_buttons_state(True)
        self.chart.plot_trades(self.paper_trader.trades)

    def stop_paper_trading(self):
        if self.paper_trader:
            self.paper_trader.reset()
            self.paper_trader.trades.clear()
        self.paper_trading_active = False
        self.strategy_panel.set_paper_buttons_state(False)
        self.chart.plot_trades([])
        self.apply_strategy()

    # ============================================================
    # LIVE CANDLE HANDLER
    # ============================================================
    def on_live_candle(self, candle):
        self.header.set_status("LIVE • Binance")
        ts = pd.to_datetime(candle["time"]).replace(second=0, microsecond=0)
        o = float(candle["open"])
        h = float(candle["high"])
        l = float(candle["low"])
        c_ = float(candle["close"])
        v = float(candle["volume"])

        raw_cols = ["open", "high", "low", "close", "volume"]

        new_bar = ts not in self.df.index

        # update / append row
        if not new_bar:
            self.df.loc[ts, raw_cols] = [o, h, l, c_, v]
        else:
            new_row = {col: np.nan for col in self.df.columns}
            for col, val in zip(raw_cols, [o, h, l, c_, v]):
                new_row[col] = val
            self.df.loc[ts] = new_row
            self.df = self.df.sort_index()

        # RECALCULATE recent indicators efficiently
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

        # Filter plotting frame
        plot_df = self.df.dropna(subset=[
            "open", "high", "low", "close", "volume",
            "TSI", "TSI_SIGNAL"
        ])

        # UPDATE CHART
        self.chart.update_live_candle({
            "time": ts,
            "open": o,
            "high": h,
            "low": l,
            "close": c_,
            "volume": v,
            "is_closed": candle.get("is_closed", True)
        })

        # UPDATE LIVE TSI PANEL
        if len(plot_df) > 0:
            latest_values = {
                "TSI": plot_df["TSI"].iloc[-1],
                "TSI_SIGNAL": plot_df["TSI_SIGNAL"].iloc[-1]
            }

            if new_bar or candle.get("is_closed", False):
                self.indicator_panel.plot_tsi(plot_df)
            else:
                self.indicator_panel.update_live_tsi(latest_values)

            last_close = plot_df["close"].iloc[-1]
            prev_close = plot_df["close"].iloc[-2] if len(plot_df) > 1 else last_close
            change_pct = ((last_close - prev_close) / prev_close * 100) if prev_close else 0
            self.header.update_quote(last_close, change_pct, self.cfg["interval"])

        if self.paper_trading_active and self.paper_trader:
            self.paper_trader.process_live_tick(self.df)
            self.chart.plot_trades(self.paper_trader.trades)

    def changeEvent(self, event):
        if event.type() == QEvent.WindowStateChange:
            if self.windowState() & Qt.WindowFullScreen:
                self.setMaximumSize(Qt.QWIDGETSIZE_MAX, Qt.QWIDGETSIZE_MAX)

            else:
                self.setMaximumSize(self._normal_max_size)
        super().changeEvent(event)


# ================================================================
# SECTION 15 — ENTRY POINT
# ================================================================
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
