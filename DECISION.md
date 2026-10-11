# Pre-registered test

Written 2026-10-11, before the deciding data exists. Nothing in this file
may be changed after that date. The point of writing it now is that a test
you can adjust afterwards is not a test.

## What is being claimed

When the market's mid price for UP sits between 20 and 35 cents, UP happens
less often than that price implies, so buying the other side has positive
expected value after fees.

This is the only claim left standing. The TWAP model was scored against the
market price over 116,942 paired forecasts and is not a better forecaster
(Brier 0.1430 against 0.1408, interval spanning zero, worse log loss). That
approach is closed and will not be revisited.

## Exactly how it will be tested

- **One observation per window.** The first moment the mid enters
  [0.20, 0.35). Quotes within a window are the same observation seen
  repeatedly and must not be counted more than once.
- **The trade priced.** Buy the opposite side at its ask, cost including
  the 0.0695 taker fee.
- **The statistic.** Mean edge per contract, one-sample t across windows.
- **The threshold.** t > 2.73. That is the Bonferroni correction for the
  eight price bands that were searched to find this one. A plain 95%
  threshold is not available, having gone looking.
- **The sample.** 341 qualifying windows. 199 existed when this was
  written.
- **The date.** 2026-10-18.

## What happens either way

**If t > 2.73:** trade it. $5 of ring-fenced money, 3% a trade, the existing
guards, paper for one week first to confirm the live fills match the
recorded quotes. Not more money until 100 live trades are on the board.

**If t <= 2.73:** stop. Not "collect a bit more", not "try a nearby band",
not "adjust the window". Stop, and the recorder keeps running only if the
data is wanted for something else.

## Things that would invalidate the result

- Changing the band, the entry definition, the threshold or the date after
  2026-10-11.
- Testing additional bands and reporting the best one.
- Extending the sample because the answer came back close.
- Counting more than one observation per window.

## Why this file exists

Two previous systems were abandoned after reaching the point where the
honest answer was no, having spent months getting there because no one had
written down in advance what no would look like. The expensive part was
never the losing trades. It was the time spent not knowing that the question
had already been answered.
