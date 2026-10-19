"""
Supervised, READ-ONLY Groww capture - the verification step for what Groww's
documentation leaves unclear (see app/services/groww/contract.py): the FAILURE
body, the shape of an EMPTY order list / positions list, the real order_status
values for part-filled / delivery orders, and which quantity field a position
uses.

What it does (only with --execute): obtain one access token with the owner's own
API key and secret (approval flow), then issue three GET requests - order list,
positions, margin. It never creates, modifies or cancels an order, never calls any
other broker, and never writes a token, key, secret or account value to disk
(see angel_one/capture.py: only response shapes and a small enum vocabulary are
kept). It spends one of the token endpoint's 150 daily requests.

Without --execute it is a dry run: it prints what it would do and exits without
any network access.

Prerequisite: Groww requires the API key to be approved on Groww Cloud (API Keys
page) each day. Do that first, or the token request is refused.

    cd backend
    $env:ALICE_BLUE_ENCRYPTION_KEY = <throwaway Fernet key, NOT the production key>
    $env:GROWW_API_KEY = ...   $env:GROWW_API_SECRET = ...
    $env:GROWW_PROXY_URL = <optional: the account's static-IP proxy>
    python scripts/groww_capture.py --execute --i-am-authorized --out groww_capture.json

For the most useful capture run it once on a flat account and once with a position
and a few orders in different states (placed manually in the Groww app).
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _capture_memory_redis import MemoryRedis  # noqa: E402  (isolated in-process store; see its docstring)

REQUIRED_ENV = ("GROWW_API_KEY", "GROWW_API_SECRET")
# (label, path key in groww/contract.ROUTES, query params). Reads only.
READ_ONLY_CALLS = (
    ("order_list", "order_list", {"segment": "CASH", "page_size": 100}),
    ("positions", "positions", {"segment": "CASH"}),
    ("margin", "margin", None),
)


async def capture(out_path: str, http_client=None) -> int:
    """`http_client` exists only so tests can inject a mock transport; main() never passes one."""
    from app.services.angel_one.capture import redact_response
    from app.services.groww.client import GrowwAuthError, GrowwClient
    from app.services.groww.contract import ROUTES
    from app.services.groww.instruments import GrowwInstrumentStore

    client = GrowwClient(
        account_id="capture",
        label="capture",
        api_key=os.environ["GROWW_API_KEY"],
        api_secret=os.environ["GROWW_API_SECRET"],
        redis=MemoryRedis(),
        http_client=http_client,
        static_ip=None,                       # reads only; no order request is ever built here
        proxy_url=os.environ.get("GROWW_PROXY_URL") or None,
        instruments=GrowwInstrumentStore(),
    )
    report = {"note": "shapes only; values other than enum vocabulary are redacted", "endpoints": {}}
    try:
        await client.login()
        for label, route, params in READ_ONLY_CALLS:
            status, data = await client._call("read", "GET", ROUTES[route], params=params)
            report["endpoints"][label] = redact_response(status, data)
    except GrowwAuthError as exc:
        print(f"Login/session failed: {exc}")
        return 2
    finally:
        try:
            await client.logout()
        finally:
            await client.aclose()
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(f"Wrote {out_path}. Review it before sharing; it is git-ignored.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="actually contact Groww (default: dry run)")
    ap.add_argument("--i-am-authorized", action="store_true", help="confirm this is your own Groww account")
    ap.add_argument("--out", default="groww_capture.json")
    args = ap.parse_args()

    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    print("Plan: get one access token, then GET " + ", ".join(label for label, _, _ in READ_ONLY_CALLS)
          + ". No orders. No writes.")
    if not args.execute:
        print("Dry run - nothing contacted." + (f" Missing env: {', '.join(missing)}" if missing else " Environment looks complete."))
        return 0
    if not args.i_am_authorized:
        print("Refusing: pass --i-am-authorized to confirm this is your own account.")
        return 1
    if missing:
        print(f"Refusing: missing environment variables: {', '.join(missing)}")
        return 1
    return asyncio.run(capture(args.out))


if __name__ == "__main__":
    sys.exit(main())
