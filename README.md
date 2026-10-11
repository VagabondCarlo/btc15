# btc15

Research tooling for the 15-minute Bitcoin up/down contract on Polymarket
US, a CFTC-designated contract market.

**The headline result is negative, and that is why it is published.** A
strategy that looked excellent on the data it was built from turned out to
be no better than the market's own price once it was scored properly. The
interesting part is not the strategy. It is the sequence of tests that
established this, several of which should have been run much earlier, and
the list of defects found along the way in the measurement code itself.

Everything runs against the public API. No credentials are needed to
reproduce any analysis here.

---

## The contract

Every 15 minutes a market opens asking whether Bitcoin will finish the
window at or above where it started. Settlement is the average of 60
one-second CF Benchmarks index values. A correct contract pays $1.

Two mechanics matter more than they appear to.

**The averaging window is `[T-60, T-1]`, not `[T-59, T]`.** The published
documentation reads the other way. Reconstructing 720 windows under both
readings, the earlier one reproduces the venue's own `priceToBeat` and
`settlementPrice` to the cent, and the documented one is off by around 35
cents. One second of misalignment corrupts every number downstream.

**The live price is not the settlement price.** Because settlement is an
average, a contract can show green on screen and settle red. Measured across
720 windows this happens in about 2% of them, which at 96 windows a day is
roughly twice daily. In the median such case the screen shows the holder
ahead by $7.64 while the settlement is $7.57 behind. The worst observed read
$26.74 in the money and lost.

---

## What was tested, and in what order

### 1. Does the venue settle correctly?

Yes. Reconstructing settlement independently from the index feed matches
every published value to the cent across 720 windows. Before disputing any
market, this is the check that answers it.

### 2. Is a simple gap rule profitable?

A discretionary rule, enter when the index is far enough from the strike
with a few minutes left, wins 86% of the time. It still loses money: the
break-even price is 85.6 cents and the market charges considerably more. A
high win rate is not evidence of profit when the contract pays a dollar.

### 3. Does parameter selection survive out of sample?

Mostly not. Settings chosen as the best of 240 combinations on the first
half of the history returned +50.3% a day there and +13.0% on the unseen
half, and only 42% of the top 40 settings remained profitable out of sample,
with a median that lost money. Selection moved to keeping only settings
profitable in all four blocks of the history, ranked by their worst block.

### 4. Is the probability model well specified?

Not initially. Trailing realised volatility is a biased forecast rather than
a noisy one, because volatility mean-reverts: the model was overconfident in
calm stretches and too cautious in fast ones, its win rates drifting 11
points between the two. Blending the trailing estimate halfway toward a
long-run average cut that drift to 3.8% and the calibration error from 3.1%
to 1.2%.

### 5. Is the model a better forecast than the price? *(the test that mattered)*

No. A prediction market's price *is* a probability forecast, so a model
claiming an edge has to be a better one. Scored against 116,942 paired
outcomes:

| Forecaster | Brier | Log loss |
|---|---|---|
| Market mid price | 0.1408 | 0.4392 |
| The model | 0.1430 | 0.5660 |

The difference is within a window-clustered confidence interval that spans
zero, and the model is clearly worse on log loss. This is the expected
result: the model reads the same index the market makers read, so it carries
no information the price does not already contain. No parameter tuning
addresses that.

**This test should have been run in the first week.** Everything built on
the premise of out-predicting the market was answered by a single
calculation on data already collected.

### 6. Is the market itself mispriced anywhere?

Possibly, in one place. Testing the market's calibration across eight price
bands, every band leans the same direction and one is notable: when UP is
priced at 20-35 cents it occurs less often than that implies, making the
other side worth about 6 cents a contract.

Eight bands were searched, so the significance threshold is t > 2.73 after
Bonferroni correction. The observed value is t = 2.14. **It does not clear
it.** The point estimate did replicate across independent halves at nearly
identical size, and shrinking for the fact that the bands were searched puts
an honest estimate at 2 to 4 cents rather than 6.

That single claim is now pre-registered in [DECISION.md](DECISION.md), with
the band, statistic, threshold, sample size and date all fixed before the
deciding data exists. `decide.py` runs it and refuses to return a verdict
early.

---

## Defects found in this repository's own code

Listed because they shaped every result that preceded them, and because
measurement code is where quiet errors do the most damage.

| Defect | Effect |
|---|---|
| Three simulators had accumulated and disagreed | One reported an account tripling where another reported the same account losing three quarters, on identical data and settings |
| Cached price paths covered only the last 300 seconds | Every sweep over entry timing compared identical data to itself and returned the same answer for every input |
| No liquidity limit in the backtest | Compounded a small account into six figures inside a market trading $85,000 a window |
| Price model inferred from window highs and lows | Those are the two worst moments in a window; the model read 8 to 20 cents too cheap and reversed the conclusion |
| Peak balance seeded from the account, not the ring-fenced portion | The profit ratchet demanded a floor out of money the bot could not reach; it halted on its first decision and logged 153,334 refusals over four days |
| Stacked entries fired on consecutive seconds | Three entries meant one trade at triple size off one unchanged signal |
| `IMMEDIATE_OR_CANCEL` on a resting-order strategy | Taker-only; every order posted below the market would cancel on arrival while the log read "unfilled" |
| Risk state held only in memory | Systemd restarts on failure, so a crash loop cleared the loss streak, cooldown and drawdown floor |
| Volatility estimator returned zero on a stale feed | Zero volatility makes the model infinitely confident on dead data; now returns NaN and refuses to trade |
| A malformed timestamp crashed window setup | Found by fuzzing the parsers; under `Restart=always` it crash-loops on the same payload |

---

## Layout

| | |
|---|---|
| `btc15/gateway.py` | Public API client, verified endpoints only |
| `btc15/twap.py` | Settlement maths, closed form validated against Monte Carlo |
| `btc15/sim.py` | The single backtest simulator, with block bootstrap |
| `btc15/strategy.py` | Entry, exit, sizing, risk limits, named profiles |
| `btc15/prices.py` | What the market charges, measured from recorded quotes |
| `btc15/state.py` | Persisted risk state and clock-skew checks |
| `edge_test.py` | Model against market as forecasters; market calibration |
| `decide.py` | Runs the pre-registered test |
| `walkforward.py`, `robust.py` | Out-of-sample and four-block selection |
| `calibrate.py` | Model honesty, split by market speed |
| `price_curve.py` | What the contract costs as certainty arrives |
| `guards.py` | Every known failure mode, replayed against current settings |
| `watch.py` | Live monitor showing the settling average, not the live tick |
| `live_record.py` | Records the order book, places no orders |
| `btc15_bot.py` | The bot. Paper mode by default |

---

## Reproducing it

```bash
python3 -m venv .venv
./.venv/bin/pip install numpy requests cryptography pytest

# about six minutes, pulls roughly nine days of windows
PYTHONPATH=. ./.venv/bin/python -m btc15.harvest 1400
PYTHONPATH=. ./.venv/bin/python -m btc15.harvest_book

PYTHONPATH=. ./.venv/bin/python price_curve.py     # the central result
PYTHONPATH=. ./.venv/bin/python walkforward.py     # what selection is worth
PYTHONPATH=. ./.venv/bin/python robust.py          # block-stable settings
PYTHONPATH=. ./.venv/bin/python guards.py          # failure modes
```

`edge_test.py` and `decide.py` additionally need recorded quotes, which
`live_record.py` collects.

110 tests cover the settlement maths, request signing, state persistence and
crash recovery, entry and exit logic, parser fuzzing, and every documented
failure mode.

```bash
PYTHONPATH=. ./.venv/bin/python -m pytest tests/ -q
```

---

## Deployment

`deploy/` contains a Raspberry Pi setup script, systemd units and a
read-only install check. Two notes specific to that target: install numpy
from apt rather than pip, since pip compiles from source and takes close to
an hour on ARM, and both units wait on `time-sync.target` because a Pi has
no battery-backed clock and settlement is averaged over exact one-second
slots.

[GOLIVE.md](GOLIVE.md) covers connecting a real account, beginning with the
step most likely to be skipped: proving a withdrawal works before trading
anything.

---

## Risk

Nothing here is advice. Prediction market trading can lose the entire amount
risked. The bot is paper-mode by default and real orders require both an
environment flag and typed confirmation. The live order path has never been
exercised against the venue.

## Licence

MIT. See [LICENSE](LICENSE).
