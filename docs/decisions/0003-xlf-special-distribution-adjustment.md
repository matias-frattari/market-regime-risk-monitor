# ADR 0003: Correcting the XLF special distribution of 2016-09-19

- **Status:** Accepted
- **Date:** 2026-11-18

## Context

The first run of `uv run mrm check` (2026-11-18) reported one warning:
`price_extreme_returns XLF 2016-09-19 +19.56%`. All OHLC fields jump on that date, and
volume falls from about 75 M to about 50 M. A +19.56% day for a broad financial-sector ETF
with no market event is implausible, so it was investigated before any metric reads XLF.

On 2016-09-19 the Real Estate sector was taken out of the Financial Select Sector Index.
XLF paid its real-estate holdings to shareholders as a special distribution of XLRE shares.

## Evidence

### Verified (sources and data)

**Distribution (sources).** State Street Global Advisors declared a special distribution of
**0.139146 XLRE shares per XLF share, valued at USD 4.44356 per XLF share** (as of
2016-09-16). The ex-date was **2016-09-19**, the record date 2016-09-21 and the pay date
2016-09-22. The OCC/MIAX options notice confirms the 2016-09-19 ex-distribution date set by
the NYSE.

- SSGA (Business Wire), index change and fund modification:
  <https://www.businesswire.com/news/home/20160608006488/en/State-Street-Global-Advisors-Announces-that-Changes-to-the-Financial-Select-Sector-Index-will-Result-in-Modifications-to-The-Financial-Select-Sector-SPDR%C2%AE-Fund>
- SSGA distribution announcement (0.139146 XLRE / USD 4.44356):
  <https://app.researchpool.com/provider/businesswire/state-street-corp-stt-state-street-global-advisors-announces-dividend-distributions-resultin>
- OCC/MIAX notice (ex-date 2016-09-19):
  <https://miaxglobal.com/sites/default/files/alert-files/XLF_Distribution_39575.pdf>

**Provider data (Twelve Data `/time_series`, XLF, 2016-09-14..2016-09-21, two requests on
2026-11-18).** `end_date` is exclusive, so 2016-09-21 was not returned; that row comes from
`raw_prices` only.
- *Unadjusted* = `adjust=none`.
- *Adjusted* = `adjust=all`, fetched on 2026-11-18. It is identical to `raw_prices`, which was
  loaded on 2026-11-17.
- *Factor* = adjusted / unadjusted.

| Date | Unadjusted close | Adjusted close (= `raw_prices`) | Unadj. return | Adj. return | Factor | Unadj. volume | Adj. volume |
|---|---|---|---|---|---|---|---|
| 2016-09-14 | 23.82 | 13.50035 | – | – | 0.56677 | 67,324,800 | 82,876,829 |
| 2016-09-15 | 23.96 | 13.57969 | +0.59% | +0.59% | 0.56677 | 55,748,600 | 68,626,527 |
| 2016-09-16 | 23.62 | 13.45090 | −1.42% | −0.95% | 0.56947 | 61,117,600 | 75,235,766 |
| **2016-09-19** | **19.31** | **16.08133** | **−18.25%** | **+19.56%** | **0.83280** | 50,396,100 | 50,396,100 |
| 2016-09-20 | 19.32 | 16.089658 | +0.05% | +0.05% | 0.83280 | 39,903,200 | 39,903,200 |

Facts established by these numbers:
1. **The distribution explains the unadjusted drop.** USD 4.44356 / 23.62 = 18.81% of the
   2016-09-16 close, and the observed unadjusted return is −18.25%. Adding the distribution
   back gives (19.31 + 4.44356) / 23.62 − 1 = +0.57%, an ordinary day.
2. **The provider's adjustment across the ex-date is wrong.**
   - A correct adjustment uses a factor of 1 − 4.44356/23.62 = **0.81187**.
   - The adjusted series implies 0.56947 / 0.83280 = **0.68380**, so the adjusted series
     jumps +19.56% instead of being continuous.
3. **Pre-ex-date volume is scaled by 1.2310** (82,876,829 / 67,324,800; the same ratio on all
   three days). 1/1.2310 = 0.8123, close to the correct 0.81187.
4. **The change of factor between 2016-09-15 and 2016-09-16** (0.56677 → 0.56947) is an
   ordinary dividend with ex-date 2016-09-16. It is already reflected in the data.
5. **There is no other event on 2016-09-19:** the factor is the same on 09-19 and 09-20.

### Inferred (not confirmed by the provider)

How Twelve Data built its `adjust=all` series is an **inference** from the numbers above, not
documented behaviour. The volume scaling (fact 3) looks like a **split** treatment of the
distribution with an approximately correct ratio. The price factor (0.68380) is that ratio
**plus another adjustment of the price** of about 0.842 (0.68380 / 0.81187), whose origin is
unknown. The fix below does not depend on this explanation; it only uses the verified numbers.

## Decision

Correct the data in a view, never in the raw table, and only while the provider's error is
present.

- **`config/price_adjustments.yaml`** documents each correction: id, ticker, `ex_date`,
  `multiplier`, `apply_if_raw_return_above`, `derivation` and `sources`. It is synced into
  the table `price_adjustments` (delete + insert, one transaction) by
  `mrm.storage.clean.prepare_clean_prices`, which `mrm check` runs first.
- **`prices_clean` view** = `raw_prices` with open/high/low/close **before** each `ex_date`
  multiplied by the adjustment's `multiplier`. Same rows as `raw_prices`; the volume is
  unchanged. `adjustment_factor` and `adjustment_ids` show what was applied.
- **Conditional application.** The view `price_adjustment_status` computes the raw return on
  `ex_date` against the previous stored trading day. The adjustment applies only while that
  return is above `apply_if_raw_return_above` (0.10 for XLF). The condition is evaluated
  every time the view is queried, not when the config is synced.
- `raw_prices` and the ingestion are not modified. Every downstream module reads
  `prices_clean` (see `docs/architecture.md`, "Data layers").

### Multiplier

```
correct factor at the ex-date   f   = 1 − 4.44356 / 23.62           = 0.81187
provider factor after ex-date   a1  = 0.83280   (2016-09-19)
provider factor before ex-date  a0  = 0.56947   (2016-09-16)
multiplier for pre-ex prices    m   = a1 × f / a0
                                    = 0.83280 × 0.81187 / 0.56947   = 1.18729
```

`m` is a ratio of provider factors. `adjust=all` rescales the whole history by the same
factor after each new dividend, so `a1 / a0` is unchanged by later re-adjustments and the
same multiplier stays valid after future full reloads. The conditional check below covers
the case where that assumption fails.

### Verification on the real warehouse (2026-11-18)

| Date | `raw_prices` close | `prices_clean` close | Factor | Raw return | Clean return |
|---|---|---|---|---|---|
| 2016-09-14 | 13.50035 | 16.02883 | 1.18729 | – | – |
| 2016-09-15 | 13.57969 | 16.12303 | 1.18729 | +0.59% | +0.59% |
| 2016-09-16 | 13.45090 | 15.97012 | 1.18729 | −0.95% | −0.95% |
| 2016-09-19 | 16.08133 | 16.08133 | 1.0 | +19.56% | **+0.70%** |
| 2016-09-20 | 16.089658 | 16.08966 | 1.0 | +0.05% | +0.05% |
| 2016-09-21 | 16.18959 | 16.18959 | 1.0 | +0.62% | +0.62% |

- **Rows changed:** 2444 XLF rows are adjusted (2007-01-04..2016-09-16). No other ticker
  changes, and `prices_clean` has the same row count as `raw_prices`.
- **`mrm check` after the change:**
  - the XLF warning is gone, with no exception in `config/data_quality.yaml`;
  - `price_adjustments XLF` reports info `applied ×1.18729`;
  - critical = 0, warning = 0;
  - the other 10 findings are identical to the previous run.
- **Test:** `tests/test_prices_clean_real.py` checks |clean return on 2016-09-19| < 1%. It
  opens the warehouse read-only and skips when the warehouse or the view is absent.

### Known limitation: +0.70% vs +0.57%

The corrected return is **+0.70%**, while the unadjusted return plus the distribution is
**+0.57%**. This is a matter of convention, not an error in the multiplier:
- **+0.70% (the clean return):** a multiplicative back-adjustment (factor 1 − D/P₀) treats
  the distribution as reinvested in XLF at the ex-date price. The adjusted return on the
  ex-date is then P₁ / (P₀ − D) − 1 = 19.31 / (23.62 − 4.44356) − 1 = **+0.6965%**. It equals
  the clean return (+0.6964%) up to the rounding of the multiplier to 1.18729.
- **+0.57%:** (P₁ + D) / P₀ − 1 treats the distribution as cash.

There is no quarterly dividend on 2016-09-19 to account for:
- The provider factor is the same on 09-19 and 09-20.
- The ordinary dividend with ex-date 2016-09-16 is already in both the raw and the clean series.

The multiplicative convention is the one `adjust=all` uses for every other dividend in the
history, so the clean series stays internally consistent.

## Volume is not corrected

- No metric in this project uses volume: returns, volatility, VaR, correlations and the
  backtest use prices only.
- The provider already rescaled pre-ex-date volume by 1.2310, a split-like treatment that
  differs from the factor it applied to prices (see "Inferred"). Undoing it would mean
  reversing an undocumented provider adjustment with no source to back it.
- `prices_clean.volume` therefore equals `raw_prices.volume`. Do not use XLF volume across
  2016-09-19 without revisiting this.

## Risk of double adjustment and mitigation

If Twelve Data fixes its series, keeping the multiplier would over-correct XLF before
2016-09-19 by 18.7%. Mitigations:
1. **Self-disabling condition.** After a provider fix, the raw ex-date return would be about
   +0.70%, below the 10% threshold. The view then stops applying the adjustment on the next
   query after the reload, without any config change.
2. **Visible status.** `mrm check` reports `price_adjustments` as a **warning** whenever an
   adjustment is skipped:
   - the raw return is at or below the threshold ("provider may have fixed the data");
   - the ex-date is missing;
   - the previous trading day is missing.
   A skipped adjustment must be reviewed and then removed from the yaml.
3. **Over-correction is caught.** Price checks read `prices_clean`. A wrong multiplier would
   show up as an extreme return on the ex-date (`price_extreme_returns`).

Residual risk: a partial provider fix that leaves the raw return above 10% but changes the
factor ratio would still apply 1.18729. In that case the clean ex-date return moves away from
about +0.70%, so the real-warehouse test (< 1%) and `price_extreme_returns` (> 15%) catch
large deviations, but not small ones.

## How to revert

1. Delete the entry from `config/price_adjustments.yaml`.
2. Run `uv run mrm check`, or call `prepare_clean_prices`. The sync empties the matching
   `price_adjustments` row, and `prices_clean` equals `raw_prices` for XLF again.
3. `raw_prices` never changed, so no reload is needed.

The XLF warning (`price_extreme_returns`, +19.56%) will reappear until the data is fixed some
other way.

## Pending actions

- **Report the error to Twelve Data:** not done yet. Include the table above and the SSGA
  sources.
- Review this ADR whenever `mrm check` reports `price_adjustments` as a warning.
- Distributions smaller than this one (below the 15% extreme-return threshold) would not be
  detected by the current checks. A continuity check comparing `adjust=all` and
  `adjust=none` factors with known distributions is a possible extension; it is not built.

## Consequences

- One documented, reversible correction lives in config, a sync table and two views; the raw
  data stays the provider's.
- Every downstream module reads `prices_clean`, never `raw_prices`.
- New corrections follow the same path: a yaml entry with derivation and sources, a
  conditional threshold, and this ADR as a template.
