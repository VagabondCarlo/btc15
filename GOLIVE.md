# Connecting the bot to your account

Work through this in order. Each step is cheap and catches a different way
the first live order can go wrong.

## 1. Get API keys

In the Polymarket US app, finish identity verification if you have not. Then
go to **polymarket.us/developer**, sign in with the same account, and create
a key. You get a **Key ID** and a **Secret**.

The secret is shown once. Copy it immediately.

```
cd ~/btc15
cp keys.env.example keys.env
```

Edit `keys.env` and fill in `PM_KEY_ID` and `PM_SECRET`. Leave `LIVE=0`.
The file is gitignored. Never commit it or paste it anywhere.

## 2. Check the account reads correctly

```
cd ~/btc15
set -a && . ./keys.env && set +a
PYTHONPATH=. ./.venv/bin/python preflight.py
```

This places no orders. It signs requests, finds which endpoint paths your
account actually answers on, prints your balance, and writes what it learned
to `data/endpoints.json` so the bot uses real paths instead of guessed ones.

If signing is rejected, it is almost always one of two things: the secret was
pasted with quotes or a line break, or this machine's clock is off. Check
with `date -u`.

Do not continue until it prints READY FOR LIVE.

## 3. Send one order by hand

This is the important step. The bot's order code has never run against the
venue. The field names come from documentation, and documentation is wrong
in small ways that matter.

```
PYTHONPATH=. ./.venv/bin/python first_order.py --dry
```

That prints the exact request and sends nothing. Read it. Then send it for
real:

```
PYTHONPATH=. ./.venv/bin/python first_order.py --price 0.10 --size 0.01
```

It asks you to type SEND. The order is one hundredth of a contract at ten
cents, so you are risking a tenth of a cent, and the price is far from the
market so it will rest unfilled rather than trade.

Now open the app and check three things:

1. The order shows the side you asked for. **If UP and DOWN are swapped, stop.**
   The intent mapping is wrong and the bot would trade the wrong side of
   every position.
2. The price matches.
3. The size matches.

Cancel the order in the app when you are done.

If the venue rejects the order, the error names the field it did not like.
Fix the body in `first_order.py`, rerun, and once it is accepted copy the
same change into `LiveBroker.place` in `btc15_bot.py`.

### Time in force

The handoff spec said `IMMEDIATE_OR_CANCEL`. That is taker-only: the order
fills against what is already in the book or dies immediately. This strategy
rests an order below the market and waits, so that setting would cancel every
order instantly and the bot would never trade while logging "unfilled"
forever.

The default is now `TIME_IN_FORCE_GOOD_TILL_CANCEL`. If the venue rejects
that name, try others until one is accepted:

```
PYTHONPATH=. ./.venv/bin/python first_order.py --dry --tif TIME_IN_FORCE_DAY
```

Then set `time_in_force` in `btc15/strategy.py` to whatever worked.

## 4. Run live, small

```
LIVE=1 ./run_bot.sh --balance <your actual balance>
```

It prints the settings, warns you, and waits for you to type YES.

Watch the first few trades. For each one check that the app shows the same
side, price and size the log does.

## 5. What to watch for in the first week

- **Fills that never come.** If the log shows orders placed and cancelled
  unfilled every window, the strategy is posting below where the market ever
  trades. That is a real answer, not a bug: it means there is no edge at a
  price you can get.
- **Fills that always come.** If every order fills immediately, the price is
  not below the market and you are paying the taker fee without knowing.
  Check the fill price against the book in the recorder database.
- **Three losses in a row.** Expected at a 76% win rate, roughly once every
  seventy trades. The bot sits out and gets pickier on its own. Let it.

## Stopping it

```
touch STOP          halt new entries, keep any open position
pkill -f btc15_bot  stop the process entirely
```

`STOP` is checked before every entry, so it takes effect within a second.
Delete the file to resume.

## A number to hold on to

At a 76% win rate you lose roughly one trade in four, and three losses in a
row will happen about once every seventy trades. Size so that stretch is
survivable and boring. The bot's 8% stake and 35% floor are built for that,
but only if the balance you give it is money you can lose entirely.

---

# Prove you can withdraw BEFORE you trade

This goes first, before any of the steps above.

A balance you cannot withdraw is a score, not money. Three separate rules
stack against a first withdrawal on Polymarket US, and any one of them is
enough to block it:

**Deposits must clear.** A debit card or ACH deposit takes 3 to 4 business
days to clear before it can be withdrawn. You can trade with it immediately,
which is the trap: the money is usable but not yet retrievable. A balance
built in the first day cannot be withdrawn on the first day no matter how
large it is.

**Promotional credit is never withdrawable.** The rules say it plainly:
promo credits "cannot be withdrawn, including after they are used in trading
or after the related position settles." Only profit generated on top becomes
eligible, and only after the full credit has been deployed as collateral and
those positions have settled. A $50 credit stays $50 of someone else's money
forever. Your deposited cash is also spent first, so the credit is the last
thing consumed and the condition is the last thing met.

**ACH withdrawals have a $50 minimum.** Debit card and Apple Pay do not, and
clear in minutes rather than days. At small balances that is the route that
works.

Withdrawals also return to the original funding source and are processed in
the order deposits were made.

## So the first thing you do is this

1. Deposit real cash. Take no promotional credit if the goal is money you
   can take out.
2. Wait for it to clear. Three to four business days. Do not trade yet.
3. Withdraw a small amount back to the card you funded with.
4. Watch it arrive.

Only then start the bot. It costs a few days and tells you whether the exit
works while you have nothing at stake. Testing the exit before you need it
is ordinary practice for anyone running money, and it is the step that turns
a number on a screen into something real.

`preflight.py` prints the withdrawable balance separately from the total
whenever the venue exposes it, so you can see which number is yours.
