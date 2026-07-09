"""
E*TRADE OAuth 1.0a authorization flow.
Run this once per session to get your access token (resource_owner_key)
and access token secret (resource_owner_secret).

These expire at midnight US Eastern time, and go inactive after
2 hours of no API calls (use the Renew Access Token endpoint to
reactivate if that happens mid-session).
"""

import os
from rauth import OAuth1Service

# --- Set these, or export as environment variables ---
CONSUMER_KEY = os.environ.get("ETRADE_CONSUMER_KEY", "a27c3352052528694b41dc5fa06da27e")
CONSUMER_SECRET = os.environ.get("ETRADE_CONSUMER_SECRET", "21e1b162a1c5ec468dac9123797f5222a935ff02080cdfb3b6dfcdd52bdb586d")

SANDBOX = False  # flip to True if testing with sandbox keys
BASE_URL = "https://apisb.etrade.com" if SANDBOX else "https://api.etrade.com"

REQUEST_TOKEN_URL = BASE_URL + "/oauth/request_token"
ACCESS_TOKEN_URL = BASE_URL + "/oauth/access_token"
AUTHORIZE_URL = "https://us.etrade.com/e/t/etws/authorize?key={}&token={}"


def get_etrade_session():
    etrade = OAuth1Service(
        name="etrade",
        consumer_key=CONSUMER_KEY,
        consumer_secret=CONSUMER_SECRET,
        request_token_url=REQUEST_TOKEN_URL,
        access_token_url=ACCESS_TOKEN_URL,
        authorize_url=AUTHORIZE_URL,
        base_url=BASE_URL,
    )

    # Step 1: get a temporary request token (valid 5 minutes)
    request_token, request_token_secret = etrade.get_request_token(
        params={"oauth_callback": "oob", "format": "json"}
    )

    # Step 2: send yourself to E*TRADE to authorize the app
    auth_url = etrade.authorize_url.format(etrade.consumer_key, request_token)
    print("\nOpen this URL in your browser and log in to E*TRADE:")
    print(auth_url)
    verifier = input("\nAfter approving, paste the verification code shown: ").strip()

    # Step 3: exchange the verified request token for a real access token
    session = etrade.get_auth_session(
        request_token,
        request_token_secret,
        params={"oauth_verifier": verifier},
    )

    # These are what you want:
    resource_owner_key = session.access_token
    resource_owner_secret = session.access_token_secret

    print("\n--- Save these for your session (expires midnight ET) ---")
    print(f"resource_owner_key:    {resource_owner_key}")
    print(f"resource_owner_secret: {resource_owner_secret}")

    return session, resource_owner_key, resource_owner_secret


if __name__ == "__main__":
    session, key, secret = get_etrade_session()

    # Quick sanity check — pull an SPX quote to confirm the session works
    resp = session.get(
        BASE_URL + "/v1/market/quote/SPX.json",
        params={"format": "json"},
    )
    print("\nTest call status:", resp.status_code)