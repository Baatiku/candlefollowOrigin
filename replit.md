# BestaBot

Automated IQ Option trading bot with a Double Martingale ladder-recovery strategy and a React dashboard.

## Stack
- **Backend:** Python / FastAPI (`src/api.py`, `src/strategies/double_martingale.py`)
- **Frontend:** React + Vite (`frontend/`)
- **Data:** JSONL flat file (`data/`) with optional PostgreSQL (env-configured)

## Run (requires dependencies installed)
```
cd frontend && npm install && npm run build && cd .. && mkdir -p data && cd src && uvicorn api:app --host 0.0.0.0 --port 5000
```
The workflow currently fails because `uvicorn` is not installed in the Nix environment — install it via pip or add to requirements before starting.

## Required environment variables
| Variable | Description |
|---|---|
| `IQ_EMAIL` | IQ Option account email |
| `IQ_PASSWORD` | IQ Option account password |
| `IQ_ACCOUNT_TYPE` | `PRACTICE` or `REAL` (default: PRACTICE) |
| `LICENSE_KEY` | Bot license key (`BESTA-FREE-TRIAL` for 72h trial) |
| `BOT_API_KEY` | Optional: random string to protect write API endpoints |

## Key source files
| File | Purpose |
|---|---|
| `src/strategies/double_martingale.py` | Core trading engine (6500+ lines) |
| `src/api.py` | FastAPI REST + WebSocket server |
| `src/config.py` | All configuration constants and env-var overrides |
| `src/pair_health.py` | Per-asset win-rate suspension + score reweighting |
| `src/market_metrics.py` | Candle analysis: entry snapshots, CI, ER, movement score |
| `src/trade_log.py` | Trade persistence (PostgreSQL + JSONL fallback) |
| `src/risk_governor.py` | Drawdown / risk limits |

## Trading strategy docs
- `FRAMEWORK.md` — trading rules (source of truth)
- `docs/PROGRESS_LOG.md` — full change history
- `docs/PRODUCT_PLAN.md` — distribution roadmap

## User preferences
- Keep existing project structure and stack.
- Do not touch `double_martingale.py` staking/ladder/martingale math unless explicitly requested.
- Shadow mode must default to `True` for any new empirical gate before real-money enforcement.
