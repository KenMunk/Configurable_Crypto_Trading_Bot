# tax/: the progressive tax model's settings

By default the simulator taxes a model's realized gains the way US federal and California income tax would: stacked on top of your salary, at progressive rates, as the extra tax trading adds (see [How taxes are settled](../Project_Documentation/model_configuration.md#how-taxes-are-settled) and `taxes.py`). This folder holds what it needs:

| File | In Git | Purpose |
|---|---|---|
| `tax_rules.json` | Tracked | Federal and California brackets, standard deductions, long-term capital gains thresholds and the net investment income tax, by year, with their sources. |
| `tax_profile.example.json` | Tracked | A template for your tax situation. |
| `tax_profile.json` | **Ignored** | Your tax situation: filing status and salary. Git never sees it, so your income stays private. Without it, the example is used. |

## Your tax profile

Copy the example and edit the copy:

```bash
cp tax/tax_profile.example.json tax/tax_profile.json
```

| Field | Meaning |
|---|---|
| `filing_status` | `single`, `married_joint` or `head_of_household`. |
| `salary` | Your yearly taxable income besides trading. Trading gains are stacked on top of it, so it sets the rates they pay. |
| `salary_by_year` | Salaries for particular years, such as `{"2027": 110000}`, as your career progresses; other years use `salary`. |
| `jurisdictions` | `federal` and/or `california`. |
| `long_term_rates` | Gains on positions held more than a year get the federal long-term rates (0%, 15%, 20%). California taxes them as ordinary income either way. |
| `net_investment_income_tax` | The federal 3.8% on investment income above $200,000 ($250,000 married filing jointly). |
| `capital_loss_deduction` | A year's net capital loss reduces other income by up to $3,000. |
| `loss_carryforward` | The rest of a year's net loss carries forward to offset later years. |

A profile can point at another tax profile with `friction_model.tax_profile`, or use a flat rate instead with `friction_model.tax_model: "flat"` (then `tax_buffer_rate` applies to every gain).

## The rules file

Each year lists its federal and California rules. A year missing from the file, or a jurisdiction missing from a year, uses the nearest year that has it: backtests of earlier years use 2025's figures, and 2026 uses California's 2025 schedules until the 2026 ones are published. Tax law changes, so check the figures against the sources in the file and add each year as it is published.

The model leaves out personal exemption credits, itemized deductions, the alternative minimum tax and California's 1% Mental Health Services Tax phase-in details beyond its $1 million threshold. It is an estimate for comparing strategies, not tax advice.
