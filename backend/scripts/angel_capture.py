"""
Supervised, READ-ONLY Angel One capture - the verification step that unblocks
angel_one/contract.py.

What it does (only with --execute): log in once with the owner's own credentials
and issue three GET requests - order book, positions, RMS (funds). It never
places, modifies or cancels an order, never calls any other broker, and never
writes a token, credential or account value to disk (see angel_one/capture.py:
only response shapes and a small enum vocabulary are kept).

Without --execute it is a dry run: it prints what it would do and exits without
any network access.

Run it only on the authorized Angel One account, from the machine whose public
IP is registered for that account (Angel One rejects other IPs), outside market
hours or with no open orders being managed by Jettone:

    cd backend
    export ANGEL_CLIENT_CODE=...  ANGEL_API_KEY=...  ANGEL_PIN=...  ANGEL_TOTP_SECRET=...
    export ANGEL_STATIC_IP=<the account's registered public IP>
    export ANGEL_PROXY_URL=<optional: the account's static-IP proxy>
    python scripts/angel_capture.py --execute --i-am-authorized --out angel_capture.json
"""

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REQUIRED_ENV = ("ANGEL_CLIENT_CODE", "ANGEL_API_KEY", "ANGEL_PIN", "ANGEL_TOTP_SECRET", "ANGEL_STATIC_IP")
READ_ONLY_CALLS = (("order_book", "GET"), ("positions", "GET"), ("rms", "GET"))


class _MemoryRedis:
    """Throw-away session store: nothing is persisted anywhere."""

    def __init__(self):
        self._d = {}

    async def get(self, key):
        return self._d.get(key)

    async def set(self, key, value, ex=None):
        self._d[key] = value

    async def delete(self, key):
        self._d.pop(key, None)


async def capture(out_path: str) -> int:
    from app.services.angel_one.capture import redact_response
    from app.services.angel_one.client import AngelOneClient, AngelAuthError
    from app.services.angel_one.contract import AngelContract
    from app.services.angel_one.instruments import AngelInstrumentStore

    client = AngelOneClient(
        account_id="capture",
        client_code=os.environ["ANGEL_CLIENT_CODE"],
        api_key=os.environ["ANGEL_API_KEY"],
        pin=os.environ["ANGEL_PIN"],
        totp_secret=os.environ["ANGEL_TOTP_SECRET"],
        redis=_MemoryRedis(),
        static_ip=os.environ["ANGEL_STATIC_IP"],
        proxy_url=os.environ.get("ANGEL_PROXY_URL") or None,
        contract=AngelContract(),
        instruments=AngelInstrumentStore(),
    )
    report = {"note": "shapes only; values other than enum vocabulary are redacted", "endpoints": {}}
    try:
        await client.login()
        for route, method in READ_ONLY_CALLS:
            raw = await client._call(route, None, method=method)
            status = raw.pop("_http_status", 0)
            report["endpoints"][route] = redact_response(status, raw)
    except AngelAuthError as exc:
        print(f"Login/session failed: {exc}")
        return 2
    finally:
        try:
            await client.logout()
        finally:
            await client.aclose()
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(f"Wrote {out_path}. Review it, then use it to fill angel_one/contract.py (see docs).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="actually contact Angel One (default: dry run)")
    ap.add_argument("--i-am-authorized", action="store_true", help="confirm this is your own Angel One account")
    ap.add_argument("--out", default="angel_capture.json")
    args = ap.parse_args()

    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    print("Plan: log in once, then GET " + ", ".join(r for r, _ in READ_ONLY_CALLS) + ". No orders. No writes.")
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
