import math
from requests_oauthlib import OAuth1Session

# ==========================================
# 1. PROVIDE YOUR E*TRADE CREDENTIALS
# ==========================================
CONSUMER_KEY = "a27c3352052528694b41dc5fa06da27e"
CONSUMER_SECRET = "21e1b162a1c5ec468dac9123797f5222a935ff02080cdfb3b6dfcdd52bdb586d"
ACCESS_TOKEN = "CKnpl4couG6HenDG/mQxDD6AnSs4QImoGidp+RUnDqU="
ACCESS_TOKEN_SECRET = "uGp74FF7pH/ackhrqoDurS4s/gQ0wjOUUYEAuW4Lg/E="

etrade = OAuth1Session(
    client_key=CONSUMER_KEY,
    client_secret=CONSUMER_SECRET,
    resource_owner_key=ACCESS_TOKEN,
    resource_owner_secret=ACCESS_TOKEN_SECRET,
    signature_type='auth_header'
)

# Base URL for Production (Change to apisb.etrade.com for Sandbox)
# ==========================================
# STEP 1: FETCH CURRENT/CLOSED SPX SPOT PRICE
# ==========================================
quote_url = "https://api.etrade.com/v1/market/quote/SPX.json"
quote_resp = etrade.get(quote_url)

if quote_resp.status_code == 200:
    quote_data = quote_resp.json()
    all_details = quote_data["QuoteResponse"]["QuoteData"][0]["All"]
    
    # 'lastTrade' holds the final closing print when the market is closed
    spx_price = float(all_details.get("lastTrade", 0.0))
    print(f"Current/Closed SPX Price: ${spx_price:.2f}")
    
    # Dynamically compute the ATM strike (SPX uses $5 increments)
    atm_strike = round(spx_price / 5) * 5
    print(f"Calculated ATM Strike Price: {atm_strike}\n")
    
    url = "https://api.etrade.com/v1/market/optionchains.json"  # prod
    #url = "https://apisb.etrade.com/v1/market/optionchains.json" # sandbox

    params = {
        "symbol": "SPX",
        "expiryYear": 2026,
        "expiryMonth": 7,
        "expiryDay": 10,
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
    
else:
    print(f"Failed to fetch SPX Quote. Status: {quote_resp.status_code}")
    print(quote_resp.text)
    exit()
