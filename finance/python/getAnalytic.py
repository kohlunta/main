"""
E*TRADE Options Analytics Module

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

import argparse
import datetime
import json
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo
from requests_oauthlib import OAuth1Session

# ===========================================================================
# 1. SETUP & UTILITY HELPERS
# ===========================================================================

def unix_to_pdt(ts):
    """
    Converts a Unix epoch timestamp (seconds) into a human-readable 
    Pacific Time string (handling PDT/PST daylight savings dynamically).
    """
    dt = datetime.datetime.fromtimestamp(ts, tz=ZoneInfo("America/Los_Angeles"))
    return dt.strftime("%Y-%m-%d %I:%M:%S %p %Z")


# ===========================================================================
# 2. E*TRADE OAUTH1 AUTHENTICATION
# ===========================================================================

def load_credentials():
    """
    Loads E*TRADE OAuth1 credentials from disk. Checks three locations in order:
      1. Environment variable: ETRADE_CREDENTIALS (path to JSON file)
      2. Active working directory: ./credentials.json
      3. Home directory directory: ~/.etrade/credentials.json

    Expected JSON schema:
        {
            "consumer_key": "...",
            "consumer_secret": "...",
            "oauth_token": "...",
            "oauth_token_secret": "...",
            "base_url": "https://api.etrade.com"
        }
    """
    candidates = []

    # Check for custom env path overrides first
    env_path = os.environ.get("ETRADE_CREDENTIALS")
    if env_path:
        candidates.append(Path(env_path))

    # Standard fallback paths
    candidates.append(Path.cwd() / "credentials.json")
    candidates.append(Path.home() / ".etrade" / "credentials.json")

    # Search candidates and parse the first valid file found
    for path in candidates:
        if path and path.is_file():
            with open(path, "r") as f:
                creds = json.load(f)
            
            # Verify all required OAuth handshake keys are present
            required = ["consumer_key", "consumer_secret", "oauth_token", "oauth_token_secret"]
            missing = [k for k in required if k not in creds]
            if missing:
                raise ValueError(f"Credentials file {path} missing keys: {missing}")
            
            # Fall back to live production API endpoint if base_url is not defined
            creds.setdefault("base_url", "https://api.etrade.com")
            return creds

    raise FileNotFoundError(
        "No credentials found. Set ETRADE_CREDENTIALS env var, or place a "
        "credentials.json in the cwd or ~/.etrade/credentials.json"
    )


def get_session():
    """
    Constructs and returns an authenticated OAuth1Session container 
    and the corresponding base API URL for executing requests.
    """
    creds = load_credentials()
    session = OAuth1Session(
        client_key=creds["consumer_key"],
        client_secret=creds["consumer_secret"],
        resource_owner_key=creds["oauth_token"],
        resource_owner_secret=creds["oauth_token_secret"],
    )
    return session, creds["base_url"]


# ===========================================================================
# 3. MARKET DATA QUOTES
# ===========================================================================

def get_spx_last_close(session, base_url, symbol="SPX"):
    """
    Queries the E*TRADE quote engine to fetch current session metrics:
    - Previous day's closing price.
    - Last traded execution price (real-time when market is open).
    - Calculated intraday points & percentage change.
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


# ===========================================================================
# 4. OPTION CHAIN PROCESSING
# ===========================================================================

def get_option_chain(session, base_url, symbol, expiry_year, expiry_month, expiry_day,
                     strike_count=40, price_type="ALL"):
    """
    Queries option chains for a specific underlying symbol and exact expiration date.
    Defenders of short vertical structures should pull both CALL and PUT data.
    """
    url = f"{base_url}/v1/market/optionchains"
    params = {
        "symbol": symbol.upper(),
        "expiryYear": expiry_year,
        "expiryMonth": expiry_month,
        "expiryDay": expiry_day,
        "noOfStrikes": strike_count,
        "includeWeekly": "true",
        "priceType": price_type,
        "chainType": "CALLPUT",
    }
    
    resp = session.get(url, params=params, headers={"Accept": "application/json"})
    if not resp.ok:
        print("Status:", resp.status_code)
        print("Error body:", resp.text)
    resp.raise_for_status()
    
    return resp.json()


def _parse_chain_pairs(chain_json, root_symbol="SPXW"):
    """
    Private parser that flattens nested E*TRADE Call/Put pairs into a flat list.
    
    Filters by 'root_symbol' (e.g., SPXW for weekly series vs SPX for AM-settled monthlies)
    to prevent overlapping strikes from duplicating Open Interest calculations.
    """
    pairs = chain_json.get("OptionChainResponse", {}).get("OptionPair", [])
    rows = []
    
    for pair in pairs:
        call = pair.get("Call", {})
        put = pair.get("Put", {})

        call_root = call.get("optionRootSymbol")
        put_root = put.get("optionRootSymbol")
        
        # Enforce strict root filter
        if root_symbol and call_root != root_symbol and put_root != root_symbol:
            continue

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


def get_walls(chain_json, spot_price=None, strike_range_pct=0.05, root_symbol="SPXW"):
    """
    Scans option chain open interest (OI) to locate major Call and Put Walls.

    spot_price: Optional. When supplied, filters out strikes beyond a specific
                 percentage range (e.g., +/- 5%) to prevent stale, ultra-deep-OTM
                 legacy open interest from distorting current support/resistance analysis.
    """
    rows = _parse_chain_pairs(chain_json, root_symbol=root_symbol)

    # Filter by percentage range around spot if active
    if spot_price:
        lo = spot_price * (1 - strike_range_pct)
        hi = spot_price * (1 + strike_range_pct)
        rows = [r for r in rows if r["strike"] and lo <= r["strike"] <= hi]

    if not rows:
        raise ValueError("No strikes available after filtering — widen strike_range_pct.")

    # Locate strikes containing absolute maximum Open Interest
    call_wall = max(rows, key=lambda r: r["call_oi"])
    put_wall = max(rows, key=lambda r: r["put_oi"])

    return {
        "call wall strike": call_wall["strike"],
        "call wall oi": call_wall["call_oi"],
        "put wall strike": put_wall["strike"],
        "put wall oi": put_wall["put_oi"],
    }


def get_put_call_ratio(chain_json, root_symbol="SPXW"):
    """
    Aggregates overall option volumes and Open Interest to calculate 
    put-to-call sentiment indicators for the selected expiration series.
    """
    rows = _parse_chain_pairs(chain_json, root_symbol=root_symbol)

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


def get_atm_iv(chain_json, spot_price, max_staleness_seconds=300, root_symbol="SPXW"):
    """
    Finds the At-the-Money (ATM) option strike closest to the spot price
    and extracts its associated implied volatility (IV).

    Also checks the exchange's update timestamp against the system clock to flag 
    delayed options pricing or stale data feeds during low-liquidity periods.
    """
    rows = _parse_chain_pairs(chain_json, root_symbol=root_symbol)
    rows = [r for r in rows if r["strike"] is not None]
    
    # Locate closest mathematical absolute strike to current spot
    atm = min(rows, key=lambda r: abs(r["strike"] - spot_price))

    # Retrieve timestamps directly from raw option pair records
    pairs = chain_json["OptionChainResponse"]["OptionPair"]
    weekly_pairs = [p for p in pairs if p["Call"].get("optionRootSymbol") == root_symbol]
    atm_pair = min(weekly_pairs, key=lambda p: abs(p["Call"]["strikePrice"] - spot_price))
    call_ts = atm_pair["Call"]["timeStamp"]

    # Calculate staleness threshold
    now_ts = time.time()
    is_stale = (now_ts - call_ts) > max_staleness_seconds

    return {
        "strike": atm["strike"],
        "call_iv": atm["call_iv"],
        "put_iv": atm["put_iv"],
        "greeks_dateTime": unix_to_pdt(call_ts),
        "possibly_stale": is_stale,
    }


# ===========================================================================
# 5. FORMATTING & PRINT STYLING
# ===========================================================================

def print_pc_ratio(pc):
    """Prints a clean, formatted block comparing Open Interest and Vol Ratios."""
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
    """Prints ATM Option strike IV, complete with automated staleness warning signs."""
    stale_flag = " ⚠ STALE" if iv['possibly_stale'] else ""
    
    print(
        f"\nATM IV (Strike: {iv['strike']:,})\n"
        f"  Call IV:      {iv['call_iv']:.2%}\n"
        f"  Put IV:       {iv['put_iv']:.2%}\n"
        f"  Greeks Time:  {iv['greeks_dateTime']}{stale_flag}"
        f"\n"
    )


# ===========================================================================
# 6. ARGUMENT PARSING & MAIN RUNNER
# ===========================================================================

def parse_args():
    """Handles terminal commands and dynamically falls back to today's date if empty."""
    parser = argparse.ArgumentParser(description="SPX options analytics")
    parser.add_argument("--s", default="SPX", help="Underlying symbol (default: SPX)")
    parser.add_argument("--y", type=int, help="Expiry year (default: today)")
    parser.add_argument("--m", type=int, help="Expiry month (default: today)")
    parser.add_argument("--d", type=int, help="Expiry day (default: today)")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # 1. Start Authenticated API Session
    session, base_url = get_session()

    # 2. Fetch Spot Quote Baseline
    quote = get_spx_last_close(session, base_url)
    print(f"\nSPX Quote ({quote['date time']})\n"
          f"  Last Trade:   {quote['last trade']:,}\n"
          f"  Last Close:   {quote['last close']:,}\n"
          f"  Change:       {quote['change']:+,.2f} ({quote['change_pct']:+.2f}%)")

    # 3. Determine Option Target Date
    today = datetime.date.today()
    expiry_year = args.y or today.year
    expiry_month = args.m or today.month
    expiry_day = args.d or today.day
    expiry_date = datetime.date(expiry_year, expiry_month, expiry_day)

    # 4. Retrieve Complete Chain
    chain = get_option_chain(
        session, base_url,
        symbol=args.s,
        expiry_year=expiry_year,
        expiry_month=expiry_month,
        expiry_day=expiry_day,
        strike_count=50
    )

    # Set active spot price for mathematical distance calculations
    spot = quote["last trade"] or quote["last close"]

    # 5. Extract Analytics and Print Results
    walls = get_walls(chain, spot_price=spot)
    
    print(f"\nCall Wall: {walls['call wall strike']} (OI: {walls['call wall oi']:,})\n" 
          f" Put Wall: {walls['put wall strike']} (OI: {walls['put wall oi']:,})")
          
    print_pc_ratio(get_put_call_ratio(chain))
    print_atm_iv(get_atm_iv(chain, spot_price=spot))