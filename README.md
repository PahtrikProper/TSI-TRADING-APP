# TSI Trading App

TSI Trading App is a desktop trading application built with Python and PySide6.  
It provides TradingView-style charting, real-time Binance market data, TSI indicator analysis, historical backtesting, and a multiprocessing strategy optimizer.

<img width="1920" height="1080" alt="Screen Shot 2025-11-16 at 7 34 55 pm" src="https://github.com/user-attachments/assets/40da1083-e0bf-4c4f-8831-66f5052f245c" />



The project is designed as a self-contained quantitative research and visualization terminal focused on the True Strength Index (TSI) trend-following system.

---

## Features

### Real-Time Market Data
- Live WebSocket Kline streaming from Binance
- Automatic reconnection
- Accurate candle append/replace handling
- Clean, corruption-free OHLC dataframe management

### TradingView-Style Charting
- High-performance candlestick renderer (pyqtgraph)
- Indicator overlays (Trend MA)
- Buy/Sell markers
- Dedicated TSI subpanel
- Auto-scaling and auto-scrolling

### TSI Strategy Engine
- TradingView-compatible TSI calculations
- Trend filter using EMA/SMA/WMA
- Slope filter and min-slope requirement
- Max trades-per-day control
- Take-profit system

### Backtesting Framework
- Automatic TSI + trend filter evaluation over historical data
- PnL, Win Rate, Max Drawdown metrics
- Detailed trade list with per-trade PnL
- Equity curve calculation

### Multiprocessing Optimizer
- Randomized parameter sweeps
- Uses multiple CPU cores
- Automatically selects best parameter set
- Automatically applies best set to the live chart
- Saves best parameters as new persistent defaults

### Paper Trading Engine
- Executes TSI strategy on live WebSocket data
- BUY on signal cross
- SELL on take-profit
- Logs trades in memory

### Paper Trading Order Execution Simulator
- Slippage
- Bid/ask spread modeling
- Maker/taker fees
- Partial fills
- Latency simulation

### JSON Configuration System
- Persistent settings stored in `strategy_config.json`
- Strategy parameters and execution parameters saved automatically
- Optimizer writes new defaults on completion

### Symbol Selector
- Full Binance USDT symbol list
- Searchable filter
- Click-to-load
- Automatic WebSocket restart on symbol change

---

## Installation

### Requirements
```

Python 3.9+
PySide6
pyqtgraph
pandas
numpy
requests
websocket-client

````

### Install dependencies
```bash
pip install PySide6 pyqtgraph pandas numpy requests websocket-client
````

### Run the application

```bash
python tsi_trading_app.py
```

---

## Configuration

Settings are stored in:

```
strategy_config.json
```

This file is created automatically on first run.
It contains:

* Symbol
* Time interval
* TSI parameters
* Trend filter parameters
* Execution simulator settings
* Optimizer defaults

The optimizer writes new defaults when it completes.

---

## Project Structure

```
TSI-Trading-App/
│
├── tsi_trading_app.py            # Main application file
├── strategy_config.json          # Auto-generated configuration
└── README.md
```

---

## Architecture Overview

### Data Flow

1. Fetch historical candles from Binance REST
2. Compute indicators
3. Plot chart and TSI panel
4. Start WebSocket kline stream
5. Update dataframe and indicators on each live candle
6. Trigger:

   * paper trading
   * live indicator updates
   * chart updates

### Strategy Flow

* Compute TSI + signal
* Compute trend filter MA + slope
* Identify crossovers
* Apply daily trade limiter
* Trigger BUY and SELL events

### Optimizer Flow

* Randomize parameter sets
* Run multiple strategy backtests
* Track best PnL
* Emit result to main GUI
* Save & apply best settings

---

## Roadmap

Planned enhancements:

* Live Paper Trading (Binance API integration)
* Strategy alerts
* Additional indicators (ATR, RSI, MACD, Supertrend)
* CSV/Excel export for backtests
* Replay mode for historical candles
* Theme customization
* Multi-symbol dashboards

---

## License

MIT License.
See `LICENSE` file for details.

---

## Notes

* This project is intended for research and educational purposes.
* No API keys or account trading features are included by default.
* Always backtest and validate strategies before using them live.


