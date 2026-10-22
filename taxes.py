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

from datetime import datetime, timezone


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

    def realize(self, pnl, timestamp):
        """Record a closed trade's gain or loss; returns the tax to move from trading cash into the reserve
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
