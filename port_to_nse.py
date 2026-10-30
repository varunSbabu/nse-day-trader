#!/usr/bin/env python3
"""Port the US day-trader tools to India's NSE.

Reads the upstream skills (Jakeschincariol/day-trader-skill, MIT) and writes
NSE versions beside them. Every edit asserts it matched exactly once, so an
upstream change breaks the port loudly instead of shipping a silent no-op.

    python3 port_to_nse.py --src ../day-trader-skill --out skills

What changes, and why:

  symbols   RELIANCE -> RELIANCE.NS. Upstream rewrites "." to "-" for US class
            shares (BRK.B -> BRK-B), which turns RELIANCE.NS into a symbol
            Yahoo has never heard of. NSE tickers also carry "&" (M&M), so
            anything going into a URL is percent-encoded.
  clock     New York -> IST. Easier than the US: UTC+5:30 all year, no DST.
  session   09:15-15:30 instead of 09:30-16:00; opening range 09:15-09:30.
  holidays  The NSE equity (CM) calendar, straight from NSE's holiday API.
  universe  NSE's EQUITY_L.csv instead of Nasdaq Trader's symbol directories.
  money     "$" -> "Rs." (ASCII on purpose: the Windows console mangles the
            rupee sign).

Not ported: trader-broker (Alpaca has no Indian market), trader-analyst (SEC
XBRL) and trader-pelosi (US Congress). Those need different data sources
entirely, not a translation.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import sys

TOOLS = {
    "nse-scanner":     ("trader-scanner", "scan.py"),
    "nse-entry":       ("trader-entry", "entry.py"),
    "nse-exit":        ("trader-exit", "exit.py"),
    "nse-timemachine": ("trader-timemachine", "timemachine.py"),
    "nse-budget":      ("trader-budget", "size.py"),
    "nse-risk":        ("trader-risk", "check.py"),
    "nse-alerts":      ("trader-alerts", "alerts.py"),
}

HERE = os.path.dirname(os.path.abspath(__file__))


class PortError(Exception):
    pass


# ------------------------------------------------------------------ helpers

def sub_once(src, old, new, what):
    n = src.count(old)
    if n != 1:
        raise PortError("%s: expected 1 match, found %d\n  %r" % (what, n, old[:90]))
    return src.replace(old, new)


def sub_all(src, old, new, what, least=1):
    n = src.count(old)
    if n < least:
        raise PortError("%s: expected >=%d matches, found %d\n  %r" % (what, least, n, old[:90]))
    return src.replace(old, new)


def replace_func(src, name, new, what):
    """Replace a whole top-level `def name(...)` with new (ending in \\n)."""
    pat = re.compile(r"^def %s\(.*?(?=^\S)" % re.escape(name), re.S | re.M)
    if len(pat.findall(src)) != 1:
        raise PortError("%s: def %s() not found exactly once" % (what, name))
    return pat.sub(lambda m: new.rstrip("\n") + "\n\n\n", src, count=1)


def replace_dict(src, name, new, what):
    """Replace a whole top-level `NAME = {...}` block."""
    pat = re.compile(r"^%s = \{.*?^\}" % re.escape(name), re.S | re.M)
    if len(pat.findall(src)) != 1:
        raise PortError("%s: %s = {...} not found exactly once" % (what, name))
    return pat.sub(lambda m: new.rstrip("\n"), src, count=1)


def ensure_import(src, mod):
    if re.search(r"^import %s$" % re.escape(mod), src, re.M):
        return src
    m = list(re.finditer(r"^import \w[\w.]*$", src, re.M))
    if not m:
        raise PortError("no import block to extend for %s" % mod)
    at = m[-1].end()
    return src[:at] + "\nimport " + mod + src[at:]


# ------------------------------------------------------------------ holidays

def nse_holidays(raw_path):
    """NSE's holiday API dump -> {"YYYY-MM-DD": "Name"} for the equity segment."""
    with open(raw_path, encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("CM")
    if not rows:
        raise PortError("no CM (equity) segment in %s" % raw_path)
    out, muhurat = {}, []
    for r in rows:
        d = datetime.datetime.strptime(r["tradingDate"].strip(), "%d-%b-%Y").date()
        name = " ".join(str(r.get("description") or "").split())
        if name.endswith("*"):            # the Diwali Muhurat evening session
            name = name[:-1].strip()
            muhurat.append(d.isoformat())
        out[d.isoformat()] = name
    return out, sorted(muhurat), sorted({x[:4] for x in out})


def holidays_literal(hol, muhurat):
    lines = ["NSE_HOLIDAYS = {"]
    for k in sorted(hol):
        tag = "  # Muhurat evening session only" if k in muhurat else ""
        lines.append("    %r: %r,%s" % (k, hol[k], tag))
    lines.append("}")
    return "\n".join(lines)


# ------------------------------------------------------------------ the code we inject

SYMBOL_BLOCK = '''
# ---------------------------------------------------------------- NSE symbols

NSE_SUFFIX = ".NS"
EXCHANGE_SUFFIXES = (".NS", ".BO")


def nse_symbol(sym):
    """A plain NSE ticker -> the Yahoo symbol. RELIANCE -> RELIANCE.NS.

    An explicit .NS or .BO is kept as given, and so is an index (^NSEI).
    Upstream rewrote "." to "-" here for US class shares; on NSE that turns
    RELIANCE.NS into RELIANCE-NS, which does not exist."""
    s = str(sym).upper().strip()
    if not s or s.startswith("^") or s.endswith(EXCHANGE_SUFFIXES):
        return s
    return s + NSE_SUFFIX


def nse_url_symbol(sym):
    """The Yahoo symbol, percent-encoded: NSE tickers carry "&" (M&M.NS)."""
    return urllib.parse.quote(nse_symbol(sym), safe="")
'''

IST_CLOCK = '''
def eastern_offset(utc):
    """Hours from UTC for India: +5.5, all year. IST has no daylight saving,
    so the US DST arithmetic upstream collapses to a constant. The name is
    kept so the call sites read the same as upstream."""
    return 5.5


def to_eastern(utc):
    """Naive UTC -> naive IST."""
    return utc.replace(tzinfo=None) + IST_DELTA


def eastern_to_utc(local):
    """Naive IST -> naive UTC."""
    return local - IST_DELTA
'''

NSE_UNIVERSE = '''
def build_universe(texts):
    """{filename: text} -> (kept [(SYM, name)], counts, created).

    NSE's EQUITY_L.csv, keeping the EQ series: the ordinary rolling-settlement
    book. BE (trade-for-trade), BZ, SM and ST (SME platforms) are dropped:
    they are surveillance or small-cap boards, too thin to day trade."""
    kept, counts, created = {}, {}, None
    text = texts.get(NSE_FILES[0])
    if text is None:
        raise ScanError("missing %s" % NSE_FILES[0])
    rows = list(csv.DictReader(io.StringIO(text)))
    counts["rows"] = len(rows)
    for r in rows:
        clean = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        sym = clean.get("SYMBOL", "")
        series = clean.get("SERIES", "")
        name = " ".join(clean.get("NAME OF COMPANY", "").split())
        if not sym:
            counts["no symbol"] = counts.get("no symbol", 0) + 1
            continue
        if series != "EQ":
            why = "%s series" % (series or "no")
            counts[why] = counts.get(why, 0) + 1
            continue
        counts["keep"] = counts.get("keep", 0) + 1
        kept[sym] = name
    return sorted(kept.items()), counts, created


def cmd_universe(a):
    t0 = time.time()
    fname = NSE_FILES[0]
    if a.offline:
        p = a.offline if os.path.isfile(a.offline) else os.path.join(a.offline, fname)
        if not os.path.exists(p):
            raise ScanError("no %s in %s" % (fname, a.offline))
        with open(p, encoding="utf-8", errors="replace") as fh:
            texts = {fname: fh.read()}
    else:
        try:
            texts = {fname: http_get(NSE_URL, timeout=60)}
        except urllib.error.HTTPError as e:
            raise ScanError("nseindia.com answered HTTP %d for %s" % (e.code, fname))
        except (urllib.error.URLError, OSError) as e:
            raise ScanError("could not reach nseindia.com: %s" % getattr(e, "reason", e))
    kept, counts, created = build_universe(texts)
    if not a.no_write:
        d = os.path.dirname(a.out)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.out, "w", encoding="utf-8") as fh:
            for sym, name in kept:
                fh.write("%s|%s\\n" % (sym, name))
    dropped = {k: v for k, v in counts.items() if k not in ("keep", "rows")}
    if a.json:
        print(json.dumps({"count": len(kept), "rows": counts.get("rows", 0),
                          "dropped": dropped, "out": None if a.no_write else a.out,
                          "seconds": round(time.time() - t0, 1)}, indent=2))
    else:
        print("NSE equity list (%s): %s rows." % (fname, "{:,}".format(counts.get("rows", 0))))
        print("Kept %s NSE stocks in the EQ series." % "{:,}".format(len(kept)))
        if dropped:
            print("Dropped: " + ", ".join("%s %s" % ("{:,}".format(v), k)
                                          for k, v in sorted(dropped.items(), key=lambda kv: -kv[1])) + ".")
        if not a.no_write:
            print("Wrote %s (one SYMBOL|Name a line)." % a.out)
    return 0 if kept else 1
'''

FULL_SESSIONS = '''
MAX_FILLED = SESSION_BARS // 10      # at most ~10% of a session carried flat


def full_sessions(days, before):
    """Sessions usable as replay material, oldest first, and the ones left out.

    NSE prints no trade at all in some 5-minute slots, even in liquid names, so
    Yahoo sends a null there and upstream's "all 75 slots present" test never
    passes: over 60 sessions of RELIANCE.NS, not one day was complete. A slot
    with no trade is not missing data, it is the price not moving, so those are
    carried flat from the previous close at zero volume. A day missing more
    than MAX_FILLED slots is genuinely broken and is left out instead."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if len(s) < SESSION_BARS - MAX_FILLED:
            short.append({"date": d, "bars": len(s)})
            continue
        seed = s[min(s)]["o"]            # a leading gap carries the first open
        seq, prev_close = [], seed
        for i in range(SESSION_BARS):
            b = s.get(i)
            if b is None:
                b = {"date": d, "slot": i, "start": "", "filled": True,
                     "o": prev_close, "h": prev_close, "l": prev_close,
                     "c": prev_close, "v": 0}
            seq.append(b)
            prev_close = b["c"]
        full.append((d, seq))
    return full, short
'''

STOOQ_BLOCK = '''
def stooq_url(sym):
    raise AlertError("stooq has no NSE coverage: use the default --source yahoo "
                     "for %s" % nse_symbol(sym))
'''


# ------------------------------------------------------------------ the port

def port(src, path, hol_literal, muhurat):
    """src = file text, path = e.g. 'trader-entry/entry.py'."""
    name = os.path.basename(path)
    log = []

    def note(s):
        log.append(s)

    # --- imports we rely on -------------------------------------------------
    src = ensure_import(src, "urllib.parse")
    if name == "scan.py":
        src = ensure_import(src, "csv")
        src = ensure_import(src, "io")

    # --- 1. symbols ---------------------------------------------------------
    anchor = "class %s" % {
        "scan.py": "ScanError", "entry.py": "EntryError", "exit.py": "ExitError",
        "timemachine.py": "TimeMachineError", "size.py": "SizeError",
        "check.py": "RiskError", "alerts.py": "AlertError",
    }.get(name, "")
    m = re.search(r"^class \w+Error\(Exception\):\n    pass\n", src, re.M)
    if not m:
        raise PortError("%s: no error class to anchor the symbol block" % name)
    src = src[:m.end()] + "\n" + SYMBOL_BLOCK.strip("\n") + "\n\n" + src[m.end():]
    note("added nse_symbol() / nse_url_symbol()")

    if name == "entry.py":
        src = replace_func(src, "yahoo_symbol",
                           "def yahoo_symbol(sym):\n    return nse_symbol(sym)\n", name)
        src = sub_once(src,
                       '"/v8/finance/chart/%s?range=%s&interval=%s" % (yahoo_symbol(sym), rng, interval)',
                       '"/v8/finance/chart/%s?range=%s&interval=%s" % (nse_url_symbol(sym), rng, interval)',
                       "entry chart url")
        note("symbol: yahoo_symbol -> NSE, chart url encoded")
    elif name == "scan.py":
        src = replace_func(src, "to_yahoo",
                           "def to_yahoo(sym):\n    return nse_symbol(sym)\n", name)
        src = sub_once(src, '",".join(to_yahoo(s) for s in symbols)',
                       '",".join(nse_url_symbol(s) for s in symbols)', "spark url")
        src = sub_once(src,
                       '"/v8/finance/chart/%s?range=%s&interval=%s" % (to_yahoo(sym), rng, interval)',
                       '"/v8/finance/chart/%s?range=%s&interval=%s" % (nse_url_symbol(sym), rng, interval)',
                       "scan chart url")
        note("symbol: to_yahoo -> NSE, both urls encoded")
    elif name == "news.py":
        src = replace_func(src, "yahoo_symbol",
                           "def yahoo_symbol(ticker):\n    return nse_symbol(ticker)\n", name)
        note("symbol: yahoo_symbol -> NSE")
    elif name == "exit.py":
        src = sub_once(src, 'ysym = sym.upper().replace(".", "-")',
                       'ysym = nse_symbol(sym)', "exit ysym")
        src = sub_once(src,
                       'text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s" % (ysym, rng, interval))',
                       'text = yahoo_get("/v8/finance/chart/%s?range=%s&interval=%s"\n                         % (nse_url_symbol(sym), rng, interval))',
                       "exit chart url")
        note("symbol: ysym -> NSE, chart url encoded")
    elif name == "timemachine.py":
        src = sub_once(src, 'host, urllib.parse.quote(sym.upper().replace(".", "-")))',
                       'host, nse_url_symbol(sym))', "timemachine chart url")
        src = sub_once(src, 'if msym and msym != sym.replace(".", "-"):',
                       'if msym and msym != nse_symbol(sym):', "timemachine meta check")
        note("symbol: chart url + meta check -> NSE")
    elif name == "alerts.py":
        src = replace_func(src, "stooq_url", STOOQ_BLOCK, name)
        src = sub_once(src,
                       '"https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, sym.upper().replace(".", "-"))',
                       '"https://%s/v8/finance/chart/%s?range=1d&interval=1d" % (host, nse_url_symbol(sym))',
                       "alerts chart url")
        note("symbol: quote url -> NSE, stooq disabled")

    if 'replace(".", "-")' in src:
        raise PortError("%s: a US symbol rewrite survived the port" % name)

    # --- 2. the clock -------------------------------------------------------
    # Drop the US versions first: IST_CLOCK below defines all three, so
    # injecting it before the deletions would leave two of each.
    for gone in ("to_eastern", "eastern_to_utc"):
        pat = re.compile(r"^def %s\(.*?(?=^\S)" % gone, re.S | re.M)
        if len(pat.findall(src)) != 1:
            raise PortError("%s: def %s() not found once" % (name, gone))
        src = pat.sub("", src, count=1)
    src = replace_func(src, "eastern_offset", IST_CLOCK, name)
    src = sub_once(src, "def _nth_sunday(year, month, n):",
                   "IST_DELTA = datetime.timedelta(hours=5, minutes=30)\n\n\n"
                   "def _nth_sunday(year, month, n):", "IST_DELTA")
    note("clock: New York DST -> IST (UTC+5:30, no DST)")

    # --- 3. the session -----------------------------------------------------
    if "OPEN = datetime.time(9, 30)" in src:
        src = sub_once(src, "OPEN = datetime.time(9, 30)", "OPEN = datetime.time(9, 15)", "open")
        src = sub_once(src, "CLOSE = datetime.time(16, 0)", "CLOSE = datetime.time(15, 30)", "close")
        note("session: 09:15-15:30")

    # The time machine indexes bars off the session's own geometry, so these
    # have to move too or every entry_bar is wrong by the difference in opens.
    if "SESSION_BARS = 78" in src:
        src = sub_once(src, "SESSION_BARS = 78", "SESSION_BARS = 75", "session bars")
        src = sub_once(src, "OPEN_MIN = 9 * 60 + 30", "OPEN_MIN = 9 * 60 + 15", "open minute")
        src = sub_once(src, "CLOSE_MIN = 16 * 60", "CLOSE_MIN = 15 * 60 + 30", "close minute")
        src = sub_once(src, "EARLY_MIN = 13 * 60",
                       "EARLY_MIN = 15 * 60 + 30  # NSE has no early close; kept equal to the close",
                       "early minute")
        note("session: 75 bars, 09:15-15:30 (NSE gives exactly 75)")
        src = replace_func(src, "full_sessions", FULL_SESSIONS, name)
        note("replays: no-trade slots carried flat (NSE prints gaps)")

    # --- 4. the calendar ----------------------------------------------------
    if "NYSE_HOLIDAYS = {" in src:
        src = replace_dict(src, "NYSE_HOLIDAYS", hol_literal, name)
        src = sub_all(src, "NYSE_HOLIDAYS", "NSE_HOLIDAYS", "holiday refs")
        note("calendar: %d NSE equity holidays" % hol_literal.count(": "))
    if "NYSE_EARLY_CLOSE = {" in src:
        src = replace_dict(src, "NYSE_EARLY_CLOSE",
                           "NSE_EARLY_CLOSE = {}  # NSE has no early closes; Diwali Muhurat is a\n"
                           "                      # separate evening session, not a short day:\n"
                           "                      # %s" % (", ".join(muhurat) or "none listed"),
                           name)
        src = sub_all(src, "NYSE_EARLY_CLOSE", "NSE_EARLY_CLOSE", "early-close refs")
        note("calendar: no early closes (Muhurat noted)")
    src = re.sub(r"CALENDAR_YEARS = \([^)]*\)", "CALENDAR_YEARS = (2026,)", src)

    # --- 5. ticker shape ----------------------------------------------------
    # NSE symbols run longer than 5 and carry & and -: M&M, BAJAJ-AUTO, 360ONE.
    # single backslashes on purpose: this text is written into the ported file
    nse_pat = r'"^[A-Z0-9&\-]{1,20}(\.(NS|BO))?$"'
    for old in (r'TICKER = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,2})?$")',
                r'TICKER = re.compile(r"^[A-Z]{1,5}$|^[A-Z]{1,4}\.[A-Z]$")'):
        if old in src:
            src = sub_once(src, old, "TICKER = re.compile(r%s)" % nse_pat, "ticker regex")
            note("tickers: allow M&M, BAJAJ-AUTO, 360ONE")
    # the time machine validates the plan's symbol inline, 10 chars and no "&"
    if r'r"^[A-Z][A-Z0-9.\-]{0,9}$"' in src:
        src = sub_once(src, r'r"^[A-Z][A-Z0-9.\-]{0,9}$"', "r%s" % nse_pat, "plan symbol regex")
        note("tickers: plan symbol accepts RELIANCE.NS")

    # --- 6. the universe ----------------------------------------------------
    if name == "scan.py":
        src = sub_once(src, 'NASDAQ_FILES = ("nasdaqlisted.txt", "otherlisted.txt")',
                       'NSE_FILES = ("EQUITY_L.csv",)', "universe files")
        src = sub_once(src, 'NASDAQ_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/"',
                       'NSE_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"',
                       "universe url")
        src = replace_func(src, "build_universe", NSE_UNIVERSE.split("def cmd_universe")[0], name)
        src = replace_func(src, "cmd_universe", "def cmd_universe" + NSE_UNIVERSE.split("def cmd_universe")[1], name)
        src = src.replace("a folder with nasdaqlisted.txt and otherlisted.txt",
                          "a saved EQUITY_L.csv (or its folder)")
        note("universe: NSE EQUITY_L.csv, EQ series only")

    # --- 6b. session time literals -----------------------------------------
    # Order matters: the US open is rewritten before the 09:45 default, and
    # the US close last, so each rule sees the text the previous one left.
    src = re.sub(r"\b09:30\b", "09:15", src)                      # US open
    src = re.sub(r"(?<!:)\b9:30\b", "9:15", src)
    src = re.sub(r'("no_entries_before":\s*)"09:45"', r'\1"09:30"', src)
    src = re.sub(r'("no_entries_after":\s*)"15:30"', r'\1"15:00"', src)
    src = re.sub(r'("flat_by":\s*)"15:55"', r'\1"15:20"', src)
    src = src.replace('cfg_flat_by or "15:55"', 'cfg_flat_by or "15:20"')
    src = re.sub(r"\b16:00\b", "15:30", src)                      # US close
    src = re.sub(r"\b4:00 PM\b", "3:30 PM", src)
    src = src.replace("default 15:30", "default 15:00").replace("--until 15:30", "--until 15:00")
    note("times: open 09:15, no entries before 09:30 / after 15:00, flat by 15:20")

    # --- 6c. NSE's WAF wants a browser --------------------------------------
    if name == "scan.py":
        src = sub_once(src, 'def http_get(url, timeout=20):\n'
                            '    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})',
                       'BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "\n'
                       '              "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")\n\n\n'
                       'def http_get(url, timeout=20):\n'
                       '    # nseindia.com sits behind a WAF that stalls on a non-browser agent.\n'
                       '    ua = BROWSER_UA if "nseindia.com" in url else UA\n'
                       '    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})',
                       "browser UA")
        note("universe: browser User-Agent for nseindia.com")

    # --- 7. wording and money ----------------------------------------------
    src = src.replace("New York", "IST").replace("NYSE", "NSE")
    src = src.replace("Nasdaq Trader", "NSE").replace("nasdaqtrader.com", "nseindia.com")
    src = re.sub(r"\bET\b", "IST", src)
    # "$%s", "$5,000" and "${:,.2f}" are money; a bare "$" ending a regex is not.
    src = re.sub(r"\$(?=[%\d{])", "Rs.", src)
    # '"$" + "{:,.2f}"...' is money too; a lone '$"' ending a regex is not.
    src = src.replace('"$" + "', '"Rs." + "')

    # The limit keys are read by name, so the config and the code have to be
    # renamed together: an "_inr" key against "_usd" code is silently ignored
    # and the US defaults apply instead.
    src = src.replace("_usd", "_inr")
    for old, new in (('"max_order_inr": 4000', '"max_order_inr": 60000'),
                     ('"min_order_inr": 5,', '"min_order_inr": 500,'),
                     ('"max_loss_per_trade_inr": 50,', '"max_loss_per_trade_inr": 1000,'),
                     ('"max_daily_loss_inr": 150,', '"max_daily_loss_inr": 3000,'),
                     ('"broker": "alpaca"', '"broker": "none"')):
        src = src.replace(old, new)
    if "_inr" in src:
        note("limits: keys renamed to _inr, defaults scaled to rupees")

    # examples and the broker that cannot serve India
    src = src.replace("TSLA", "RELIANCE").replace("AAPL", "INFY")
    src = src.replace("Alpaca's place_stock_order", "your broker's order API")
    src = src.replace("Alpaca", "your broker")

    # cross-references: the pack's own tools were renamed, and the three that
    # were not ported must stop being pointed at. "trader/" paths keep their
    # name, and none of these patterns can match one.
    # the whole-sentence fixes go first, while the upstream names are intact
    src = src.replace("trader-risk checks it, then trader-broker asks you yes or no.",
                      "nse-risk checks it, then you place it with your broker yourself.")
    # this sentence is split across two source lines, so match the fragment
    src = src.replace('"trader-broker flatten)."', '"close it yourself)."')
    src = src.replace("trader-broker", "your broker")
    for other, (updir, _fn) in TOOLS.items():
        src = src.replace(updir, other)
    for dead in ("trader-news", "trader-analyst", "trader-pelosi"):
        src = src.replace("/" + dead, dead + " (not in this pack)")
    note("cross-references point at the NSE tools")
    note('wording: IST/NSE, "$" -> "Rs."')

    return src, log


# ------------------------------------------------------------------ SKILL.md and config

NSE_CONFIG = {
    "budget": 100000,
    "cash_buffer_pct": 10,
    "max_position_pct": 75,
    "max_order_inr": 60000,
    "min_order_inr": 500,
    "max_loss_per_trade_inr": 1000,
    "max_daily_loss_inr": 3000,
    "max_trades_per_day": 3,
    "max_orders_per_day": 10,
    "min_reward_risk": 1.0,
    "require_stop": True,
    "no_entries_before": "09:30",
    "no_entries_after": "15:00",
    "flat_by": "15:20",
    "fractional": False,
    "broker": "none",
    "paper": True,
    "blocked": [],
    "allow_options": False,
    "allow_margin": False,
    "allow_short": False,
    "pdt_guard": False,
}

BROKER_NOTE = (
    "\n## Placing the order\n\n"
    "There is no broker step in this pack. Upstream ends at Alpaca, which does\n"
    "not serve Indian markets, so the chain stops at the risk check and hands\n"
    "you a ticket to place yourself. Indian brokers with APIs you could wire up:\n"
    "Zerodha Kite Connect, Upstox, Dhan, Angel One SmartAPI, Fyers.\n"
)


def port_skill_md(text, skill, tools):
    """Upstream SKILL.md -> the NSE one."""
    for old, new in sorted(tools.items(), key=lambda kv: -len(kv[1][0])):
        text = text.replace(new[0], old)          # trader-scanner -> nse-scanner
    text = re.sub(r"^name:\s*\S+", "name: " + skill, text, count=1, flags=re.M)
    # the US examples
    text = text.replace("TSLA", "RELIANCE").replace("AAPL", "INFY")
    text = text.replace("New York", "IST").replace("NYSE", "NSE")
    text = text.replace("Nasdaq Trader", "NSE").replace("nasdaqtrader.com", "nseindia.com")
    text = re.sub(r"\bET\b", "IST", text)
    text = re.sub(r"\b09:30\b", "09:15", text)
    text = re.sub(r"(?<!:)\b9:30\b", "9:15", text)
    text = re.sub(r"(?<!:)\b9:45\b", "9:30", text)
    text = re.sub(r"\b15:30\b", "15:00", text)
    text = re.sub(r"\b15:55\b", "15:20", text)
    text = re.sub(r"\b16:00\b", "15:30", text)
    text = re.sub(r"\$(?=[\d{])", "Rs.", text)
    text = text.replace("a IST", "an IST").replace("US-listed", "NSE-listed")
    text = text.replace("_usd", "_inr")
    text = text.replace("Alpaca's place_stock_order", "your broker's order API")
    text = text.replace("Alpaca", "your broker")
    if "nse-broker" in text or "trader-broker" in text:
        text = re.sub(r"/?(nse|trader)-broker", "the risk check", text)
    return text.rstrip("\n") + "\n" + BROKER_NOTE


# ------------------------------------------------------------------ main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Port the US day-trader tools to NSE.")
    ap.add_argument("--src", default=os.path.join(HERE, "..", "day-trader-skill"),
                    help="the upstream checkout")
    ap.add_argument("--out", default=os.path.join(HERE, "skills"))
    ap.add_argument("--holidays", default=os.path.join(HERE, "nse_holidays_raw.json"))
    a = ap.parse_args(argv)

    hol, muhurat, years = nse_holidays(a.holidays)
    lit = holidays_literal(hol, muhurat)
    print("NSE equity calendar: %d holidays, years %s%s" % (
        len(hol), ",".join(years), (" (Muhurat: %s)" % ", ".join(muhurat)) if muhurat else ""))

    os.makedirs(a.out, exist_ok=True)
    for skill, (updir, fname) in sorted(TOOLS.items()):
        ipath = os.path.join(a.src, updir, fname)
        if not os.path.exists(ipath):
            raise PortError("no %s" % ipath)
        with open(ipath, encoding="utf-8") as fh:
            text = fh.read()
        ported, log = port(text, os.path.join(updir, fname), lit, muhurat)
        compile(ported, fname, "exec")           # syntax gate
        d = os.path.join(a.out, skill)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, fname), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(ported)
        sp = os.path.join(a.src, updir, "SKILL.md")
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as fh:
                md = port_skill_md(fh.read(), skill, TOOLS)
            with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(md)
            log.append("SKILL.md rewritten for NSE")
        for extra in ("targets.example.json", "alerts.example.json"):
            p = os.path.join(a.src, updir, extra)
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    ex = fh.read().replace("TSLA", "RELIANCE").replace("AAPL", "INFY")
                ex = re.sub(r'("at":\s*)"15:55"', r'\1"15:20"', ex)
                with open(os.path.join(d, extra), "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(ex)
        if os.path.exists(os.path.join(a.src, updir, "config.example.json")):
            with open(os.path.join(d, "config.example.json"), "w", encoding="utf-8",
                      newline="\n") as fh:
                json.dump(NSE_CONFIG, fh, indent=2)
                fh.write("\n")
            log.append("config.example.json in rupees, NSE hours")
        print("\n%s/%s" % (skill, fname))
        for line in log:
            print("   - " + line)
    print("\nPorted %d tools into %s" % (len(TOOLS), a.out))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PortError as e:
        print("port failed: %s" % e, file=sys.stderr)
        sys.exit(2)
