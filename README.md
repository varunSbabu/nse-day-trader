# The Day Trader skill, ported to NSE

Seven of the eleven [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
tools (MIT, by Jake Schincariol), translated from New York to India's National
Stock Exchange. Free Yahoo data, standard-library Python, nothing to install.

The port is a script, not a fork: `port_to_nse.py` reads the upstream checkout
and rewrites it. Every edit asserts it matched exactly once, so when upstream
changes the port fails loudly instead of silently shipping a no-op. To refresh
after upstream updates, pull it and run the script again.

```bash
python3 port_to_nse.py --src ../day-trader-skill --out skills
```

## What was wrong with the US version on NSE

| | upstream | here |
| --- | --- | --- |
| symbols | `RELIANCE` -> `RELIANCE` (and `.` -> `-`, so `RELIANCE.NS` -> `RELIANCE-NS`, which does not exist) | `RELIANCE` -> `RELIANCE.NS`, percent-encoded so `M&M` works |
| clock | New York, with US daylight-saving arithmetic | IST, UTC+5:30 all year |
| session | 09:30-16:00, 78 five-minute bars | 09:15-15:30, 75 bars |
| opening range | 09:30-09:45 | 09:15-09:30 |
| no entries | before 09:45, after 15:30 | before 09:30, after 15:00 |
| flat by | 15:55 | 15:20 |
| holidays | NYSE, plus 1 p.m. early closes | the NSE equity calendar from NSE's own holiday API |
| universe | Nasdaq Trader, ~5,900 US stocks | NSE `EQUITY_L.csv`, 2,367 EQ-series stocks |
| tickers | `^[A-Z]{1,5}` | up to 20 chars with `&` and `-`, optional `.NS`/`.BO` |
| money | `$` | `Rs.` (ASCII: the Windows console mangles the rupee sign) |
| limit keys | `max_loss_per_trade_usd` | `..._inr` — renamed in the config **and** the code together |

Two NSE-specific fixes worth knowing about:

- **No-trade slots.** NSE prints nothing at all in some 5-minute slots, even in
  liquid names, so Yahoo sends a null. Upstream requires all 75 slots present
  and over 60 sessions of RELIANCE not one day qualified. A slot with no trade
  is the price not moving, so it is carried flat from the previous close at
  zero volume. A day missing more than ~10% of its slots is left out instead.
- **NSE's WAF** stalls on a non-browser User-Agent, so the universe fetch sends
  a browser one. Only for `nseindia.com`; Yahoo still gets the tool's own.

## The seven

```bash
python3 nse-scanner/scan.py universe                                  # 2,367 NSE stocks
python3 nse-scanner/scan.py orb                                       # opening-range breakouts
python3 nse-entry/entry.py check RELIANCE --setup orb                 # waiting / triggered / expired
python3 nse-exit/exit.py plan --stop or-mid --target 2R --write trader/plan.json
python3 nse-timemachine/timemachine.py run --write                    # 10,000 replays + the history
python3 nse-budget/size.py --write                                    # -> trader/orders.json
python3 nse-alerts/alerts.py quote RELIANCE --write                   # a fresh price for the risk check
python3 nse-risk/check.py                                             # PASS / BLOCK per order
python3 nse-alerts/alerts.py add --from-plan                          # stop, target, flat by 15:20
```

Run `nse-alerts/alerts.py quote SYM --write` **before** `nse-risk/check.py`.
The risk check measures risk from the higher of the plan's entry and a fresh
price, but it only has a fresh price if you fetch one; without it, it silently
measures against a plan entry that may be hours stale and can pass a trade that
is well over your loss limit.

## Not ported

- **broker** — upstream places orders through Alpaca, which does not serve
  Indian markets. The chain ends at the risk check and hands you a ticket to
  place yourself. Indian brokers with APIs, if you want to wire one up:
  Zerodha Kite Connect, Upstox, Dhan, Angel One SmartAPI, Fyers.
- **news** — Yahoo headlines would work for `.NS`, but half the tool reads SEC
  EDGAR 8-K filings. The Indian analogue is NSE/BSE corporate announcements, a
  different source.
- **analyst** — SEC XBRL company facts. Indian filings are not in EDGAR.
- **pelosi** — US congressional disclosures under the STOCK Act.

## Fine print

Not financial advice, and nothing here can place an order. The data is free,
delayed and unofficial: Yahoo's chart endpoint is not an official API and can
lag, change or rate-limit you. Check anything that matters against your broker.
A stop is a plan, not a guarantee. The Time Machine is a simulation built from
the stock's own recent sessions, not a prediction — a result, a halt or a
headline can do what those sessions never did. SEBI's own studies have found
the large majority of individual intraday and F&O traders lose money.

The holiday calendar covers 2026 only; NSE had not published 2027 when this was
built. Re-run the port once it does. Day-trading gains in India are short-term
gains, and STT, stamp duty and brokerage are not modelled anywhere in here.

MIT, following upstream.
