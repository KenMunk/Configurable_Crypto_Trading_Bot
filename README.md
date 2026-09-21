# Configurable Crypto Trading Bot

This repository is initialized from the quant infrastructure blueprint and provides a minimal local-first trading engine skeleton.

## Project structure

- `engine.py` – core trading engine and local dashboard
- `config/` – declarative strategy profiles
- `tests/` – verification tests for the initial infrastructure

## Runtime modes

This project now separates the runtime into two distinct tools:

- `engine.py` — production headless engine for live node execution
- `simulator.py` — simulation and strategy testing tool with a GUI interface for visual feedback

## Private configuration policy

The repository intentionally ignores the local `config/` directory so strategy models and runtime settings stay private. A template folder can be kept in the repo using a placeholder structure, but real strategy files must live only in the local, untracked configuration folder.

## Production engine

The production engine remains the headless runtime for the local node. It loads `.env` settings and uses them for exchange connectivity and run configuration.

## Simulator

The simulator is a separate tool intended for configuration testing and visual inspection. It exposes a small GUI window and accepts a JSON strategy profile for rapid evaluation.

Run it with:

```bash
python simulator.py
```

## Environment configuration

Create a local `.env` file to store API keys and runtime secrets. The repository ignores this file so credentials stay off version control.

Use the template in `.env.example` and copy it to `.env` before running the engine.

```bash
cp .env.example .env
```

Example values:

```env
COINBASE_API_KEY=your_coinbase_api_key
COINBASE_API_SECRET=your_coinbase_api_secret
COINBASE_PASSPHRASE=your_coinbase_passphrase
COINBASE_BASE_URL=https://api.coinbase.com
KRAKEN_API_KEY=your_kraken_api_key
KRAKEN_API_SECRET=your_kraken_api_secret
KRAKEN_BASE_URL=https://api.kraken.com
LAN_PORT=8080
LIVE_EXECUTION_ACTIVE=false
```

## Local validation

```bash
python -m pytest
```

## Notes

The architecture intentionally keeps the production environment local, uses SQLite for low-wear storage, and isolates the monitoring endpoint to the private LAN. The simulator operates independently to provide visual feedback without affecting live trading execution.
