# System Configuration Structure and Meaning

This document explains the shared system-level configuration used across all trading models. Unlike the model config files, the system config is not strategy-specific. It defines platform-wide settings that affect how the bot connects to exchanges, refreshes operational data, and enforces cross-model runtime policies.

## Purpose

System configuration files define:
- exchange API connection settings
- shared fee refresh cadence
- local runtime behavior and network settings
- operational defaults that apply to every model
- global safeguards that are not strategy-specific

These settings are intended to be consistent across the entire trading system, regardless of which model is loaded.

## Separation from model configuration

The model config file defines the trading logic for one asset or strategy. The system config defines the environment in which the strategy runs.

Examples:
- Model config: BTC trend logic, risk thresholds, entries, exits
- System config: exchange fee refresh schedule, API connectivity, local LAN port, global execution state

This keeps model files focused on strategy decisions and keeps shared infrastructure concerns centralized.

## Example system config

```json
{
  "system": {
    "name": "Quantitative Trading Cluster",
    "environment": "LOCAL",
    "live_execution_active": false,
    "lan_port": 8080
  },
  "exchange_connections": {
    "coinbase": {
      "enabled": true,
      "api_key": "",
      "api_secret": "",
      "passphrase": "",
      "base_url": "https://api.coinbase.com",
      "fee_refresh_interval": "WEEKLY",
      "fee_refresh_time_utc": "00:00:00",
      "fee_buffer_multiplier": 1.25,
      "fallback_commission_rate": 0.005
    },
    "kraken": {
      "enabled": false,
      "api_key": "",
      "api_secret": "",
      "base_url": "https://api.kraken.com",
      "fee_refresh_interval": "WEEKLY",
      "fee_refresh_time_utc": "00:00:00",
      "fee_buffer_multiplier": 1.25,
      "fallback_commission_rate": 0.005
    }
  },
  "global_safeguards": {
    "hard_stop_enabled": true,
    "max_global_drawdown": 0.12,
    "minimum_execution_gate": 0.05
  }
}
```

## Field-by-field meaning

### `system`

This section contains the shared runtime environment details.

- `name`: a descriptive name for the trading cluster or deployment
- `environment`: deployment environment such as `LOCAL`, `TEST`, or `PRODUCTION`
- `live_execution_active`: whether live order routing is allowed system-wide
- `lan_port`: local HTTP port used for the internal dashboard or monitoring endpoint

### `exchange_connections`

This block defines each exchange the system may use.

#### Coinbase / Kraken entries

Each exchange object can contain:
- `enabled`: whether the exchange is active for this system
- `api_key`: API key from the local environment or `.env` file
- `api_secret`: secret credential for the exchange API
- `passphrase`: optional exchange passphrase where required
- `base_url`: API base URL for the exchange
- `fee_refresh_interval`: how often the fee schedule should be refreshed; common values are `WEEKLY`, `MONTHLY`, or `MANUAL`
- `fee_refresh_time_utc`: the scheduled time to refresh fees in UTC
- `fee_buffer_multiplier`: multiplier applied to the live exchange fee rate for conservative modeling
- `fallback_commission_rate`: fee rate used when the live rate cannot be fetched

These settings are intentionally global because they affect all models sharing the same orchestration layer.

### `global_safeguards`

This section outlines shared safety controls that do not belong to a single model.

- `hard_stop_enabled`: globally enables the emergency shutdown guard
- `max_global_drawdown`: maximum acceptable combined drawdown limit across strategies
- `minimum_execution_gate`: minimum profitability threshold before execution is allowed system-wide

## Why this is separate from model config

The model config is not the right place for refresh timing and shared platform policy because those settings are not specific to one asset or strategy. A weekly fee refresh schedule should not be duplicated in every model file; it should exist once in the system config and apply uniformly.

This separation prevents:
- repeated configuration drift across models
- inconsistent fee refresh timing
- accidental strategy-specific override of platform-wide rules
- overloading model files with shared runtime settings

## Template vs private config

The project should keep public templates in version control and store real system settings locally.

Examples:
- repository template: `system/.example/system_config.example.json`
- local private runtime file: a local untracked `system_config.json` or equivalent private config path

The same principle applies here as with model configs: keep secrets and operational details out of the repository.

## Best practice

When setting up a new environment:
1. define the exchange connections in the system config
2. set the fee refresh cadence once at the system level
3. keep credentials in the local `.env` file or another private secret store
4. leave model config files focused on asset-specific strategy logic

## Summary

The system config is the shared operational layer of the project. It defines how the platform connects to exchanges, refreshes fee data, and enforces infrastructure-wide policies. Model config files remain focused on trading behavior for a single asset or strategy, which keeps the architecture cleaner and more maintainable.
