"""Friction model: the real-world costs every simulated trade passes through.

Charged on each fill (both ends of a round trip):
- exchange commission, from the live Coinbase fee tier or the profile
- slippage, as a price penalty

Used only to screen entries, never charged at the moment of a trade:
- a liquidity buffer: a fraction (default 50%) of the round-trip trade fees plus
  the Bitcoin network transaction fee for moving the position on-chain
- the tax buffer on the remaining gain

Taxes themselves are settled by the backtest once a year on net realized gains.
"""

import json
from urllib.request import Request, urlopen

# A typical one-input, two-output native SegWit transaction.
BITCOIN_TX_VBYTES = 141
BITCOIN_FEE_URL = "https://mempool.space/api/v1/fees/recommended"
DEFAULT_LIQUIDITY_FEE_BUFFER = 0.5


def fetch_exchange_fee_rates(client):
    """Return the account's live Coinbase fee tier as {"maker", "taker", "tier"}."""
    tier = client.get_transaction_summary().fee_tier
    tier = tier if isinstance(tier, dict) else tier.to_dict()
    return {
        "maker": float(tier["maker_fee_rate"]),
        "taker": float(tier["taker_fee_rate"]),
        "tier": tier.get("pricing_tier", "unknown"),
    }


def fetch_bitcoin_network_fee(timeout=10):
    """Return the fee in BTC for a typical transaction confirmed within about half an hour."""
    request = Request(BITCOIN_FEE_URL, headers={"User-Agent": "ConfigurableCryptoTradingBot/1.0"})
    with urlopen(request, timeout=timeout) as response:
        rates = json.loads(response.read().decode("utf-8"))
    sats_per_vbyte = float(rates["halfHourFee"])
    return {"sats_per_vbyte": sats_per_vbyte, "btc": sats_per_vbyte * BITCOIN_TX_VBYTES / 1e8}


def fetch_friction_inputs(client, profiles):
    """Look up the live rates that one profile, or a list of profiles, asks for.

    Each rate is fetched at most once, however many profiles share it, and the result
    works for every profile: FrictionModel only uses the rates its own profile needs.
    Failures are recorded rather than raised; the model falls back to the profile's rates.
    """
    profiles = profiles if isinstance(profiles, list) else [profiles]
    inputs = {"exchange_fees": None, "exchange_fee_error": None,
              "network_fee": None, "network_fee_error": None}

    if any(_commission_source(profile.get("friction_model", {})) == "exchange_api" for profile in profiles):
        try:
            inputs["exchange_fees"] = fetch_exchange_fee_rates(client)
        except Exception as exc:  # Fall back to the configured rate; the result says why.
            inputs["exchange_fee_error"] = str(exc)

    if any(_base_currency(profile) == "BTC" for profile in profiles):
        try:
            inputs["network_fee"] = fetch_bitcoin_network_fee()
        except Exception as exc:
            inputs["network_fee_error"] = str(exc)
    return inputs


def _commission_source(friction):
    if not friction.get("exchange_fee_coverage_enabled", True):
        return "disabled"
    # Profiles written before commission_rate_source existed still look up live fees;
    # their exchange_commission_rate becomes the fallback.
    return str(friction.get("commission_rate_source", "exchange_api")).lower()


def _base_currency(profile):
    return str(profile.get("product_id") or "").partition("-")[0].upper()


class FrictionModel:
    def __init__(self, profile, inputs=None):
        inputs = inputs or {}
        friction = profile.get("friction_model", {})
        exit_rules = profile.get("strategy", {}).get("exit_signal", {})
        safeguards = profile.get("safeguards", {})

        self.commission_source = _commission_source(friction)
        fallback = float(friction.get("exchange_commission_rate", friction.get("fallback_commission_rate", 0.005)))
        live = inputs.get("exchange_fees")
        if self.commission_source == "disabled":
            maker = taker = 0.0
            self.fee_source_detail = "exchange fee coverage disabled"
        elif self.commission_source == "exchange_api" and live:
            maker, taker = live["maker"], live["taker"]
            self.fee_source_detail = f"Coinbase {live['tier']} tier (live)"
        else:
            maker = taker = fallback
            if self.commission_source == "exchange_api":
                reason = inputs.get("exchange_fee_error") or "live rates not fetched"
                self.fee_source_detail = f"profile fallback ({reason})"
            else:
                self.fee_source_detail = "profile"
        self.maker_rate = maker
        self.taker_rate = taker
        self.fee_buffer_multiplier = float(friction.get("fee_buffer_multiplier", 1.0))
        # The backtest fills immediately at the candle price, so both ends pay the taker rate.
        self.fill_fee_rate = taker * self.fee_buffer_multiplier

        self.slippage = float(friction.get("slippage_buffer", 0.0))
        self.tax_rate = float(friction.get("tax_buffer_rate", 0.0))
        self.liquidity_fee_buffer = float(friction.get("liquidity_fee_buffer", DEFAULT_LIQUIDITY_FEE_BUFFER))
        self.charges_network_fee = _base_currency(profile) == "BTC"
        network = inputs.get("network_fee") if self.charges_network_fee else None
        self.network_fee_btc = network["btc"] if network else 0.0
        if network:
            self.network_fee_detail = f"{network['sats_per_vbyte']:g} sat/vB x {BITCOIN_TX_VBYTES} vB (live)"
        elif self.charges_network_fee:
            self.network_fee_detail = inputs.get("network_fee_error") or "not fetched"
        else:
            self.network_fee_detail = "not applicable"

        self.profit_target = float(exit_rules.get("profit_lock_target", 0.0))
        self.minimum_net_profit = float(safeguards.get("minimum_net_profit_gate", 0.0))
        self.screen_enabled = bool(self.profit_target and "minimum_net_profit_gate" in safeguards)

    def network_fee_fraction(self, price, position_value):
        """The on-chain fee for moving the position, as a fraction of its value."""
        if not self.charges_network_fee or position_value <= 0:
            return 0.0
        return self.network_fee_btc * price / position_value

    def expected_net_return(self, price, position_value):
        """After-tax return if the trade reaches its profit target, net of all friction."""
        fee, slip = self.fill_fee_rate, self.slippage
        target_return = (1 - fee) ** 2 * (1 + self.profit_target) * (1 - slip) / (1 + slip) - 1
        liquidity = self.liquidity_fee_buffer * (2 * fee + self.network_fee_fraction(price, position_value))
        pre_tax = target_return - liquidity
        return pre_tax * (1 - self.tax_rate) if pre_tax > 0 else pre_tax

    def passes_screen(self, price, position_value):
        if not self.screen_enabled:
            return True
        return self.expected_net_return(price, position_value) >= self.minimum_net_profit

    def describe(self, price=None, position_value=None):
        summary = {
            "commission_source": self.fee_source_detail,
            "maker_rate": self.maker_rate,
            "taker_rate": self.taker_rate,
            "fill_fee_rate": self.fill_fee_rate,
            "slippage": self.slippage,
            "network_fee_btc": self.network_fee_btc,
            "network_fee_detail": self.network_fee_detail,
            "liquidity_fee_buffer": self.liquidity_fee_buffer,
            "tax_rate": self.tax_rate,
            "screen_enabled": self.screen_enabled,
            "profit_target": self.profit_target,
            "minimum_net_profit": self.minimum_net_profit,
        }
        if price is not None and position_value:
            summary["expected_net_return"] = self.expected_net_return(price, position_value)
        return summary
