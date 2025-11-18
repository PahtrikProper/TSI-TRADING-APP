# TSI Trading App

TSI Trading App is a desktop trading terminal built with Python + PySide6.
It delivers TradingView-style charting, a SOL-only True Strength Index (TSI)
strategy, historical backtesting, multiprocessing optimization, and both paper
and manual live controls through a dedicated remote panel.

<img width="1920" height="1080" alt="Screen Shot 2025-11-16 at 7 34 55 pm" src="https://github.com/user-attachments/assets/40da1083-e0bf-4c4f-8831-66f5052f245c" />

---

## Features

### Real-Time Market Data
- Locked SOL/USDT WebSocket kline streaming from Binance
- Automatic reconnection & candle replacement
- Clean OHLC dataframe maintenance

### TradingView-Style Charting
- High-performance candlesticks (pyqtgraph)
- Trend MA overlay + TSI indicator subpanel
- Buy/Sell markers for backtests and paper/manual trades
- Auto-scaling chart stack with splitter layout

### Remote Control Panel
- Start/stop paper trading & live trading hooks
- Launch optimizer, manual BUY/SELL buttons, apply parameters, exit
- Buttons stay in sync with the strategy state

### TSI Strategy Engine
- TradingView-compatible TSI + signal calculations
- EMA/SMA/WMA trend filter and slope checks
- Configurable TP/SL, trade-per-day guard, and stop-loss enforcement

### Backtesting & Optimizer
- Historical evaluation with PnL, Win Rate, Max Drawdown, trade table
- Multiprocessing randomized optimizer (2,000 trials by default)
- Best parameters persisted to `strategy_config.json`

### Paper Trading Engine
- Executes strategy on live candles with TP/SL exits
- Tracks trades and plots them on the chart

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
```

### Install dependencies
```bash
pip install PySide6 pyqtgraph pandas numpy requests websocket-client
```

### Run the application

```bash
python main.py
```

The repo also includes `TSI TRADING APP.py`, `SOL_ONLY_TRADER_FULL.py`, and
`WIP.PY` which mirror the same build for archival/testing purposes.

---

## Configuration

Settings are stored in `strategy_config.json` (auto-created on first run) and
contain the locked SOL/USDT interval plus TSI/trend/execution parameters.
The optimizer overwrites this file with the best-performing set when it finishes.

---

## Project Structure
```
TSI-TRADING-APP/
│
├── main.py                   # Primary application entry point
├── TSI TRADING APP.py        # Full build copy (legacy naming)
├── SOL_ONLY_TRADER_FULL.py   # Distribution copy
├── WIP.PY                    # Working copy used during development
├── strategy_config.json      # Generated on first run (not committed)
└── README.md
```

---

## Architecture Overview

### Data Flow
1. Fetch historical SOL/USDT candles
2. Compute indicators & plot chart/TSI panels
3. Start Binance WebSocket
4. Update dataframe + indicators per candle
5. Drive paper trading, manual markers, and optimizer updates

### Strategy Flow
- Compute TSI + signal crossover
- Apply trend MA + slope filters
- Enforce daily trade limit and TP/SL exits
- Emit BUY/SELL events for backtests and live/paper trading

### Optimizer Flow
- Randomize parameter sets
- Run multiprocessing backtests
- Track highest PnL and emit status updates
- Save/apply best parameters to chart and config

---

## Roadmap
- Native live trading integration (REST/WebSocket order placement)
- Additional indicators (ATR, RSI, MACD, Supertrend)
- Trade/export tooling and playback
- Theming & multi-symbol dashboards (post SOL-only phase)

---

## License
MIT License. See `LICENSE`.

---

## Notes
- Research/education use only
- No API keys or account trading included
- Always validate strategies before live deployment
