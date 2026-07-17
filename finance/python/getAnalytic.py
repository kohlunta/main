"""
E*TRADE options analytics module.

Consolidates:
- OAuth1 credential loading
- SPX ATM IV retrieval
- Put/call ratio (volume + OI)
- Call/put wall detection (max OI per side)
- SPX last close / last trade lookup

Usage:
    from etrade_analytics import get_session, get_spx_last_close, get_walls, get_atm_iv, get_put_call_ratio

    session, base_url = get_session()
    print(get_spx_last_close(session, base_url))
"""

import json
import os
from pathlib import Path
from requests_oauthlib import OAuth1Session

import datetime
from zoneinfo import ZoneInfo

def unix_to_pdt(ts):
    """Converts a Unix timestamp (seconds) to Pacific time."""
    dt = datetime.datetime.fromtimestamp(ts, tz=ZoneInfo("America/Los_Angeles"))
    return dt.strftime("%Y-%m-%d %I:%M:%S %p %Z")

# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

def load_credentials():
    """
    Loads E*TRADE OAuth1 credentials, checking in order:
      1. ETRADE_CREDENTIALS env var (path to JSON file)
      2. ./credentials.json in the current working directory
      3. ~/.etrade/credentials.json

    Expected JSON shape:
        {
            "consumer_key": "...",
            "consumer_secret": "...",
            "oauth_token": "...",
            "oauth_token_secret": "...",
            "base_url": "https://api.etrade.com"
        }
    """
    candidates = []

    env_path = os.environ.get("ETRADE_CREDENTIALS")
    if env_path:
        candidates.append(Path(env_path))

    candidates.append(Path.cwd() / "credentials.json")
    candidates.append(Path.home() / ".etrade" / "credentials.json")

    for path in candidates:
        if path and path.is_file():
            with open(path, "r") as f:
                creds = json.load(f)
            required = ["consumer_key", "consumer_secret", "oauth_token", "oauth_token_secret"]
            missing = [k for k in required if k not in creds]
            if missing:
                raise ValueError(f"Credentials file {path} missing keys: {missing}")
            creds.setdefault("base_url", "https://api.etrade.com")
            return creds

    raise FileNotFoundError(
        "No credentials found. Set ETRADE_CREDENTIALS env var, or place a "
        "credentials.json in the cwd or ~/.etrade/credentials.json"
    )


def get_session():
    """Returns an authenticated OAuth1Session and the base API URL."""
    creds = load_credentials()
    session = OAuth1Session(
        client_key=creds["consumer_key"],
        client_secret=creds["consumer_secret"],
        resource_owner_key=creds["oauth_token"],
        resource_owner_secret=creds["oauth_token_secret"],
    )
    return session, creds["base_url"]


# ---------------------------------------------------------------------------
# Quotes
# ---------------------------------------------------------------------------

def get_spx_last_close(session, base_url, symbol="SPX"):
    """
    Returns the previous session's closing value for SPX, plus the most
    recent traded price (live if market is open, today's close if closed).
    """
    url = f"{base_url}/v1/market/quote/{symbol}"
    resp = session.get(url, headers={"Accept": "application/json"})
    resp.raise_for_status()
    data = resp.json()

    quote_data = data["QuoteResponse"]["QuoteData"][0]
    all_data = quote_data["All"]

    last_close = all_data.get("previousClose")
    last_trade = all_data.get("lastTrade")
    change = last_trade - last_close
    change_pct = (change / last_close) * 100

    return {
        "symbol": symbol,
        "last close": last_close,
        "last trade": last_trade,
        "change": change,
        "change_pct": change_pct,
        "date time": unix_to_pdt(quote_data.get("dateTimeUTC")),
    }


# ---------------------------------------------------------------------------
# Option chain helpers
# ---------------------------------------------------------------------------

def get_option_chain(session, base_url, symbol, expiry_year, expiry_month, expiry_day,
                      strike_count=40, price_type="ALL"):
    """
    Fetches the option chain for a specific expiration.
    strike_count: number of strikes to return above/below the underlying price.
    """
    url = f"{base_url}/v1/market/optionchains"
    params = {
        "symbol": symbol,
        "expiryYear": expiry_year,
        "expiryMonth": expiry_month,
        "expiryDay": expiry_day,
        "strikeCount": strike_count,
        "priceType": price_type,
        "includeWeekly": "true",
    }
    resp = session.get(url, params=params, headers={"Accept": "application/json"})
    resp.raise_for_status()
    return resp.json()


def _parse_chain_pairs(chain_json):
    """
    Flattens the raw E*TRADE optionchains response into a list of dicts:
    [{'strike': 7550.0, 'call_oi': 1200, 'put_oi': 800,
      'call_volume': 300, 'put_volume': 150, 'call_iv': 0.121, 'put_iv': 0.118}, ...]
    """
    pairs = chain_json.get("OptionChainResponse", {}).get("OptionPair", [])
    rows = []
    for pair in pairs:
        call = pair.get("Call", {})
        put = pair.get("Put", {})
        strike = call.get("strikePrice") or put.get("strikePrice")
        rows.append({
            "strike": strike,
            "call_oi": call.get("openInterest", 0) or 0,
            "put_oi": put.get("openInterest", 0) or 0,
            "call_volume": call.get("volume", 0) or 0,
            "put_volume": put.get("volume", 0) or 0,
            "call_iv": (call.get("OptionGreeks") or {}).get("iv"),
            "put_iv": (put.get("OptionGreeks") or {}).get("iv"),
        })
    return rows


def get_walls(chain_json, spot_price=None, strike_range_pct=0.05):
    """
    Computes call wall / put wall from raw open interest.

    spot_price: if provided, restricts consideration to strikes within
        +/- strike_range_pct of spot, to avoid stale far-OTM OI dominating.
    """
    rows = _parse_chain_pairs(chain_json)

    #print(f"\nDEBUG: spot_price={spot_price}, total rows before filter={len(rows)}")
    #if rows:
    #    print(f"DEBUG: strike range in data = {min(r['strike'] for r in rows if r['strike'])} to {max(r['strike'] for r in rows if r['strike'])}")

    if spot_price:
        lo = spot_price * (1 - strike_range_pct)
        hi = spot_price * (1 + strike_range_pct)
        rows = [r for r in rows if r["strike"] and lo <= r["strike"] <= hi]

    if not rows:
        raise ValueError("No strikes available after filtering — widen strike_range_pct.")

    call_wall = max(rows, key=lambda r: r["call_oi"])
    put_wall = max(rows, key=lambda r: r["put_oi"])

    return {
        "call wall strike": call_wall["strike"],
        "call wall oi": call_wall["call_oi"],
        "put wall strike": put_wall["strike"],
        "put wall oi": put_wall["put_oi"],
    }


def get_put_call_ratio(chain_json):
    """Returns put/call ratio by both volume and open interest."""
    rows = _parse_chain_pairs(chain_json)

    total_call_oi = sum(r["call_oi"] for r in rows)
    total_put_oi = sum(r["put_oi"] for r in rows)
    total_call_vol = sum(r["call_volume"] for r in rows)
    total_put_vol = sum(r["put_volume"] for r in rows)

    return {
        "pc ratio oi": (total_put_oi / total_call_oi) if total_call_oi else None,
        "pc ratio volume": (total_put_vol / total_call_vol) if total_call_vol else None,
        "total call oi": total_call_oi,
        "total put oi": total_put_oi,
        "total call volume": total_call_vol,
        "total put volume": total_put_vol,
    }


def get_atm_iv(chain_json, spot_price, max_staleness_seconds=300):
    rows = _parse_chain_pairs(chain_json)
    rows = [r for r in rows if r["strike"] is not None]
    atm = min(rows, key=lambda r: abs(r["strike"] - spot_price))

    # also grab the raw pair to check timestamp freshness
    pairs = chain_json["OptionChainResponse"]["OptionPair"]
    atm_pair = min(pairs, key=lambda p: abs((p["Call"] or p["Put"])["strikePrice"] - spot_price))
    call_ts = atm_pair["Call"]["timeStamp"]
    quote_ts = chain_json["OptionChainResponse"].get("timeStamp", call_ts)  # adjust if available

    is_stale = abs(quote_ts - call_ts) > max_staleness_seconds if quote_ts else None

    return {
        "strike": atm["strike"],
        "call_iv": atm["call_iv"],
        "put_iv": atm["put_iv"],
        "greeks_dateTime": unix_to_pdt(call_ts),
        "possibly_stale": is_stale,
    }

def print_pc_ratio(pc):
    oi_ratio = f"{pc['pc ratio oi']:.2f}" if pc['pc ratio oi'] is not None else "N/A"
    vol_ratio = f"{pc['pc ratio volume']:.2f}" if pc['pc ratio volume'] is not None else "N/A"

    print(
        f"\nPut/Call Ratio\n"
        f"  OI Ratio:       {oi_ratio}\n"
        f"  Volume Ratio:   {vol_ratio}\n"
        f"  Total Call OI:  {pc['total call oi']:,}\n"
        f"  Total Put OI:   {pc['total put oi']:,}\n"
        f"  Total Call Vol: {pc['total call volume']:,}\n"
        f"  Total Put Vol:  {pc['total put volume']:,}"
    )

def print_atm_iv(iv):
    stale_flag = " ⚠ STALE" if iv['possibly_stale'] else ""
    
    print(
        f"\nATM IV (Strike: {iv['strike']:,})\n"
        f"  Call IV:      {iv['call_iv']:.2%}\n"
        f"  Put IV:       {iv['put_iv']:.2%}\n"
        f"  Greeks Time:  {iv['greeks_dateTime']}{stale_flag}"
    )
    
# ---------------------------------------------------------------------------
# Example end-to-end usage
# ---------------------------------------------------------------------------

import argparse
import datetime

def parse_args():
    parser = argparse.ArgumentParser(description="SPX options analytics")
    parser.add_argument("--s", default="SPX", help="Underlying symbol (default: SPX)")
    parser.add_argument("--y", type=int, help="Expiry year (default: today)")
    parser.add_argument("--m", type=int, help="Expiry month (default: today)")
    parser.add_argument("--d", type=int, help="Expiry day (default: today)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    session, base_url = get_session()

    quote = get_spx_last_close(session, base_url)
    print(f"\nSPX Quote ({quote['date time']})\n"
          f"  Last Trade:   {quote['last trade']:,}\n"
          f"  Last Close:   {quote['last close']:,}\n"
          f"  Change:       {quote['change']:+,.2f} ({quote['change_pct']:+.2f}%)")

    today = datetime.date.today()
    expiry_year = args.y or today.year
    expiry_month = args.m or today.month
    expiry_day = args.d or today.day

    chain = get_option_chain(
        session, base_url,
        symbol=args.s,
        expiry_year=expiry_year,
        expiry_month=expiry_month,
        expiry_day=expiry_day,
        strike_count=50
    )


    #print("DEBUG chain keys:", list(chain.keys()))
    #print("DEBUG chain content:", json.dumps(chain, indent=2)[:2000]) 

    spot = quote["last trade"] or quote["last close"]

    walls = get_walls(chain, spot_price=spot)
    expiry_date = datetime.date(expiry_year, expiry_month, expiry_day)
    
    print(f"\n\nSPX {expiry_date.strftime('%m-%d-%Y')}")
    print(f"Call Wall: {walls['call wall strike']} (OI: {walls['call wall oi']:,})\n" f" Put Wall: {walls['put wall strike']} (OI: {walls['put wall oi']:,})")
    print_pc_ratio(get_put_call_ratio(chain))
    print_atm_iv(get_atm_iv(chain, spot_price=spot))
    


