# Changelog

## 2026-10-02 — initial research build

Built the research tooling for the Polymarket US "BTC Up or Down: 15 min"
contract and used it to test the proposed entry rule. No trading code was
written and no orders were placed.

### Contract facts established against the live venue
- Settlement window is the 60 one-second BRTI prints stamped `[T-60, T-1]`,
  not `[T-59, T]` as documented. Verified by reconstructing published
  `priceToBeat` and `settlementPrice` to the cent across 720 windows.
- `feeCoefficient` is 0.0695 for these crypto markets, same as every other
  market. Earlier reporting of a "1.80% peak crypto fee" was quoting the fee
  as a share of the $1 payout, not of notional.
- `minimumTradeQty` is 0.01, so fractional contracts are allowed.
- `gateway.polymarket.us` serves markets, events, per-second BRTI and the
  full order book with no authentication.
- Index history before 2026-09-25 is restated by about a cent; those windows
  are filtered out of the analysis.

### Result
The $40 gap / 7 minute rule wins 86.4% of 641 trades but needs to be bought
below 85.6 cents to break even. The market charges 99 cents at the median,
and only 6.5% of triggering moments are available at or below 85 cents. The
rule as specified loses money.

The only configuration that clears break-even is resting a limit order in
the 4-7 minute zone and being filled in the cheapest quarter of prices.
Whether those fills are achievable, and whether they are adversely selected,
needs live book data.

### Added
- `btc15/gateway.py`, `btc15/twap.py`, `btc15/fees.py`, `btc15/auth.py`,
  `btc15/shapes.py`, `btc15/storage.py`, `btc15/harvest.py`,
  `btc15/harvest_book.py`
- `backtest_gap_rule.py`, `analyze_market_price.py`, `edge_table.py`,
  `live_record.py`, `scripts/edge_demo.py`
- `tests/test_twap.py`

### Known gaps
- No live order book history yet. `live_record.py` is collecting it.
- The probability model is underconfident below 90% and must not be used for
  position sizing until its calibration is fixed.
- 8.6 days of data covers one volatility regime only.

### Sizing analysis added
`sizing_check.py` quantifies the `LIVE_BET_FRACTION = 0.25` setting. With a
genuine edge (buy at 0.80, true 85% win rate) a 25% stake has a 17% chance
of finishing below the starting balance and a bottom 5% outcome of losing
90% of the account, against a 10% stake which finishes up even in its
bottom 5%. If the edge is not real and the market's 0.80 was correct, a 25%
stake ends at 0.2% of the starting balance.

It also shows why these prices are hostile to sizing at all: at 90 cents,
being 3 points wrong about the win rate takes the Kelly stake from 25% to
zero. At 99 cents the win rate has to be known to a tenth of a point.

## 2026-10-02 — real trade data and the corrected rule

The rule was then clarified: the $40 figure was a guess. The real entry is
2 to 4 minutes left, betting the side when the move looks decisive. A screen
recording of the app supplied 38 trade detail screens, 12 of them settled.

### Corrections to the earlier finding
Tested at 2-4 minutes rather than 7, the gap is far more predictive:

| gap | win rate | worth up to |
|---|---|---|
| $25-50 | 81.5% | 0.804 |
| $50-75 | 93.6% | 0.932 |
| $75-100 | 98.1% | 0.979 |
| $150-250 | 98.8% | 0.987 |

The rule is not broken. It is priced. Observed entry odds of 80% to 96% sit
within about a point of the measured win rate for that gap, so expected
value per trade ran from +$0.01 to +$3.36 on stakes of $49 to $178.

### What actually emptied the account
Stakes reached the largest bet at 96% odds to win a few dollars. One loss there costs 26
wins. At roughly 97.8% true odds, 38 trades carry a 57% chance of ruin. The
balance went from about the starting balance to near zero while the trades themselves were
close to break-even.

Replaying the same 12 trades in the same order at a fixed 2% stake ends at
$64.23 instead of $2.14.

### Added
- `data/real_trades.csv`, `analyze_real_trades.py`
- The app's own cost/odds/payout figures confirm the 0.0695 fee formula to
  the cent.

## 2026-10-02 — the bot

Built `btc15_bot.py`. Paper mode by default; real orders need `LIVE=1` in the
environment and typing YES at startup.

### Price feed
The bot reads BRTI from the venue's own `asset-prices` endpoint, which is the
exact series settlement is computed from. Measured staleness is 0.3s median,
0.9s worst, on 55-78ms round trips. At three minutes left that lag is about
$2 of uncertainty against $46 still to come, so a paid CF Benchmarks licence
would buy nothing. No Coinbase or Kraken proxy is needed and the handoff's
`PROXY_EXTRA_GAP` compensation can be dropped.

### Strategy, set by the measurements
- Entry band is a $25-75 gap with 2-4 minutes left. Gaps above $75 are
  refused outright: they are worth 97.8-98.8% and the market charges 99, so
  every observed price in that zone loses.
- Rests a limit order inside the spread rather than crossing. Taking the
  median price loses 1 to 11 cents a contract; the same bets in the cheapest
  quarter of prices make 2 to 4 cents, and resting pays the maker rebate
  instead of the taker fee.
- Requires 2 cents of edge after costs, and that the measured win-rate table
  and the live TWAP model agree within 12 points.

### Sizing and failsafes
Stake is a fraction of the current balance, so wins compound. The fraction is
quarter-Kelly on the measured edge, capped at 5%. Limits: 15% daily loss, 4
consecutive losses, 20 trades a day, one position per window, and a `STOP`
file that halts new entries immediately.

### On fees
Added `why_price_matters.py`. The taker fee at these prices is 0.07 to 0.48
cents. The gap between the market's asking price and what the bet is worth is
0.2 to 11 cents. The fee is affordable and was never the problem. A 99% win
rate still loses money at 99 cents.

## 2026-10-02 — rolling cooldown instead of daily caps

Removed the trades-per-day cap and the daily-loss stop by request. The bot now keeps rolling and handles losses with a cooldown.

- A loss sits out 2 windows. Consecutive losses double it, to a maximum of 8.
  A win resets the streak.
- Before resuming, the bot compares its recent results against the win rate
  its own table predicted. Running more than 8 points behind raises the edge
  it demands by a cent, up to 5 cents. Wins earn that back slowly. The
  cooldown is not just a pause; it changes how picky the bot is.
- The only hard stop left is a capital floor at 35% of peak balance, which
  needs a person to clear.

### Replay over the harvested windows
`replay.py` runs the full strategy and risk machine across 720 real windows.
The one unknown is the price a resting order fills at, so it runs both ends:

| Fill assumption | Trades | Win rate | $100 becomes |
|---|---|---|---|
| Limit order reached, cheap | 83 | 86% | $113.20 over 8.6 days |
| Filled at the typical price | 0 | n/a | $100.00 |

The second row is the point. When the price on offer carries no edge the bot
takes no trades at all rather than losing money slowly. The first row is what
compounding looks like if the fills are real.

### Fixed: pending strike at window open
`priceToBeat` is published a few seconds after a window opens, so the first
fetch at the boundary returns null. Both the recorder and the bot read it
once and then sat out the entire window. They now retry every 3 seconds
until the strike appears. Without this fix neither process would have
recorded or traded a single window.

### Fixed: limit price was anchored to the quote
Live recording showed the book's spread is exactly one tick. Resting "inside
the spread" therefore saved half a cent, while the market's price sat up to
ten cents above what the bet was worth, so the order was just buying the
overpricing slightly cheaper.

The limit price is now solved from fair value: the highest price that still
leaves the required edge after the maker rebate. For a $30 gap worth 81.5%
that is 79 cents. If the book is already at or below that the bot simply
takes the offer. On the days the market never comes down, it does not trade.

## 2026-10-02 — validation, and two bugs in my own tooling

### Walk-forward
`walkforward.py` chooses settings on the first half of the history and runs
them on the second. Fitted: +50.3%/day. Unseen: +13.0%/day. More telling,
of the top 40 settings from the first half only 42% were still profitable on
the second, with a median of $87.57, a loss. Picking the best backtest number
out of 144 tries is not evidence.

### Robust selection replaces best-fit
`robust.py` splits the history into four blocks, keeps only settings
profitable in all four, and ranks by the worst block. 62 of 240 survived and
they cluster around 7 cents of required edge with a 0.40 exit: a plateau
rather than a peak. Defaults now come from this, not from `tune.py`.

### Two bugs found and fixed
1. The fill-price sensitivity sweep returned an identical result for every
   price. The model fills at your limit or not at all, which is correct for
   a limit order, so price was never the variable. Fill *frequency* is.
2. The fill-rate roll happened once per second of the entry window instead of
   once per window, so a 2% fill rate still produced trades in 91% of
   windows. Every fill-rate number before this was meaningless.

### Honest expectation after both fixes
Growth scales with fill rate, which is the one unmeasured input:

| fill rate | trades/day | per day | per week |
|---|---|---|---|
| 20% | 9 | +7.7% | +68% |
| 10% | 4.6 | +3.9% | +31% |
| 5% | 2.0 | +2.8% | +21% |

Posting below the cheapest observed quartile suggests 5-15% fills, so
3-5% a day. Win rate stays 72-80% across the whole range.

## 2026-10-02 — going live

Added `preflight.py`, `first_order.py`, `GOLIVE.md` and signing tests.

### Fixed: orders would never have rested
The handoff specified `TIME_IN_FORCE_IMMEDIATE_OR_CANCEL`. That is taker
only: an order posted below the market cancels instantly. The entire
strategy rests a limit order and waits, so the bot would have placed orders
that died on arrival and logged "unfilled" forever without ever sitting in
the book. Default is now `GOOD_TILL_CANCEL` with `synchronousExecution`
false, the value is configurable, and `first_order.py --tif` lets you find
which name the venue accepts.

Live fills are no longer assumed on placement. The bot polls order status
and only treats a position as open once the venue says it filled, and
cancels anything still resting 20 seconds before settlement.

### Signing is now tested
`tests/test_auth.py` generates a keypair and verifies the signature the way
a server would, including the 64-byte secret form and rejection of a
tampered message. Previously nothing exercised the auth path at all.

## 2026-10-02 — the entry rule was regime-dependent

Checking why the paper bot had taken zero trades in 252 decisions turned up
the real problem, and it is the one that killed Argus.

### What was wrong
The win-rate table maps a dollar gap to a win rate. A $30 gap three minutes
out is worth 95% when the market moves $45 a window and 62% when it moves
$240. The backtest ran through a stretch averaging $30 per window; the live
market today averages $102. Every number derived from that table was
specific to a calm market.

### What the fix is not
Normalising the gap by trailing volatility, which was the obvious answer,
made things worse. Measured over 17,280 samples the volatility-aware model
drifted 11 points between calm and fast markets against the dollar table's
3.9, because volatility mean-reverts: a quiet ten minutes is usually
followed by a burst. Trailing volatility is a biased forecast, not a noisy
one, and the bias has a predictable sign.

### What it is
Blend the trailing estimate halfway toward a long-run average.

| | trailing only | blended |
|---|---|---|
| calm vs fast drift | 11.0% | 3.8% |
| calibration error | 3.1% | 1.2% |

The model is now both regime-stable and better calibrated than the table it
was supposed to be checking. `vol_safety` is removed: inflating sigma by a
quarter was a crude stand-in for this bias and only pushed every probability
toward 50%.

Added `calibrate.py`, which measures model honesty bucket by bucket and
splits the result by market speed.

## 2026-10-02 — ring-fence and profit ratchet

A split bankroll: half to the bot, half held back. Two things were needed and the first attempt at both was wrong.

### Ring-fence
`--bankroll` caps what the bot may size against, regardless of the account
balance. The first version floored the working balance at the allocation,
which meant a bot that had lost $0.90 was topped back up to $5 out of the
reserved half. That is the opposite of ring-fencing. A bot that has lost two
of its five dollars is now a three dollar bot, and the reserve never moves.

### Profit ratchet
The stop was a flat 35% of the high water mark, so a bot that turned $5 into
$8 could fall back to $2.80 before halting: ahead, then behind on the
original stake. Once the bot reaches 1.4x its allocation the floor becomes
the allocation plus half of everything above it.

| peak | old stop | new stop |
|---|---|---|
| $6 | $2.10 | $2.10 |
| $7 | $2.45 | $6.00 |
| $8 | $2.80 | $6.50 |
| $15 | $5.25 | $10.00 |

Past $7 a winning run can no longer end below what went in.

### Small-size handling
At a $5 balance a trade is about 0.26 contracts. The taker fee on that is
under half a cent and rounds up to a whole one, nearly doubling it and
taking a third of the edge. Below 3 contracts the bot will now only rest an
order, never cross the spread.

## 2026-10-02 — the price model was wrong

The discretionary method it replaced had been profitable by hand, which
warranted replicating rather than discarding. Building his method as a profile exposed a
bad assumption underneath everything before it.

### The error
My market-price model came from each finished window's published high and
low, paired with the index gap at that second. Those are the two worst
moments in a window by construction. It said the market charges 99 cents at
the gaps the trader traded, and on that basis I concluded taking the offer could
never work and built a strategy that rests below the market instead.

5,936 recorded quotes say otherwise:

| gap | real median ask | my model claimed | bet is worth |
|---|---|---|---|
| $25-50 | 0.75 | 0.93 | 81.5% |
| $50-75 | 0.88 | 0.99 | 93.6% |
| $75-100 | 0.90 | 0.99 | 98.1% |

Taking the offer pays 5 to 7 cents a contract in those three buckets. His
own fills at 80 to 96 percent were evidence against my model the whole time
and I read them as evidence of a mistake on his part.

### Result
Over 720 real windows at measured prices:

| profile | trades | win rate | $5 becomes |
|---|---|---|---|
| manual | 437 | 82% | $12.81 |
| manual_capped | 307 | 76% | $12.73 |
| patient | 0 | n/a | $5.00 |

`manual` is now the default. The one refinement his own results already
argued for is a $100 gap ceiling: above that the market charges 98 to 99 and
the edge is gone, which is exactly where his largest losses were.

### Added
`btc15/prices.py` measures the asking price by gap bucket from recorded
quotes and rebuilds as more data arrives. Nothing infers prices from window
extremes any more.

## 2026-10-02 — two builds, and the full video record

Read all 241 frames of the screen recording: 38 trade detail screens and 90
distinct history-list screens, deduped and stitched into a complete record.

### What the video actually shows
41 buys totalling the total staked, 20 settled wins returning $1,202.15, 3 settled
losses costing $92.80, and 9 early sells returning $533.90. Stakes from $0.13
to the largest bet, median $33.35. Both sides traded, 26 Up and 15 Down.

Two behaviours the earlier build did not have:
- **Stacking.** Up to 5 separate buys in a single window. The one window's loss loss was
  two buys of $7.73 and $36.36 in the same window.
- **Selling out.** Nine positions closed before settlement, both to cut
  losses ($27.69 sold for $15.59) and to take gains ($74.94 sold for $98.83).

The arc, newest first: 13 and 16 cent bets in the last half hour, a $4.41
loss an hour before, $2 bets at 12 hours, a a later loss loss at 16 hours, and
$95-177 bets a day earlier. the peak to near zero.

### Two builds
`replica` replicates the record: $10-400 gap, pays up to 99 cents, 40% stakes,
3 entries per window, no edge check. `manual` is the refined version: $25-100
gap, 93 cent cap, 8% stakes, one entry, 2 cent edge floor.

On the 278 real windows were actually traded, Sept 26-28, starting from the start:

| build | trades | win rate | $65 becomes |
|---|---|---|---|
| replica | 138 | 89% | $338.45 |
| manual | 167 | 84% | $120.55 |
| patient | 0 | n/a | $65.00 |

His method beats the refinement on his own windows. In-sample and optimistic,
since the win-rate table partly derives from the same period, but the ranking
is the point: the read was good and the sizing was what killed it.

`./race.sh start` runs both in paper on the same live market with separate
books and separate ring-fenced balances.

## 2026-10-02 — one simulator, and an honest out-of-sample test

### Bugs fixed
Three separate simulators had accumulated across three scripts and they
disagreed. One took a single entry per window and reported $65 growing to
$338; another stacked three and reported $5 shrinking to $1.17 on the same
data and the same profile. Both claimed to be the same strategy.
`btc15/sim.py` is now the only one.

Three real defects it exposed:
- **Stacked entries fired on consecutive seconds**, so "three entries" meant
  one trade at triple size off a single unchanged signal. Now separated by
  `entry_spacing_secs`, default 45.
- **No liquidity limit.** The simulator happily compounded $5 into $1.5m
  inside a market that trades about $85,000 a window. Capped at 0.2% of
  window notional.
- **Every result was a single path.** `bootstrap()` resamples in contiguous
  blocks, preserving volatility clustering while varying which stretches are
  met and in what order.

### Test result
Best configuration by worst-case outcome is 8% stakes with 3 entries per
window. On the fitted days that is a median of $127 from $5 across 200
paths, with the worst 5% still at $48.

On days the win-rate table did not come from:

| period | win rate | median | worst 5% | halted |
|---|---|---|---|---|
| Sept 26-28 (in sample) | 93% | $127.52 | $48.05 | 2% |
| Sept 25 (unseen) | 94% | $8.84 | $6.02 | 10% |
| Sept 29-30 (unseen) | 86% | $6.79 | $4.40 | 40% |
| Oct 1-2 (unseen) | 75% | $4.64 | $3.09 | 12% |

The edge is mostly in-sample. Out of sample the win rate falls from 93% to
75-86% and the median outcome collapses from 25x to roughly flat. Oct 1-2,
the most recent and most relevant regime, loses money.

This is the shape Argus had, caught before any money was at stake.

## 2026-10-02 — sized for income rather than growth

Added the `income` profile and made it the default: the same entry read as
`replica`, staked at 3% a trade rather than 40%.

Across 300 bootstrap paths over 720 windows the difference is the whole
story. At 3% the median finish from $5 is $29.24, the worst 5% is $13.87,
the typical drawdown is 14% and it stops out in 3% of paths. At 40% the
median is higher and it stops out in 91% of them.

1% would be safer still, but at a $5 balance 1% is five cents, which is the
minimum order. The safe fraction is too small to place and anything
placeable is too big to be safe. 3% is the smallest stake that works at $5.

### Measured rate
About 4.6 cents per dollar staked across roughly 93-140 trades a day.

| fixed stake | per day | per week | per month |
|---|---|---|---|
| $0.15 | +$0.96 | +$6.75 | +$28.95 |
| $0.50 | +$3.22 | +$22.52 | +$96.50 |
| $2.00 | +$12.87 | +$90.06 | +$385.98 |
| $10.00 | +$64.33 | +$450.31 | +$1,929.91 |

$140 a day would need about $22 a trade, which at 2% of bankroll is roughly
$1,100 in the account.

Every figure here is measured over 7.5 days against win rates partly fitted
to that stretch, and one period in four lost money. They are ceilings.

## 2026-10-02 — entry timing is the edge

A second recorded session, 23 buys over eight hours, showed a different exit
style from the first: sixteen positions sold early rather than held, typical
hold one to five minutes, winners of 20-35% on capital instead of 5-20%, and
bets of $4-14 instead of $4-177. Net +the session net on a a small deposit, with a small amount
successfully withdrawn.

### Fixed: the entry-timing sweep had been testing nothing
Cached price paths covered only the last 300 seconds of each window, so every
sweep over entry time returned identical results for 780s and 420s because
neither existed in the data. Rebuilt over the full 900 seconds.

### What that revealed
Holding everything else constant and varying only when the position opens:

| entry | win rate | $14 becomes | stopped out |
|---|---|---|---|
| 14 min left | 50% | $4.90 | 94% |
| 10 min | 64% | $5.20 | 48% |
| 7 min | 76% | $13.83 | 9% |
| 4 min | 87% | $61.59 | 3% |
| 3 min | 90% | $83.61 | 0% |

Nothing else in the strategy comes close to mattering this much. Entering
early does not give a trade room to run; it removes the information that made
it a good trade.

### Growth versus drift
At the 4-minute entry, stake size decides the shape:

| stake | median | stopped out | shape |
|---|---|---|---|
| 2% | $36.47 | 1% | grows |
| 3% | $58.43 | 4% | grows |
| 5% | $142.69 | 14% | mixed |
| 8% | $295.46 | 37% | mixed |
| 15% | $37.82 | 68% | stalls |

Above 5% the median keeps rising while the account stops surviving. 15% has
both a worse median and a 68% stop-out rate.

### Added
`turnover` profile and profit-target exits. Selling into strength and holding
to settlement test within each other's error bars; entry timing and stake
size dominate both.

## 2026-10-02 — the live price is not the settlement price

A real loss prompted this. Window 23:30z: strike 84,545.77, the app showed
84,549.15 in green, and it settled DOWN at 84,543.92. A a single stake position
bought at 2 cents went to zero while the screen showed it $3.38 in the money.

The venue settled it correctly. The index dipped to 84,527.08 early in the
final minute and recovered late, so 24 of the 60 settling prints were below
the strike and the average landed $1.85 under it. The last twelve seconds
were all above, which is what the screen was showing.

### Added: `watch.py`
A live monitor that prints the running settlement average instead of the
live tick, with how much of it is already locked, and raises an alert the
moment the two disagree. Replayed against that window it flags at 40 seconds
remaining and reads DOWN at 100% from 15 seconds on, while the app reads
green throughout.

### The bot already refused this trade on three independent grounds
Outside the 2-4 minute entry window, gap below the $20 minimum, and a
TWAP-aware probability of 0.0% at the moments the screen looked best. The
monitor exists because manual trading has none of those guards.

## 2026-10-02 — every observed loss mode, guarded

`guards.py` lists each way money was actually lost across both recorded
sessions and replays it against the current settings. `tests/test_guards.py`
runs the same checks over every profile so none can be left as a trap in the
config.

Ten cases, all blocked: buying a near-settled contract off a green screen,
paying 92-96 cents, staking most of the account, stacking buys on one signal,
letting losers run past -78%, entering early in the window, trading straight
after a loss, giving back a whole winning run, reaching money set aside for
something else, and winning most trades while still losing money.

Two profiles were leaving holes. `income` would pay up to 99 cents, where a
99.1% win rate is needed just to break even; capping at 90 tested better on
every measure. `replica` carried a 40% stake, which is what emptied the account
it was reconstructed from; it is now 3% and 93 cents, so it can be studied
and raced without repeating itself.

Default profile is now `turnover`.

## 2026-10-06 — the price model was built on too little data

### Fixed: the bot had been halted for four days
Peak balance was seeded from the raw account balance instead of the
ring-fenced one. With $25 in the account and $10 ring-fenced, the profit
ratchet computed a floor of $17.50 from money the bot could never reach, so
it halted on its first decision and stayed halted. 153,334 refusals, zero
trades. Now seeded from the working balance, with a regression test.

### The asking price is 8 to 20 cents higher than I measured
The earlier price model came from about 6 hours of recorded quotes. 70 hours
now say something different, and the two halves of that longer sample agree
with each other, so the short one was the noise.

| gap | measured in 6h | measured in 70h |
|---|---|---|
| $25-50 | 0.75 | 0.95 |
| $50-75 | 0.88 | 0.98 |
| $75-100 | 0.90 | 0.99 |

At those prices taking the typical offer loses in every bucket, from -15.2
cents at small gaps to -0.3 at large ones.

### What still works
The cheap end of the same distribution does pay. In the cheapest tenth of
moments, $50-75 clears by 4.9 cents, $75-100 by 7.5, $100-150 by 5.3 and
$150-250 by 3.5. Small gaps lose even there.

So the binding constraint is price, not the gap. `price_ceiling()` reads a
measured ceiling per gap bucket and the bot refuses anything above it, which
means sitting out roughly seven qualifying moments in eight. The `turnover`
gap band moves to $50-250 because below that nothing clears at any price.
