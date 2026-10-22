"""Tax on a model's realized gains: how much each closed trade sets aside, and when it is paid.

Two rules, chosen in the profile's friction_model (see Project_Documentation/model_configuration.md):

    tax_loss_offset: true (default)   Losses offset gains within a tax year (the calendar year, UTC), set
                                      aside cautiously: each winning trade sets aside its full tax, and a
                                      losing trade releases the tax its loss would save, as far as tax is
                                      set aside. The reserve is held until the year ends; then the year's
                                      actual tax (on its net realized gain) is paid, and whatever was set
                                      aside beyond it goes back to trading cash. With tax_loss_carryforward
                                      (default true), a year that ends with a net loss carries it forward
                                      to offset later years' gains at their year end.

    tax_loss_offset: false            Each winning trade is taxed on its own and losses offset nothing;
                                      the month's reserve is paid at the start of the next month. (The
                                      simulator's rule before loss offsets; tax rules change.)

Only realized gains are taxed: a position still open, buy and hold included, owes nothing yet.
"""

import json
from datetime import datetime, timezone
from pathlib import Path


def _period_of(timestamp, yearly):
    date = datetime.fromtimestamp(timestamp, timezone.utc)
    return (date.year, 12) if yearly else (date.year, date.month)


def _start_of_next(period, yearly):
    year, month = period
    if yearly or month == 12:
        year, month = year + 1, 1
    else:
        month += 1
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp())


class TaxLedger:
    """The tax set aside on realized gains and the payments made, under one of the two rules above."""

    def __init__(self, rate, loss_offset=True, carryforward=True):
        self.rate = float(rate)
        self.loss_offset = bool(loss_offset)
        self.carryforward = bool(carryforward) and self.loss_offset
        self.yearly = self.loss_offset  # offsetting needs the whole year's trades, so it settles yearly
        self.reserved = 0.0  # set aside and not yet paid
        self.paid = 0.0
        self.loss_carried = 0.0  # net losses from earlier years still to offset (with carryforward)
        self.period = None  # the tax period (year, month) the reserve belongs to
        self.next_settlement = None
        self.net_gain = 0.0  # this period's net realized gain
        self.taxable_gain = 0.0  # this period's gains from winning trades (the per-trade rule taxes these)

    def _due(self):
        """The tax owed for the period so far, under the offset rule."""
        return self.rate * max(0.0, self.net_gain - self.loss_carried)

    def start(self, timestamp):
        """Begin the ledger at the first candle tested."""
        self.period = _period_of(timestamp, self.yearly)
        self.next_settlement = _start_of_next(self.period, self.yearly)

    def realize(self, pnl, timestamp, long_term=False):
        """Record a closed trade's gain or loss (`long_term` only matters to the progressive model); returns the tax to move from trading cash into the reserve
        (negative when a loss releases tax set aside earlier in the year back to trading cash)."""
        if self.period is None:
            self.start(timestamp)
        self.net_gain += pnl
        self.taxable_gain += max(pnl, 0.0)
        if pnl >= 0 or not self.loss_offset:
            # Out of caution, a gain always sets aside its full tax, whatever losses came before it.
            tax = max(pnl, 0.0) * self.rate
            self.reserved += tax
            return tax
        released = min(-pnl * self.rate, self.reserved)  # the tax this loss saves, as far as any is set aside
        self.reserved -= released
        return -released

    def settle(self, timestamp):
        """Pay every period that ended by `timestamp`: the payments made, oldest first. With loss offsets, each
        year pays its actual tax and its "refund" (what was set aside beyond that) goes back to trading cash."""
        payments = []
        while self.next_settlement is not None and timestamp >= self.next_settlement:
            tax = min(self._due(), self.reserved) if self.loss_offset else self.reserved
            payment = {"year": self.period[0], "month": self.period[1], "net_realized_gain": self.net_gain,
                       "taxable_gain": (max(0.0, self.net_gain - self.loss_carried) if self.loss_offset
                                        else self.taxable_gain),
                       "tax": tax, "refund": self.reserved - tax}
            if self.loss_offset:
                remaining = self.loss_carried - self.net_gain  # the loss left once this year's gains used it
                self.loss_carried = max(0.0, remaining) if self.carryforward else 0.0
                payment["loss_carried_forward"] = self.loss_carried
            self.paid += tax
            self.reserved = 0.0
            self.net_gain = self.taxable_gain = 0.0
            payments.append(payment)
            self.period = _period_of(self.next_settlement, self.yearly)
            self.next_settlement = _start_of_next(self.period, self.yearly)
        return payments

    def describe(self):
        """How tax is worked out, in a sentence for reports."""
        if not self.rate:
            return "no tax"
        if not self.loss_offset:
            return (f"{self.rate:.1%} of each profitable trade, losses offsetting nothing, paid at the start "
                    f"of the next month")
        carried = ("; a year's net loss is carried forward to offset later gains" if self.carryforward
                   else "; a year's net loss is not carried forward")
        return (f"{self.rate:.1%} of each year's net realized gain (losses offset gains{carried}): each profitable "
                f"trade sets aside its full tax and each losing trade releases what its loss saves, held until "
                f"the year ends, when the year's tax is paid and anything set aside beyond it is returned")


# --- the progressive model: federal and California income tax on top of a salary ------------------------

TAX_DIR = Path(__file__).parent / "tax"
RULES_FILE = TAX_DIR / "tax_rules.json"
PROFILE_FILE = TAX_DIR / "tax_profile.json"  # private: Git ignores it
EXAMPLE_PROFILE = TAX_DIR / "tax_profile.example.json"
FILING_STATUSES = ("single", "married_joint", "head_of_household")
LONG_TERM_SECONDS = 365 * 86400  # held more than a year: a long-term gain


def load_tax_profile(path=None):
    """The user's tax situation: tax/tax_profile.json, or the example's when there is none yet."""
    path = Path(path) if path else (PROFILE_FILE if PROFILE_FILE.exists() else EXAMPLE_PROFILE)
    profile = json.loads(path.read_text(encoding="utf-8"))
    if profile.get("filing_status", "single") not in FILING_STATUSES:
        raise ValueError(f"{path}: filing_status must be one of {', '.join(FILING_STATUSES)}")
    profile["source"] = str(path)
    return profile


def load_tax_rules(path=None):
    return json.loads(Path(path or RULES_FILE).read_text(encoding="utf-8"))["years"]


_codes = {}


def tax_code(profile_path=None):
    """The TaxCode for a tax profile (the private one by default) and the rules file, read again only when either
    file changes."""
    profile = Path(profile_path) if profile_path else (PROFILE_FILE if PROFILE_FILE.exists() else EXAMPLE_PROFILE)
    key = (str(profile), profile.stat().st_mtime, RULES_FILE.stat().st_mtime)
    if key not in _codes:
        _codes[key] = TaxCode(load_tax_rules(), load_tax_profile(profile))
    return _codes[key]


def _bracket_tax(amount, brackets, start=0.0):
    """Tax on the income from `start` to `start + amount` under [[upper bound, rate], ...] brackets."""
    tax, low, top = 0.0, 0.0, start + max(amount, 0.0)
    for upper, rate in brackets:
        high = float("inf") if upper is None else upper
        overlap = min(top, high) - max(start, low)
        if overlap > 0:
            tax += overlap * rate
        low = high
        if high >= top:
            break
    return tax


def _net_capital(short, long_):
    """Net short- and long-term results against each other, as the federal rules do: (short, long)."""
    if short < 0 <= long_:
        long_ += short
        short = 0.0
        if long_ < 0:
            short, long_ = long_, 0.0
    elif long_ < 0 <= short:
        short += long_
        long_ = 0.0
        if short < 0:
            short, long_ = 0.0, short
    return short, long_


class TaxCode:
    """A year's tax on trading: the federal (with the net investment income tax) and California tax it adds on
    top of the user's salary. `rules` are tax_rules.json's years and `profile` load_tax_profile()'s."""

    def __init__(self, rules, profile):
        self.rules, self.profile = rules, profile
        self.status = profile.get("filing_status", "single")
        self.jurisdictions = profile.get("jurisdictions", ["federal", "california"])

    def salary(self, year):
        return float(self.profile.get("salary_by_year", {}).get(str(year), self.profile.get("salary", 0.0)))

    def _rules(self, year, jurisdiction):
        """That jurisdiction's rules for the year, or the nearest year's that has them."""
        years = sorted((int(y) for y, rules in self.rules.items() if rules.get(jurisdiction)),
                       key=lambda y: (abs(y - year), -y))
        return self.rules[str(years[0])][jurisdiction] if years else None

    def _deduction(self, rules, loss):
        return min(loss, rules["capital_loss_deduction"]) if self.profile.get("capital_loss_deduction", True) else 0.0

    def _total(self, year, short, long_):
        """The year's federal, net investment income and California tax with these net capital results."""
        salary = self.salary(year)
        short, long_ = _net_capital(short, long_)
        loss = -(min(short, 0.0) + min(long_, 0.0))
        out = {"federal": 0.0, "niit": 0.0, "california": 0.0}
        federal = self._rules(year, "federal") if "federal" in self.jurisdictions else None
        if federal:
            deduction = self._deduction(federal, loss)
            ordinary = salary + max(short, 0.0) - deduction
            standard = federal["standard_deduction"][self.status]
            taxable_ordinary = max(0.0, ordinary - standard)
            taxable_all = max(0.0, ordinary + max(long_, 0.0) - standard)
            out["federal"] = _bracket_tax(taxable_ordinary, federal["ordinary"][self.status])
            # Long-term gains stack on top of ordinary income, at the long-term rates.
            long_rates = federal["long_term" if self.profile.get("long_term_rates", True) else "ordinary"]
            out["federal"] += _bracket_tax(taxable_all - taxable_ordinary, long_rates[self.status], taxable_ordinary)
            if self.profile.get("net_investment_income_tax", True):
                investment = max(short, 0.0) + max(long_, 0.0)
                over = salary + investment - deduction - federal["niit"]["threshold"][self.status]
                out["niit"] = federal["niit"]["rate"] * max(0.0, min(investment, over))
        california = self._rules(year, "california") if "california" in self.jurisdictions else None
        if california:  # every gain is ordinary income in California
            taxable = max(0.0, salary + max(short, 0.0) + max(long_, 0.0) - self._deduction(california, loss)
                          - california["standard_deduction"][self.status])
            out["california"] = _bracket_tax(taxable, california["ordinary"][self.status])
            surcharge = california.get("mental_health_services")
            if surcharge:
                out["california"] += surcharge["rate"] * max(0.0, taxable - surcharge["over"])
        return out

    def trading_tax(self, year, short, long_):
        """{federal, niit, california, total}: the extra tax this year's net capital results add to the salary's."""
        with_trading, salary_only = self._total(year, short, long_), self._total(year, 0.0, 0.0)
        out = {key: with_trading[key] - salary_only[key] for key in with_trading}
        out["total"] = sum(out.values())
        return out

    def carried_loss(self, year, short, long_):
        """The (short, long) loss, zero or negative, left to carry into next year once the year's results and
        the deduction against other income used it (short-term losses first)."""
        short, long_ = _net_capital(short, long_)
        federal = self._rules(year, "federal") or {"capital_loss_deduction": 3000}
        allowance = self._deduction(federal, float("inf"))
        left_short, left_long = min(short, 0.0), min(long_, 0.0)
        used = min(allowance, -left_short)
        return left_short + used, left_long + min(allowance - used, -left_long)

    def describe(self):
        places = " and ".join({"federal": "federal", "california": "California"}[j] for j in self.jurisdictions)
        extras = [part for part, on in (("3.8% net investment income tax", self.profile.get("net_investment_income_tax", True)),
                                        ("long-term rates on gains held over a year", self.profile.get("long_term_rates", True)))
                  if on and "federal" in self.jurisdictions]
        return (f"progressive {places} income tax ({', '.join(extras) or 'no extras'}) on gains stacked on a "
                f"{self.salary(0):,.0f} salary, {self.status.replace('_', ' ')}")


class ProgressiveTaxLedger:
    """Tax set aside on realized gains under a TaxCode, with TaxLedger's cautious timing: each gain sets aside
    its full tax at the marginal rates of the year's gains so far (losses left out), each loss releases what it
    saves against the year's net result (as far as tax is set aside), and the reserve is held until the year
    ends, when the year's actual tax is paid and the difference returned (or, rarely, made up from cash)."""

    yearly = True

    def __init__(self, code, carryforward=True):
        self.code, self.carryforward = code, carryforward
        self.rate = 1.0  # some tax applies
        self.reserved = self.paid = 0.0
        self.period = self.next_settlement = None
        self.gross = [0.0, 0.0]  # this year's gains (short, long), losses left out: the cautious basis
        self.net = [0.0, 0.0]  # this year's net results (short, long)
        self.carried = [0.0, 0.0]  # losses carried in from earlier years (short, long; zero or negative)

    def start(self, timestamp):
        self.period = _period_of(timestamp, True)
        self.next_settlement = _start_of_next(self.period, True)

    def _tax(self, short, long_):
        return self.code.trading_tax(self.period[0], short, long_)["total"]

    def realize(self, pnl, timestamp, long_term=False):
        if self.period is None:
            self.start(timestamp)
        kind = 1 if long_term else 0
        if pnl >= 0:
            before = self._tax(*self.gross)
            self.gross[kind] += pnl
            self.net[kind] += pnl
            tax = max(0.0, self._tax(*self.gross) - before)
            self.reserved += tax
            return tax
        net = lambda: (self.net[0] + self.carried[0], self.net[1] + self.carried[1])  # noqa: E731
        before = self._tax(*net())
        self.net[kind] += pnl
        released = min(max(0.0, before - self._tax(*net())), self.reserved)
        self.reserved -= released
        return -released

    def settle(self, timestamp):
        payments = []
        while self.next_settlement is not None and timestamp >= self.next_settlement:
            year = self.period[0]
            short, long_ = self.net[0] + self.carried[0], self.net[1] + self.carried[1]
            due = self.code.trading_tax(year, short, long_)
            tax = max(0.0, due["total"])
            payment = {"year": year, "month": 12, "net_realized_gain": self.net[0] + self.net[1],
                       "short_term": self.net[0], "long_term": self.net[1], "tax": tax,
                       "federal": due["federal"], "niit": due["niit"], "california": due["california"],
                       "refund": self.reserved - tax}
            self.carried = list(self.code.carried_loss(year, short, long_)) if self.carryforward else [0.0, 0.0]
            payment["loss_carried_forward"] = -(self.carried[0] + self.carried[1])
            self.paid += tax
            self.reserved = 0.0
            self.gross, self.net = [0.0, 0.0], [0.0, 0.0]
            payments.append(payment)
            self.period = _period_of(self.next_settlement, True)
            self.next_settlement = _start_of_next(self.period, True)
        return payments

    def describe(self):
        return (f"{self.code.describe()}: each profitable trade sets aside its tax at the year's marginal rates, "
                f"losses release what they save, held until the year ends, when the year's tax is paid and the "
                f"difference returned")
