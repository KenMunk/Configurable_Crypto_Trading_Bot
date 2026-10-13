# Project Documentation

This folder holds all of the project's documentation. The [project README](../README.md) gives the overview and a quick start. The pages below go into depth.

## Guides: how to use the tools

| Page | What it covers |
|---|---|
| [simulator.md](simulator.md) | **Simulator guide.** Covers installing and launching the simulator, every control and results tab, the candle cache, and exactly how trades are filled, charged and taxed. It also covers what a backtest leaves out, scripting backtests in Python, and troubleshooting. |
| [trader.md](trader.md) | **Trader guide.** Covers every `.env` setting, creating a view-only Coinbase API key, and running the engine and keeping it running. It also covers the status dashboard and its security, the ledger, the serial link, the GPU helper, the trading safety switches, and troubleshooting. |
| [model_configuration.md](model_configuration.md) | **Model configuration guide.** Covers creating a profile step by step, and every field with its default and whether the simulator uses it. It also covers flash-crash buys, the friction screen with a worked example, and what validation does and doesn't catch. This page was formerly `config_file_structure.md`. |

## References: for looking things up

| Page | What it covers |
|---|---|
| [indicator_reference.md](indicator_reference.md) | **Indicators and signal rules.** This page has ten sections: (1) a quick start; (2) defining indicators, with periods in candles or durations and warm-up; (3) price fields; (4) the catalog of all 24 indicators, with parameters and formulas; (5) the rule language and its functions; (6) how signals become trades; (7) eight strategy recipes; (8) every validation message and its fix; (9) migrating old-format profiles; (10) how the charts draw indicators. |
| [reference_links.md](reference_links.md) | Links to the Coinbase Developer Platform and Kraken API documentation. |

## Design documents: why the project is built this way

| Page | What it covers |
|---|---|
| [quant_infrastructure_blueprint.md](quant_infrastructure_blueprint.md) | **The founding specification.** Its five sections cover: (1) the workstation and headless-node architecture, and the friction, value-harvesting and network-isolation principles; (2) the original engine code listing; (3) storage and memory budgets; (4) the LAN firewall rules; (5) the hardware cutover procedure. The [project README](../README.md#the-quant-infrastructure-blueprint) summarizes it and shows what has been built so far. |
| [system_config_structure.md](system_config_structure.md) | **The planned system configuration file:** exchange connections, fee refresh schedules and global safeguards. It is not built yet; the local `.env` file holds these settings today. |

## Suggested reading order

- **New to the project:** read the [project README](../README.md) quick start, then [simulator.md](simulator.md), [model_configuration.md](model_configuration.md) and [indicator_reference.md](indicator_reference.md).
- **Setting up a node or the API key:** read [trader.md](trader.md).
- **Understanding the architecture:** read [quant_infrastructure_blueprint.md](quant_infrastructure_blueprint.md), then [system_config_structure.md](system_config_structure.md).

## Adding a page

When you add a page, also link it from this index and from the **Documentation** section of the [project README](../README.md#documentation). Use relative links, such as `[trader.md](trader.md#5-the-status-dashboard)`, so that they work both on GitHub and in a local editor.
