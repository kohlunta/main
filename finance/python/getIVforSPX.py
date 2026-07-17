import json
import math
from pathlib import Path
from requests_oauthlib import OAuth1Session

# ==========================================
# 1. LOAD CREDENTIALS FROM credentials.json
# ==========================================
def load_credentials():
    """
    Loads E*TRADE OAuth1 credentials from credentials.json in the
    current working directory.

    Expected JSON shape:
        {
            "consumer_key": "...",
            "consumer_secret": "...",
            "oauth_token": "...",
            "oauth_token_secret": "..."
        }
    """
    path = Path.cwd() / "credentials.json"
    if not path.is_file():
        raise FileNotFoundError(f"credentials.json not found in {Path.cwd()}")

    with open(path, "r") as f:
        creds = json.load(f)

    required = ["consumer_key", "consumer_secret", "oauth_token", "oauth_token_secret"]
    missing = [k for k in required if k not in creds]
    if missing:
        raise ValueError(f"credentials.json missing keys: {missing}")

    return creds


creds = load_credentials()

etrade = OAuth1Session(
    client_key=creds["consumer_key"],
    client_secret=creds["consumer_secret"],
    resource_owner_key=creds["oauth_token"],
    resource_owner_secret=creds["oauth_token_secret"],
    signature_type="auth_header"
)

# ==========================================
# STEP 1: FETCH CURRENT/CLOSED SPX SPOT PRICE
# ==========================================
quote_url = "https://api.etrade.com/v1/market/quote/SPX.json"
quote_resp = etrade.get(quote_url)

if quote_resp.status_code != 200:
    print(f"Failed to fetch SPX Quote. Status: {quote_resp.status_code}")
    print(quote_resp.text)
    exit()

quote_data = quote_resp.json()
all_details = quote_data["QuoteResponse"]["QuoteData"][0]["All"]

# 'lastTrade' holds the final closing print when the market is closed
spx_price = float(all_details.get("lastTrade", 0.0))
print(f"Current/Closed SPX Price: ${spx_price:.2f}")

# Dynamically compute the ATM strike (SPX uses $5 increments)
atm_strike = round(spx_price / 5) * 5
print(f"Calculated ATM Strike Price: {atm_strike}\n")

# ==========================================
# STEP 2: FETCH OPTION CHAIN AROUND ATM STRIKE
# ==========================================
url = "https://api.etrade.com/v1/market/optionchains.json"  # prod
# url = "https://apisb.etrade.com/v1/market/optionchains.json"  # sandbox

params = {
    "symbol": "SPX",
    "expiryYear": 2026,
    "expiryMonth": 7,
    "expiryDay": 17,
    "strikePriceNear": atm_strike,
    "noOfStrikes": 4,
    "includeWeekly": "true"
}

resp = etrade.get(url, params=params)
data = resp.json()

for pair in data["OptionChainResponse"]["OptionPair"]:
    call = pair["Call"]
    put = pair["Put"]
    call_iv = call["OptionGreeks"]["iv"]
    put_iv = put["OptionGreeks"]["iv"]
    print(f"Strike {call['strikePrice']}: Call IV={call_iv:.4f}  Put IV={put_iv:.4f} IV={(call_iv + put_iv)/2:.4f}")