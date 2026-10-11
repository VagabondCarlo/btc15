# Code review before going live

Done from the position of someone who has watched automated trading code
fail in production. Eight defects found, all fixed. Four of them would have
cost real money, and three of those were invisible in testing because the
bot would have looked like it was working.

## Would have lost money

**1. Orders would never have rested.** The spec said
`TIME_IN_FORCE_IMMEDIATE_OR_CANCEL`. That is taker-only: an order posted
below the market cancels on arrival. The entire strategy rests a limit order
and waits, so the bot would have placed orders that died instantly and
logged "unfilled" forever. You would have concluded the market never came to
your price. Now `GOOD_TILL_CANCEL`, configurable, with `first_order.py --tif`
to find the name this venue accepts.

**2. Every safety limit reset on restart.** The loss streak, cooldown, edge
penalty and peak balance lived only in memory, and both systemd units carry
`Restart=always`. A bot crashing in a loss streak would have come back with
every brake released, repeatedly. State is now persisted to SQLite and
restored, including the halted flag.

**3. Live balance was fiction.** `fetch_balance` was written and never
called. The bot took `--balance` at startup and tracked it internally
forever. Position size is 8% of balance and the hard floor is 35% of peak,
so both were computed against a number that drifts from the account the
moment anything unexpected happens. The balance is now read from the venue
at startup and at every window boundary.

**4. Open positions were forgotten on restart.** A resting order or filled
position lives on the venue, not in the process. A restart lost it, so the
order stayed live unattended, the settlement was never booked, and the next
window could open a second position on top. Open orders are now persisted
and reconciled at startup before anything else happens.

## Would have corrupted the numbers

**5. The published strike was trusted blindly.** Every trade is priced off
the distance from `priceToBeat`. A stale or wrong value poisons everything
downstream and nothing would look wrong. The bot holds the same per-second
index prints the venue averages, so it now reconstructs the strike itself
and compares. More than a dollar apart and it sits out the window. Checked
against a live window, ours matched theirs to the cent.

**6. Partial fills were treated as complete.** A fill of 3 contracts on a
10 contract order would have been booked as 10, making the position size,
the P&L and the balance all wrong from that point on. Now reads the filled
quantity and books what actually traded.

**7. A malformed timestamp crashed window setup.** Found by fuzzing the
parsers with twelve hostile payloads. `parse_terms` raised on a bad ISO
string, which under `Restart=always` means crash-looping into the same bad
payload. Now returns None and the window is skipped.

## Operational

**8. The database grew without limit.** The recorder writes about 170,000
rows a day. On a Pi that fills an SD card quietly over a couple of months.
Now prunes daily, 30 days by default.

Also added: a clock skew check against the venue's own clock at startup and
hourly, because signed requests are rejected past 30 seconds of drift and a
Pi has no battery-backed clock. The bot refuses to start beyond 20 seconds
and halts if it drifts while running. And `keys.env` is checked for
permissions, since a 644 secret is readable by every account on the machine.

## Security

- No SQL is built by string formatting anywhere. All queries are parameterised.
- The secret is read from the environment, held in memory only, and never
  logged. Error messages about it mention length, never content.
- `keys.env` is gitignored and permission-checked at load.
- Surrounding quotes are stripped from the secret, because pasting it with
  quotes is the most common setup error and the resulting base64 length
  complaint explains nothing.
- All traffic is HTTPS with default certificate verification.
- The only untrusted input is the venue's own API responses. Those are
  parsed defensively, fuzz-tested, and the one value that matters most, the
  strike, is independently verified rather than believed.

## Still untested

The live order path. No keys existed while it was written, so the request
shape comes from documentation. `preflight.py` verifies signing and
discovers the real endpoint paths read-only, and `first_order.py` sends a
single 0.01 contract order far from the market so the side, price and size
can be checked against the app by hand. Do both before running unattended.

## Test coverage

48 tests. Settlement maths against Monte Carlo, request signing verified
against a generated keypair, state persistence including the crash-recovery
cases, entry and exit logic, and parser fuzzing.

```
PYTHONPATH=. ./.venv/bin/python -m pytest tests/ -q
```
