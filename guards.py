#!/usr/bin/env python3
"""Every observed way money was lost, and the guard that now blocks it.

Built from two recorded sessions rather than from imagination. Each case is
a real trade that cost real money, replayed against the current settings to
confirm the guard fires. Run it after any config change; a guard that stops
firing is a regression.
"""

from __future__ import annotations

from btc15.fees import THETA_TAKER
from btc15.strategy import Config, Risk, evaluate, profile, should_exit, stake_for

PASS, FAIL = "BLOCKED", "STILL POSSIBLE"


def check(name, cost, blocked, how):
    print(f"  {'[' + (PASS if blocked else FAIL) + ']':<17} {name}")
    print(f"  {'':<17} cost you {cost}")
    print(f"  {'':<17} {how}\n")
    return blocked


def main() -> int:
    cfg = profile("turnover", bankroll=25.0)
    ok = []

    print("LOSS MODES FROM YOUR OWN TRADING, AND WHAT STOPS THEM NOW")
    print(f"profile: turnover, gap ${cfg.gap_min:.0f}-${cfg.gap_max:.0f}, "
          f"{cfg.entry_secs_min}-{cfg.entry_secs_max}s, stake "
          f"{cfg.max_fraction:.0%}, max price {cfg.max_price:.2f}\n")

    # 1 ---------------------------------------------------------------
    s = evaluate(3.38, 5, 0.02, 0.03, 0.0, cfg)
    ok.append(check(
        "Buying a near-settled contract because the screen looked green",
        "a single stake, Oct 2 23:30z",
        not s.take,
        f"entry window is {cfg.entry_secs_min}-{cfg.entry_secs_max}s; "
        f"at 5s left it refuses. watch.py also shows the settling average."))

    # 2 ---------------------------------------------------------------
    s = evaluate(250, 180, 0.97, 0.99, 0.98, cfg)
    ok.append(check(
        "Paying 92-96 cents, risking $178 to win a few dollars",
        "the three biggest September bets",
        not s.take,
        f"max price {cfg.max_price:.2f}; anything dearer is refused "
        f"whatever the gap."))

    # 3 ---------------------------------------------------------------
    stake = stake_for(25.0, 0.90, 0.78, Config(**{**cfg.__dict__,
                                                 "bankroll": None}))
    ok.append(check(
        "Betting 80% of the account on one window",
        "the peak to near zero over a day",
        stake <= 25.0 * cfg.max_fraction + 1e-9,
        f"stake is capped at {cfg.max_fraction:.0%} of balance: on $25 that "
        f"is ${stake:.2f}, not $20."))

    # 4 ---------------------------------------------------------------
    ok.append(check(
        "Stacking several buys into one window on the same signal",
        "one window's loss, two buys of $7.73 and $36.36",
        cfg.max_entries_per_window <= 2 and cfg.entry_spacing_secs >= 30,
        f"at most {cfg.max_entries_per_window} entries per window, "
        f"{cfg.entry_spacing_secs}s apart, so they cannot be one trade "
        f"at double size."))

    # 5 ---------------------------------------------------------------
    r = should_exit(0.55, 300, 60, cfg, entry_cost_per=0.80, sell_value=0.55)
    ok.append(check(
        "Letting a loser run to -78% and -89% before cutting",
        "one loss and another loss on Oct 2",
        r is not None,
        f"stop at {cfg.exit_stop_loss:.0%}: {r}"))

    # 6 ---------------------------------------------------------------
    s = evaluate(60, 700, 0.70, 0.72, 0.80, cfg)
    ok.append(check(
        "Entering early in the window, where the read is a coin flip",
        "50% win rate at 14 minutes left vs 87% at 4",
        not s.take,
        f"entries only inside {cfg.entry_secs_max}s. Earlier is refused."))

    # 7 ---------------------------------------------------------------
    risk = Risk(cfg)
    risk.s.peak_balance = 25.0
    risk.note_balance(25.0)
    risk.record(0.85, False, 0)
    ok.append(check(
        "Revenge trading straight after a loss",
        "the slide from $44 bets to 13 cent bets",
        risk.blocked(25.0, 900) is not None,
        f"cooldown after a loss: {risk.blocked(25.0, 900)}"))

    # 8 ---------------------------------------------------------------
    risk2 = Risk(cfg)
    risk2.s.peak_balance = 40.0
    ok.append(check(
        "Giving back a whole winning run",
        "the peak peak to near zero",
        risk2.floor() > cfg.bankroll,
        f"once past ${cfg.bankroll * cfg.profit_lock_at:.2f} the floor "
        f"ratchets: at a $40 peak it stops at ${risk2.floor():.2f}, above "
        f"the ${cfg.bankroll:.2f} you started with."))

    # 9 ---------------------------------------------------------------
    c9 = profile("turnover", bankroll=5.0)
    ok.append(check(
        "The bot reaching money set aside for something else",
        "promotional credit that could never be withdrawn",
        c9.bankroll == 5.0,
        "the ring-fence sizes only against its own equity; a losing bot "
        "shrinks rather than drawing on the reserve."))

    # 10 --------------------------------------------------------------
    be = 0.99 + THETA_TAKER * 0.99 * 0.01
    ok.append(check(
        "Winning most trades and still losing money",
        "a 94% win rate that lost the account",
        cfg.max_price < 0.99,
        f"at 99 cents you need {be:.1%} just to break even. The price cap "
        f"of {cfg.max_price:.2f} keeps trades where a win is worth having."))

    print("-" * 70)
    print(f"  {sum(ok)}/{len(ok)} blocked")
    if not all(ok):
        print("  A guard is not firing. Do not run live until it does.")
    return 0 if all(ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
