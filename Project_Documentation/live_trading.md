# Live Trading Guide (live_trader.py)

`live_trader.py` trades your models on Coinbase Advanced Trade: any number of models, on any number of currencies, at the same time. Several models can trade the same currency, each with its own money.

Its API key decides how it trades. A key that can trade means **live trading with real orders**. A view-only key means paper trading, kept in separate records (see [Paper and live](#5-paper-and-live)).

> **Risk warning: this is a speculative automated trader.** It buys and sells cryptocurrency on its own, without asking you first. Cryptocurrency prices are extremely volatile, and **you can lose your entire investment**. Backtested, simulated and paper-traded results do not predict future returns. Nothing in this project is financial advice. Use it at your own risk, and only with money you can afford to lose completely.
>
> Run every model on paper first, start live trading with small amounts, and read [What can still go wrong](#12-what-can-still-go-wrong).

---

## Contents

1. [How it decides: the simulator is the brain](#1-how-it-decides-the-simulator-is-the-brain)
2. [Quick start (paper)](#2-quick-start-paper)
3. [Your data folder](#3-your-data-folder)
4. [The settings file](#4-the-settings-file)
5. [Paper and live](#5-paper-and-live)
6. [Orders](#6-orders)
7. [Safeguards](#7-safeguards)
8. [The ledger, restarts and reviews](#8-the-ledger-restarts-and-reviews)
9. [The web portal: watching from any machine on your network](#9-the-web-portal-watching-from-any-machine-on-your-network)
10. [Running it on its own: background, status and stop](#10-running-it-on-its-own-background-status-and-stop)
11. [Power cuts, crashes and moving to another machine](#11-power-cuts-crashes-and-moving-to-another-machine)
12. [What can still go wrong](#12-what-can-still-go-wrong)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. How it decides: the simulator is the brain

A **deployment** is one model profile given its own capital. It keeps its own cash, coins, trades and tax reserve in the trading ledger.

The trader does not have its own copy of the trading rules. At every candle close, it replays each deployment's model through the simulator's backtest (`backtest.run_backtest`), from the moment the model was deployed to the candle that just closed. That replay applies everything the simulator does:

- the indicators and entry and exit rules
- market regimes
- the friction screen
- hard, trailing and profit-lock stops
- the drawdown limit and its cooldown
- the loss halt
- `max_orders_per_day`
- the tax reserve

The replay's open position is the model's **target**. The trader then brings the real account into line with it:

| Replay | Real account | What the trader does |
|---|---|---|
| Just entered (on the latest candle) | In cash | Buys, sized from the deployment's own cash (see [Sizing](#sizing)) |
| Out | Holding | Sells everything the deployment holds, giving the replay's exit reason |
| Entered a while ago | In cash | Skips the entry; the model waits for its next one (see [Late entries](#late-entries-are-skipped-not-chased)) |
| Same as the account | Same | Nothing |

**Between candle closes**, the trader checks live prices every `poll_seconds`. It compares the best bid with the stops and profit target that the backtest would apply on the current candle: hard stop, trailing stop, profit-lock trail, drawdown stop and loss-halt floor. When the price reaches one, the trader sells at once, as the backtest's intrabar stops assume. At the next candle close, the replay sees the same dip in the candle's low and exits too, so the two stay in step. If the replay then re-enters on that candle's close (its entry signal is still on), the account re-enters with it.

**A restart replays the same history and reaches the same decisions.** No trading state lives only in memory.

### Why this design

Every rule the simulator has, and every rule it gains later, works live the moment it exists, with no second implementation to drift out of line. Backtest, semi-live paper trading and live trading all make the same decision on the same candles.

A full 10-year replay of a 15-minute model takes about 1–2 seconds, and coarser candles take a few milliseconds. Even dozens of models finish well inside one candle. The trader checks prices between replays, so a slow batch can't delay a stop.

### What differs from a backtest

- **The replay's money is simulated; the account's is real.** The replay decides *when* to trade, using its own simulated cash, so its drawdown limit and loss halt measure the model's simulated value. The account decides *how much*: each buy is sized from the deployment's real cash. The two normally track each other closely. The dashboard shows both, so you can see if they drift apart.
- **Fills are real.** A live order fills at the book's price, not exactly at the candle close the backtest assumed.
- **Flash-crash ladders are not traded live yet.** A profile's `strategy.flash_crash_buy` is left out, and the deployment's notes say so. This also means stops act during crashes, as they do in synthetic and stress runs.
- **The fee tier is read from Coinbase** at start-up and every `fee_refresh_hours`, as the simulator does at the start of each run.

---

## 2. Quick start (paper)

1. Create the live trader's two files in your [data folder](#3-your-data-folder), outside the program:

   ```bash
   python live_trader.py --init
   ```

   It prints where they are, such as `C:\Users\you\AppData\Local\ConfigurableCryptoTradingBot\LiveTrader\`:

   - `live_trader.env`: **the live trader's own configuration**, for its Coinbase API key and its portal settings ([below](#its-own-configuration-file)).
   - `live_config/live_trading.json`: your deployments ([section 4](#4-the-settings-file)).

2. Put a **view-only** Coinbase key in `live_trader.env` (see [Creating a Coinbase API key](trader.md#3-creating-a-coinbase-api-key)), and list your deployments in `live_trading.json`. With a view-only key the trader paper-trades. A key that can trade would make it trade live.

3. Check it once. This loads every deployment, reads prices and fees, makes the first decisions, prints the status and exits:

   ```bash
   python live_trader.py --once
   ```

4. Run it:

   ```bash
   python -X utf8 live_trader.py
   ```

   It runs until Ctrl+C. At start-up it prints the [web portal](#9-the-web-portal-watching-from-any-machine-on-your-network)'s address, such as `http://192.168.1.20:8080/portal`, which you can open from any machine on your network. To leave it running on its own, use `python live_trader.py --background` instead (see [Running it on its own](#10-running-it-on-its-own-background-status-and-stop)), and `--stop` to stop it.

A new deployment makes its first decision when the first full candle after it was deployed closes. On daily candles, that can be the next day.

---

## 3. Your data folder

The live trader keeps everything it writes, and everything it needs to carry on, in one **data folder outside the program**. Removing, updating or reinstalling the program never touches it. Your trading records, settings and backups stay available, including as plain spreadsheet files.

| System | Default data folder |
|---|---|
| Windows | `%LOCALAPPDATA%\ConfigurableCryptoTradingBot\LiveTrader` (for example `C:\Users\you\AppData\Local\...`). It is deliberately not in OneDrive or Documents: a database must never be synced while it is open. |
| macOS | `~/Library/Application Support/ConfigurableCryptoTradingBot/LiveTrader` |
| Linux | `~/.local/share/configurable-crypto-trading-bot/live-trader` (or under `$XDG_DATA_HOME`) |

`python live_trader.py --status` and `--init` print where it is. It holds:

| In the data folder | What it is |
|---|---|
| `live/` and `paper/` | **Each mode's own records**, kept apart so paper trades are never mixed with real money ([section 5](#5-paper-and-live)). Each holds the four entries below: `reports/`, `ledger/`, `backups/` and `profiles/`. |
| `reports/` | **Your records as CSV files**, readable in any spreadsheet without the program: `trades.csv`, `orders.csv`, `deployments.csv`, `value_history.csv` and `events.csv`. They are rewritten after every order, at every backup and at every stop. A file you have open in a spreadsheet is updated the next time. |
| `live_config/live_trading.json` | Your settings ([section 4](#4-the-settings-file)), and the `HALT` switch beside them when you create it |
| `ledger/trading_ledger.db` | The trading ledger, the trader's own record ([section 8](#8-the-ledger-restarts-and-reviews)) |
| `backups/` | Recovery bundles ([section 11](#11-power-cuts-crashes-and-moving-to-another-machine)) |
| `tax/tax_profile.json` | Your tax profile. On the first start it is copied in from the program's `tax/` folder, and the trader uses the copy from then on. |
| `logs/` | `live_trader.log` and `supervisor.log` |
| `cache/candles.db` | The trader's price history. On the first start it is copied from the simulator's, so years of candles needn't be downloaded. Safe to delete. |
| `profiles/` | Profile files restored from a bundle, for reference |
| `run/` | The running trader's lock and process records |
| `live_trader.env` | **The live trader's own configuration**: its API key (which decides live or paper), the web portal settings and `FORCE_PAPER_TRADING`. See [below](#its-own-configuration-file). It never goes into a backup. |
| `README.txt` | What this folder is, for anyone who finds it |

### Its own configuration file

The simulator and the live trader read **different configuration files**:

| Tool | Reads | Typically holds |
|---|---|---|
| Simulator, Model Lab, model search | `.env` in the program folder ([Trader guide](trader.md#2-setting-up-the-env-file)) | A view-only key: they never trade |
| Live trader | `live_trader.env` in the data folder | The key it should trade with: view-only for paper trading, View and Trade for live trading |

The live trader never reads the simulator's `.env`, and refuses it even if `--env-file` points at it. The key in `live_trader.env` decides whether it trades real money, so a key meant for the simulator can never change that, and a trading key never has to sit in the simulator's file. If `live_trader.env` is missing, the trader stops and says to run `--init`, which writes it from [its template](../live_config/.example/live_trader.env.example) and never overwrites an existing one.

It takes the same `COINBASE_*` key settings as `.env`. A relative `COINBASE_KEY_FILE` path starts from the data folder, so a key file can sit beside it. It also takes `FORCE_PAPER_TRADING`, `LAN_PORT` and the `DASHBOARD_*` settings. `--status` shows which file is in use.

### Choosing another folder

```bash
python live_trader.py --set-data-dir "D:\TraderData"      # Windows
python live_trader.py --set-data-dir ~/trader-data         # Linux and macOS
```

- **The data moves with it.** If the current folder holds data and the new one doesn't, everything is moved there.
- **Your choice is remembered,** in a small file outside the program (`%APPDATA%\ConfigurableCryptoTradingBot\data_location.json` on Windows, `~/.config/configurable-crypto-trading-bot/` on Linux, beside the data folder on macOS), so every later command uses the folder.
- **After reinstalling the program**, or on a machine where the folder was copied over, point it at the folder again. If the folder already holds trading data, the trader simply starts using it:

  ```bash
  python live_trader.py --set-data-dir "D:\TraderData"
  ```

The trader must be stopped while you change the folder. For a single command, `--data-dir <folder>` overrides the folder, as does the `LIVE_TRADER_DATA_DIR` environment variable. `--config`, `--ledger` and `--log-file` can still point at single files elsewhere.

**Data from an earlier version.** An earlier version of the trader kept its ledger, settings, backups and logs inside the program folder. The first start of this version moves them into the data folder, once. Nothing else in the program folder is touched: the simulator's `config/` profiles and `data/` stay where they are, because they belong to the simulator.

**Uninstalling.** Delete the program folder. The data folder stays until you delete it yourself.

---

## 4. The settings file

`live_config/live_trading.json` in your [data folder](#3-your-data-folder). `--init` puts the template there. Run with `--config` to use another file.

```json
{
  "settings": {"poll_seconds": 15},
  "global_safeguards": {"max_global_drawdown": 0.25, "max_order_value": 2500},
  "deployments": [
    {"id": "btc_trend",   "profile": "config/BTC-USD/btc_best_01.json", "capital": 1000},
    {"id": "btc_meanrev", "profile": "config/BTC-USD/btc_balanced_02.json", "capital": 500},
    {"id": "eth_best",    "profile": "best:ETH-USD", "capital": 750},
    {"id": "doge_best",   "profile": "best:DOGE-USD", "capital": 250, "enabled": false}
  ]
}
```

### Deployments

| Field | Default | Meaning |
|---|---|---|
| `id` | required | The deployment's name in the ledger: letters, digits, `-` and `_`. It is permanent. Its money and history stay with this name. |
| `profile` | required | A profile file (relative paths start at `--base-dir`, the program folder, by default), or `best:<PRODUCT>` for the currency's recorded best model. |
| `capital` | required | The money this deployment starts with. It is set aside in the ledger, not moved anywhere. |
| `live` | none | No longer used: the API key decides between live and paper for every deployment (see [Paper and live](#5-paper-and-live)). A deployment that still has it says so in its notes. |
| `enabled` | `true` | `false` stops new entries. A deployment holding a position keeps managing its exits until it is flat (it is *winding down*). A deployment that was never deployed isn't created. |
| `wait_for_fresh_signal` | `true` | At deployment, don't buy into an entry signal that is already on; wait for it to switch off and fire again, as the simulator's option of the same name does. It is fixed when the deployment is created. |
| `order_style` | the profile's `execution.order_style` | `MARKET` or `LIMIT_WITH_FRICTION_MARGIN`; see [Orders](#6-orders). |

**What is fixed when a deployment is created:** its profile (a copy is stored in the ledger), its mode (paper or live), its start time and `wait_for_fresh_signal`. The replay always starts from the same moment with the same rules, so its decisions never rewrite themselves.

If you edit the profile file afterwards, the deployment keeps trading the stored copy, and its notes say the file has changed. **To trade a changed profile, deploy it under a new `id`.** Set the old one's `enabled` to `false` to let it wind down.

A deployment removed from the settings while it holds a position also winds down, using its stored profile.

### settings

| Field | Default | Meaning |
|---|---|---|
| `poll_seconds` | `15` | How often prices are checked (at least 5). |
| `fee_refresh_hours` | `24` | How often the Coinbase fee tier is read again. `0` reads it only at start-up. |
| `order_timeout_seconds` | `30` | How long to wait for an order to finish before cancelling it. |
| `sell_fallback_to_market` | `true` | Sell what a limit sell couldn't fill at the market (see [Orders](#6-orders)). |
| `quote_currency` | `USD` | The currency whose free balance is checked before a live buy. |
| `backup_hours` | `24` | How often a recovery bundle is written to the data folder's `backups/` while it runs (also at every start and clean stop); `0` only at start and stop. See [Backups](#backups). |
| `backup_keep` | `14` | How many automatic bundles to keep. |

### global_safeguards

These are described in [Safeguards](#7-safeguards).

---

## 5. Paper and live

**The API key decides.** Every time it starts, the trader asks Coinbase what its API key may do:

| The key | The trader |
|---|---|
| Can **trade** (View and Trade permissions) | **Trades live.** Every deployment places real orders with real money. |
| Is **view-only** | **Paper-trades** every deployment. Nothing reaches the exchange. |
| Can trade, but `--paper` is given (or `FORCE_PAPER_TRADING=true` is in `live_trader.env`) | Paper-trades. Use this to trial models on a machine that has a trading key. |
| Can't be checked, because there is no network | **Doesn't trade.** It retries every minute and starts once Coinbase answers. In the background, that means it starts trading on its own when the network comes back. |
| Is refused by Coinbase (HTTP 401 or 403) | Stops, saying so. Fix the key in `live_trader.env`. |

The trader says which mode it chose and why: at start-up, in the log, at the top of `--status`, and on the [web portal](#9-the-web-portal-watching-from-any-machine-on-your-network) ("LIVE TRADING: real orders", or "Paper trading (view-only key)"). It warns if the key can also **transfer** funds out: the trader never needs that, so give a live node a key with View and Trade only.

**Paper** fills are simulated at the live mid price. The profile's `slippage_buffer` is applied against them, and they pay its taker fee rate, the same costs the simulator charges.

### Live and paper records are kept apart

Paper trading never writes to the records live trading uses. Each mode has its own area in the [data folder](#3-your-data-folder):

```text
<data folder>/live/     ledger, reports, backups: real money only
<data folder>/paper/    ledger, reports, backups: paper money only
```

Each ledger is also stamped with its mode the first time it is used. A paper trader refuses the live ledger, and a live trader refuses the paper one, even if a mistaken `--ledger` points at it. So a paper position can never become a real sell order, and real holdings are never paper-traded.

Changing the key changes the mode, and with it the records. A deployment that was paper-trading starts afresh in the live area, with its `capital` and no position, when a trading key is first used, and the paper records stay as they were. The settings (`live_config/live_trading.json`), the HALT switch, your tax profile, the logs and the price history are shared by both modes.

### Before you use a key that can trade

- Run every deployment on paper first, with a view-only key, and read [What can still go wrong](#12-what-can-still-go-wrong).
- Start with small `capital` and a low `max_order_value`.
- Create the trading key for this one machine, with **View** and **Trade** only, never **Transfer**. If you can, restrict it to the machine's IP address.
- To stop live trading quickly: `python live_trader.py --stop`, or create the `HALT` file to stop new entries while exits carry on. To make sure it can't trade, swap the key for a view-only one; the next start then paper-trades.

---

## 6. Orders

| Style | Buy | Sell |
|---|---|---|
| `MARKET` | A market order for the amount in dollars. | A market order for the coins held. |
| `LIMIT_WITH_FRICTION_MARGIN` | An immediate-or-cancel limit order at the best ask plus the profile's `slippage_buffer`. A fill never costs more slippage than the backtest charged. Whatever doesn't fill is left unfilled. | An immediate-or-cancel limit at the best bid minus `slippage_buffer`. With `sell_fallback_to_market`, any part it couldn't fill is then sold at the market, because an exit must happen. |

Sizes are rounded down to the product's size step. An amount below the product's smallest order is skipped (for an entry), or left in the ledger as dust (for an exit).

Every order is written to the ledger **before** it is sent, with a unique client order id. When it finishes, its outcome, the deployment's new position and any trade it closed are written together in one transaction. A crash at any moment leaves a record to recover from (see [Restarts](#8-the-ledger-restarts-and-reviews)).

### Sizing

A buy spends the deployment's cash, less its tax reserve and any tax treated as paid, times the replay's position fraction (`target_position_fraction`, or the active regime's). It is then capped by `max_order_value`. The tax reserve is worked out from the deployment's real closed trades under the profile's tax rule, the same rule the simulator uses (see `taxes.py`). Money the simulator would set aside for tax is never traded.

### Late entries are skipped, not chased

A buy is placed only while the replay's entry is fresh: within `max_entry_delay_candles` candles of the signal, and with the price within `max_entry_price_deviation` of the replay's entry price. Otherwise the entry is skipped and logged, and the model waits for its next entry. This covers the trader having been off when a model entered, and a price that has run away from the signal. **Sells are never skipped.**

---

## 7. Safeguards

The trader adds these on top of each model's own:

| Safeguard | Default | What it does |
|---|---|---|
| `HALT` file | none | Create `live_config/HALT` in the data folder, with any content, to stop **new entries** for every deployment at once. Exits continue. Delete the file to resume. |
| `max_global_drawdown` | `0.25` | If all deployments of one mode (paper or live) together fall this far from their peak, new entries halt for that mode until you run `--clear-halt <mode>`. The combined value is chained from returns, so adding or retiring a deployment isn't counted as a gain or a loss. `0` turns it off. |
| `liquidate_on_global_drawdown` | `false` | Also sell every position of that mode when the limit is hit. |
| `max_order_value` | `0` (none) | The most any one buy may spend. |
| `max_orders_per_day` | `50` | Live orders per UTC day, across all deployments; entries stop once it is reached. `0` turns it off. |
| `max_price_age_seconds` | `120` | No order is placed on a price older than this. |
| `max_entry_delay_candles` | `1` | See [Late entries](#late-entries-are-skipped-not-chased). |
| `max_entry_price_deviation` | `0.03` | See [Late entries](#late-entries-are-skipped-not-chased). |
| `balance_tolerance` | `0.001` | How far the exchange balance may fall short of the ledger before a deployment stops for review. |

Products that Coinbase lists as disabled, cancel-only or not online get no buys until they reopen.

---

## 8. The ledger, restarts and reviews

The trading ledger is `ledger/trading_ledger.db` in your [data folder](#3-your-data-folder), an SQLite file outside the program. You can choose another file with `--ledger`. The same records are kept as CSV files in the data folder's `reports/`. It holds:

- `deployments`: each deployment's stored profile, mode, start, cash, coins, cost basis and status
- `orders`: every order with its client and exchange ids, status, fill and fees
- `trades`: every closed trade with its profit or loss
- `events`: the log
- `equity`: each deployment's real value and its replay's value at each candle close
- `meta`: the combined-drawdown state

It uses full sync, so every order record reaches the disk. Real money is worth the extra disk writes, and orders are rare.

**On restart**, the trader settles any live order it didn't see finish. If the order has an exchange id, it asks Coinbase what filled and applies that to the deployment. If the order never got an id, it searches recent orders for its client order id.

Some situations stop a deployment for **review** (`NEEDS_REVIEW`). It then places no orders until you have checked the exchange:

- an order's outcome is unknown, such as a timeout or a server error
- an order the trader was sending doesn't show up on the exchange
- the exchange holds fewer coins than the ledger says the live deployments hold
- five orders in a row failed

To clear a review, run this while the trader runs or before you restart it:

```bash
python live_trader.py --clear-review <id>
```

A running trader picks the change up at its next cycle. It first settles the deployment's unfinished orders, and only then lets the deployment trade again.

---

## 9. The web portal: watching from any machine on your network

While the trader runs, it serves a monitoring page that any browser on your network can open:

```text
http://<the trader's machine>:8080/portal        e.g. http://192.168.1.20:8080/portal
```

The trader prints and logs the exact addresses at start-up. The port is `LAN_PORT` in `live_trader.env`, or `--port`. The page refreshes itself every 10 seconds and shows:

- whether live orders are allowed, the HALT file and any halts, and the fee tier
- the combined paper and live value and return, how many deployments hold a position, and how many need attention
- every deployment, grouped by currency: status, price, value, return, its position, whether its model is in or out (with a ≠ when the two differ), its nearest stop, and its notes
- a chart of the selected deployment's real value against its model replay's value; click a row to choose it
- recent trades, recent orders and the event log

It is **read-only**: nothing on it can place, halt or clear anything. It is one self-contained page, so nothing loads from the internet. It works on phones and in light and dark mode.

### Reaching it from other machines

The portal listens on every network interface (`DASHBOARD_HOST=0.0.0.0`), so other machines can reach it once the trader machine's firewall lets them in:

| Trader machine | Allow the port from your local network |
|---|---|
| Windows | Allow Python on **private** networks when Windows Defender Firewall asks. Or, in an elevated PowerShell: `New-NetFirewallRule -DisplayName "Live trader web portal" -Direction Inbound -Protocol TCP -LocalPort 8080 -Action Allow -Profile Private -RemoteAddress LocalSubnet`. Your network must be set to **Private** in Windows settings. |
| Linux (ufw) | `sudo ufw allow from 192.168.1.0/24 to any port 8080 proto tcp` (use your own subnet) |
| Linux (firewalld) | `sudo firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=192.168.1.0/24 port port=8080 protocol=tcp accept' && sudo firewall-cmd --reload` |
| macOS | Allow incoming connections for Python when asked, or under System Settings > Network > Firewall > Options. |

### Who can see it

| Setting in `live_trader.env` | Default | Effect |
|---|---|---|
| `DASHBOARD_HOST` | `0.0.0.0` | The address to listen on. `0.0.0.0` means every interface, so other machines can connect. `127.0.0.1` keeps the portal to the trader's own machine. `--host` overrides it. |
| `DASHBOARD_ALLOW_PUBLIC` | `false` | Only clients on private networks are answered: home and office LANs (10.x, 172.16–31.x, 192.168.x), link-local addresses and the machine itself. Anyone else gets `403`, even if a router forwards the port. Set `true` only behind your own VPN or reverse proxy. |
| `DASHBOARD_PASSWORD` | empty | When set, every page and endpoint asks for this password. The browser shows a sign-in box; any user name works. |

The portal uses plain HTTP, so a password set here crosses your network unencrypted. It keeps casual visitors out of a home network, but **never expose the port to the internet**. To watch from outside, use a VPN into your network.

### The data behind it

| Endpoint | Returns |
|---|---|
| `GET /api/live` | Every deployment: status, mode, cash, coins, value, return, tax reserve, fees, its real position and its replay's position, stops and target, whether the two agree (`in_sync`), and notes. Also the halts and the last 20 events. |
| `GET /api/live/trades?limit=N` | The latest closed trades (up to 500). |
| `GET /api/live/orders?limit=N` | The latest orders. |
| `GET /api/live/events?limit=N` | The latest log events. |
| `GET /api/live/equity?deployment=ID&limit=N` | A deployment's real value and its replay's value at each candle close, newest first. |
| `GET /api/status` | The node status. `monitored_assets` lists the currencies being traded, and `live_trading` lists the deployments needing review. |

From the command line, `python live_trader.py --status` lists the ledger's deployments without connecting to Coinbase, and `--once` runs one full cycle and prints the status table.

| Status | Meaning |
|---|---|
| `ACTIVE` | Trading. |
| `WINDING_DOWN` | Disabled or removed from the settings while holding a position; exits only. |
| `INACTIVE` | Disabled and flat. |
| `HALTED` | The model's own loss halt stopped it, as in the simulator. |
| `NEEDS_REVIEW` | Stopped until you clear it (see above). |
| `BLOCKED` | Couldn't start, for example because of a missing profile or a profile error. The note says why. |

---

## 10. Running it on its own: background, status and stop

The live trader is a stand-alone tool. It runs itself on Windows, Linux and macOS, without a service manager, task scheduler or anything else to install:

```bash
python live_trader.py --background     # start it; returns at once and prints the portal address
python live_trader.py --status         # running? since when? where is the portal? and every deployment
python live_trader.py --stop           # stop it cleanly, from any terminal, and wait until it has
```

On Windows, add `-X utf8` after `python` if you like; the trader's own log is UTF-8 either way.

**What `--background` does:**

- It starts the trader **detached from your terminal**: a new session on Unix, a detached process on Windows. Closing the terminal, or signing out of an SSH session, doesn't stop it.
- A small **supervisor** inside the tool runs the trader and **restarts it if it crashes**. It waits 10 seconds after the first crash, doubling the wait after each crash in a row up to 5 minutes. The wait resets once the trader has run for 10 minutes. It doesn't restart after a problem a restart can't fix, such as a broken settings file or another trader already running. It logs why and stops.
- It **never asks a question**. An SDK install or a `PATH` change is declined and logged.
- It **logs** to `logs/live_trader.log` in the data folder (UTF-8, rotated at 5 MB, five old files kept; change it with `--log-file`). The supervisor's restarts are logged to `logs/supervisor.log`.
- It keeps trying every minute **if Coinbase can't be reached** at start-up.
- It serves the [web portal](#9-the-web-portal-watching-from-any-machine-on-your-network), which is how you watch it.

**`--stop`** leaves a stop request in the data folder's `run/stop`. The trader notices it within a second, finishes the cycle it is in, closes the ledger and exits, and the supervisor exits with it. `--stop` waits until both are gone, up to three minutes. Because a file carries the request, stopping works the same on every system and needs no signals. Ctrl+C (and Ctrl+Break on Windows, SIGTERM on Unix) also stop a trader running in a terminal.

**Only one trader per data folder.** Whichever process is trading holds an operating-system lock on `run/live_trader.lock`. A second trader refuses to start rather than placing the same orders twice. The lock disappears with its process, however that process ends, so a crash never leaves a stale lock to clear by hand.

| Way to run | Use it for |
|---|---|
| `--background` | Normal running. It runs unattended, restarts itself, and you watch it on the portal. |
| `--headless` | Unattended, but in the current terminal: no questions, logs to the file, no restarts. Useful under another tool of your own choosing. |
| *(no flag)* | Watching it in a terminal. It prints the status table when you press Ctrl+C. |
| `--once` | One cycle, then the status table; a quick check. |

All of these use the same settings, ledger and portal, and accept `--data-dir`, `--config`, `--ledger`, `--host` and `--port`. `--background` passes them on to the trader it starts.

The trader doesn't start itself when the computer starts. After a reboot or a power cut, run `python live_trader.py --background` again and it resumes where it stopped (see [section 11](#11-power-cuts-crashes-and-moving-to-another-machine)).

The files in the data folder's `run/`:

| File | Holds |
|---|---|
| `live_trader.lock` | The lock held by the trading process |
| `supervisor.json`, `worker.json` | The supervisor's and the trader's process ids and start times, and the portal addresses; `--status` reads these |
| `stop` | A stop request, while one is pending |

---

## 11. Power cuts, crashes and moving to another machine

The trader is built to pick up where it left off. After a power cut or a crash, start it again with `python live_trader.py --background` and it resumes on its own. To move it, copy one backup file to the new machine and restore it there.

### What happens when it starts again

Everything the trader decides follows from two things it keeps on disk: the trading ledger, and each model's deployment time. So resuming takes no special mode. The steps run every time it starts, in this order:

1. **The clock is checked** against Coinbase's. A computer that lost power can come back with the wrong time, and candles and orders are timed by it. If it is more than 30 seconds out, the trader waits (retrying every minute) until the clock is set right.
2. **The outage is noted.** The ledger records a heartbeat every minute while the trader runs, and marks a clean stop. If the last run didn't stop cleanly, the trader logs when it was last seen and how long it was offline. The portal shows it ("Resumed after an outage: offline 3h 12m") for a week, and `--status` lists it.
3. **A backup is written** before anything trades, in case something goes wrong while catching up.
4. **The deployments are loaded from the ledger.** Each one trades the profile stored when it was deployed. A profile file that is missing, which is common on a new machine, doesn't stop it; its notes say it is using the stored copy.
5. **Unfinished orders are settled** from what Coinbase says happened to them (see [section 8](#8-the-ledger-restarts-and-reviews)).
6. **Coin balances are checked** against the ledger for live deployments.
7. **Every model is replayed** from when it was deployed, over price history that now includes the hours or days it missed (any missing candles are downloaded first). The account is then brought into line:

| While it was offline, the model... | On resuming, the trader... |
|---|---|
| exited (a stop, its exit rule, the drawdown limit) | **sells at once.** Sells are never skipped, so a position is never left open longer than it takes to restart. |
| entered | **skips the entry,** because it is stale, and waits for the model's next one. It never buys late into a move it missed. |
| did nothing | carries on as if it had never stopped. |

Taxes for any year that ended while it was offline are settled from the ledger's trades, exactly as if it had been running.

### Backups

A **recovery bundle** is one `.zip` file holding everything the trader needs to carry on:

- the trading ledger: every deployment's money, stored profile, orders and trades
- the settings, and the HALT switch if it was on
- your tax profile, because without it tax reserves would follow the example profile's salary
- the profile files the settings name
- optionally, the price history

It never holds `live_trader.env` or `.env`: your API keys stay on the machines you put them on.

| When | Where |
|---|---|
| Every `backup_hours` (24) while it runs, at every start, and at every clean stop | The data folder's `backups/live_trader_backup_<time>.zip`, the newest `backup_keep` (14) kept |
| Whenever you run `python live_trader.py --backup [file]` | That file, or the data folder's `backups/` |

`--with-candles` adds the price history (`data/candles.db`; tens of MB), so a new machine needn't download ten years of candles first. The ledger is copied with SQLite's own backup method, so a bundle is consistent even while the trader runs.

**Keep a copy somewhere else.** A backup on the same disk doesn't survive that disk. Copy the newest bundle from the data folder's `backups/` to another drive, a NAS or cloud storage now and then; it is one small file.

### Moving to a new machine

1. On the old machine, write a bundle and stop the trader:

   ```bash
   python live_trader.py --backup move.zip --with-candles
   python live_trader.py --stop
   ```

   If the old machine is gone, use the newest bundle from its data folder's `backups/`, or your copy elsewhere.

2. On the new machine, set up the project as in the [Quick start](#2-quick-start-paper): Python, the requirements and this repository. Run `python live_trader.py --init` after restoring (below) and put the live trader's API key in `live_trader.env`. If your Coinbase key is restricted to IP addresses, add the new machine's address on Coinbase first.

3. Restore the bundle and start:

   ```bash
   python live_trader.py --restore move.zip
   python live_trader.py --background
   ```

`--restore` refuses while a trader is running. It checks the bundle's ledger for damage before putting anything in place, and puts everything into this machine's data folder, moving any file it would replace into `backups/replaced_<time>/`, so nothing is lost. When the trader starts on the new machine, it resumes as described above. It notes the move ("this ledger last ran on old-pc"), and the portal shows it.

The same steps repair a machine whose disk failed: restore the newest bundle you have, and the trader catches up from there.

### A damaged ledger

SQLite with full sync survives power cuts in normal use. If the ledger file is still damaged, for example by a failing disk, the trader refuses to trade on it. It names the newest backup to restore:

```text
The trading ledger at ledger/trading_ledger.db is damaged: ...
Nothing will trade on it. Restore a backup with:
  python live_trader.py --restore backups/live_trader_backup_20261029_201416.zip
```

The background supervisor doesn't keep restarting a trader that stops for this reason.

**After restoring an older bundle,** the ledger doesn't know about trades made after the backup was written. For live deployments, compare the restored positions with your Coinbase account before trading. The balance check stops a deployment for review if the exchange holds fewer coins than the ledger.

---

## 12. What can still go wrong

- **Backtests are optimistic, and so is the replay.** It fills at candle closes and stop levels. Read [what the results do not include](simulator.md#what-the-results-do-not-include).
- **Stops are checked every `poll_seconds`, not placed on the exchange.** If the trader is down or cut off from the network, nothing protects an open position. Run it with [`--background`](#10-running-it-on-its-own-background-status-and-stop) so it restarts after a crash, and keep an eye on the portal. After the computer restarts, start it again. A fast fall between two checks also fills below the stop.
- **Coins shared with other activity.** The ledger attributes coins to deployments. If you trade the same coins by hand, the balance checks will stop deployments for review.
- **Taxes are estimated, not filed.** The reserve follows the simulator's rule and your tax profile.

---

## 13. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `No live trading settings at ...` | Run `python live_trader.py --init`; it puts the template in the data folder. If you moved the data folder, point the trader at it with `--set-data-dir`. |
| It paper-trades although you meant to trade live | The key is view-only, or `--paper` / `FORCE_PAPER_TRADING=true` is set. The start-up line, `--status` and the portal say which. |
| `can't reach Coinbase to check the API key ...; not trading` | No network yet. It retries every minute and starts by itself once it can. |
| `Coinbase refused the API key` | The key in `live_trader.env` is wrong, expired, or restricted to another IP address. |
| `The live trader's configuration file is missing` | Run `python live_trader.py --init` and put the live trader's API key in the `live_trader.env` it names. |
| `... is the simulator's configuration file` | `--env-file` points at the program folder's `.env`. Leave `--env-file` out; the live trader uses `live_trader.env` in its data folder. |
| `... is the paper trading ledger; a live trader won't use it` (or the other way round) | A `--ledger` points at the other mode's records. Leave `--ledger` out: each mode uses its own area of the data folder. |
| `BLOCKED: was created as a paper deployment; its mode can't change` | Deploy it under a new `id`. |
| `entry not taken: ... too long ago to follow` | The trader was off, or started, after the model entered. It waits for the next entry. |
| `entry not taken: the HALT file is present` | Delete `live_config/HALT` in the data folder. |
| `the paper deployments are halted: combined value fell ...` | Check what happened, then run `python live_trader.py --clear-halt paper` (or `live`). |
| A deployment is `NEEDS_REVIEW` | Read its note, check the order or balance on Coinbase, then run `--clear-review <id>`. |
| `UnicodeEncodeError` writing a log | Run with `python -X utf8`. Headless mode's own log file is always UTF-8. |
| The portal works on the trader's machine but not from another | The firewall is blocking the port, or Windows treats the network as Public. See [Reaching it from other machines](#reaching-it-from-other-machines). |
| `403 Only machines on private networks may use this dashboard` | The browser's address isn't on a private network, for example a phone on mobile data. Use a VPN into your network. |
| The portal asks for a password | `DASHBOARD_PASSWORD` is set in `.env`; any user name works. |
| `Could not listen on 0.0.0.0:8080` in the log | Another program (another trader?) uses the port. Stop it, or use `--port`. |
| `Another live trader is already running on this base directory` | One is running: `--status` shows it, and `--stop` stops it. |
| `The trading ledger ... is damaged` | Restore the named backup: `python live_trader.py --restore <bundle>` (see [A damaged ledger](#a-damaged-ledger)). |
| `could not start ... this computer's clock is ...s off Coinbase's` | Turn on automatic time in the system settings (or `sudo timedatectl set-ntp true` on Linux). The trader retries every minute. |
| `... tax_profile.json is missing, so tax reserves ... use the example profile's salary` | Restore your tax profile from a backup, or copy `tax/tax_profile.json` from the old machine. |
| A deployment's note says `it trades the copy stored in the ledger` | Its profile file isn't on this machine. That's fine; copy the file over only if you want the "profile changed" check back. |
| `--background` says it didn't start | Read `logs/live_trader.log` and `logs/supervisor.log` in the data folder; a settings problem is named there. Running `--once` in a terminal shows it too. |
