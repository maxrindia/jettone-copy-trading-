"""
Supervised Groww ORDER-LIFECYCLE check - verification of what Groww's documentation leaves unproven for the Alice Blue -> Groww
pair: placing, looking up by reference, modifying and cancelling ONE quantity-one LIMIT BUY, and reconciling positions and funds.

It drives GrowwClient directly with an isolated in-memory store. It never touches the Jettone database, Redis, copy engine, allowlist,
capability ceilings or verification flags, and it never calls another broker. A PASS here records what this script observed; it does
not verify anything by itself and changes no flag.

    *** A LIMIT ORDER PRICED AWAY FROM THE MARKET IS NOT GUARANTEED TO STAY UNFILLED. ***
    Prices move, gap and cross. The order can fill (fully or partly) before the script modifies or cancels it, and a fill is a real
    position and a real loss. The script reads the order's state before and after every action and reports a fill loudly, but it
    cannot prevent one. The operator-approved risk limit below is the most the operator accepts to lose; set it accordingly.

Dry run is the default: nothing is contacted and no client is built. Nothing below has a default - every risk input must be given:

    cd backend
    $env:ALICE_BLUE_ENCRYPTION_KEY = <throwaway Fernet key, NOT the production key>   (only if your environment requires it)
    $env:GROWW_API_KEY = ...   $env:GROWW_API_SECRET = ...      # never printed, never written
    $env:GROWW_PROXY_URL = <optional: the account's static-IP proxy>
    python scripts/groww_order_lifecycle.py --exchange NSE --symbol <SYMBOL> --quantity 1 --product CNC ^
        --limit-price <P> --modify-price <P2> --reference-price <LTP> --max-notional <INR> --risk-limit <INR> ^
        --static-ip <registered IP> --instrument-master <groww instruments csv>            # dry run: prints the plan only
    ... add  --execute --i-am-authorized  to go live (then you must also type two confirmations).

Inputs
  --exchange / --symbol   the instrument; it must resolve in the Groww instrument master you pass (NSE/BSE cash equity only).
  --quantity              must be exactly 1.
  --product               CNC or MIS (the only products the adapter maps for equity).
  --limit-price           the BUY limit price. Must be BELOW --reference-price.
  --modify-price          the price the order is modified to. Also BELOW --reference-price, and different from --limit-price.
  --reference-price       the last traded price YOU looked up. The script has no market data; it only refuses a price at/above it.
  --max-notional          upper bound for price x quantity of every order the script sends.
  --risk-limit            the loss you have approved. The script cannot compute a loss, so it conservatively requires the whole
                          notional (price x quantity) to be within it.
  --static-ip             the static IP registered with Groww for this account. The script cannot check it is the one in use.
                          Documentation addresses (203.0.113.x, 198.51.100.x, 192.0.2.x) are placeholders, never a real test IP;
                          live mode refuses any non-public address.
  --instrument-master     a local CSV of Groww's instrument master. Nothing is downloaded.

Outcomes and exit codes
  0 every step observed as expected     1 refused (missing/invalid input, no authorisation, confirmation not given)
  2 login failed     3 placement AMBIGUOUS - manual inspection required (nothing is resent, nothing is cancelled)
  4 order FILLED or PART-FILLED - the sequence stops; a position exists; inspect and manage it by hand     5 cancellation NOT confirmed - cancel by hand
  6 another step failed or the reconciliation did not match
An ambiguous placement is never resent. The cancel is sent at most once and is never assumed to have worked: the order is re-read.
"""

import argparse
import asyncio
import ipaddress
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _capture_memory_redis import MemoryRedis  # noqa: E402  (isolated in-process store; see its docstring)

REQUIRED_ENV = ("GROWW_API_KEY", "GROWW_API_SECRET")
LIMITATION = (
    "A limit order priced away from the market is NOT guaranteed to stay unfilled: it can fill, fully or partly, before it is "
    "modified or cancelled. A fill is a real position and a real loss; the risk limit is the most you accept to lose."
)
READ_ONLY_CALLS = (
    ("order_list", "order_list", {"segment": "CASH", "page_size": 100}),
    ("positions", "positions", {"segment": "CASH"}),
    ("margin", "margin", None),
)
CONFIRM_ATTEMPTS = 5            # bounded read-only re-checks after a modify or a cancel
CONFIRM_INTERVAL_SECONDS = 2.0
EXCHANGES = ("NSE", "BSE")
PRODUCTS = ("CNC", "MIS")
SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")


class Refusal(Exception):
    """An input or precondition problem; nothing was contacted."""


@dataclass(frozen=True)
class Config:
    exchange: str
    symbol: str
    quantity: int
    product: str
    limit_price: Decimal
    modify_price: Decimal
    reference_price: Decimal
    max_notional: Decimal
    risk_limit: Decimal
    static_ip: str
    instrument_master: str

    @property
    def cap(self) -> Decimal:
        return min(self.max_notional, self.risk_limit)

    @property
    def confirm_place(self) -> str:
        return f"PLACE BUY {self.quantity} {self.symbol} {self.exchange} {self.product} AT {self.limit_price} CAP {self.cap}"

    @property
    def confirm_modify(self) -> str:
        return f"MODIFY {self.symbol} TO {self.modify_price}"


# ── input validation (pure) ───────────────────────────────────────────────────────────────────

def _money(name: str, raw: Any) -> Decimal:
    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        raise Refusal(f"{name} is not a number")
    if not value.is_finite() or value <= 0:
        raise Refusal(f"{name} must be a positive number")
    return value


MANDATORY = ("exchange", "symbol", "quantity", "product", "limit_price", "modify_price", "reference_price", "max_notional",
             "risk_limit", "static_ip", "instrument_master")


def missing_inputs(args: argparse.Namespace) -> List[str]:
    return [name for name in MANDATORY if getattr(args, name, None) in (None, "")]


def build_config(args: argparse.Namespace) -> Config:
    """Validates every operator input. Raises Refusal on the first problem. There are no defaults to fall back on."""
    missing = missing_inputs(args)
    if missing:
        raise Refusal("missing required inputs: " + ", ".join("--" + m.replace("_", "-") for m in missing))
    exchange, symbol = str(args.exchange).strip().upper(), str(args.symbol).strip().upper()
    if exchange not in EXCHANGES:
        raise Refusal(f"exchange must be one of {', '.join(EXCHANGES)}")
    if not SYMBOL_RE.match(symbol):
        raise Refusal("symbol has characters or a length that a trading symbol cannot have")
    try:
        quantity = int(str(args.quantity).strip())
    except ValueError:
        raise Refusal("quantity must be the integer 1")
    if quantity != 1:
        raise Refusal("quantity must be exactly 1: this is a minimal-risk lifecycle check")
    product = str(args.product).strip().upper()
    if product not in PRODUCTS:
        raise Refusal(f"product must be one of {', '.join(PRODUCTS)}")
    limit_price, modify_price = _money("limit-price", args.limit_price), _money("modify-price", args.modify_price)
    reference = _money("reference-price", args.reference_price)
    max_notional, risk_limit = _money("max-notional", args.max_notional), _money("risk-limit", args.risk_limit)
    if limit_price >= reference or modify_price >= reference:
        raise Refusal("the limit price and the modify price must both be BELOW the reference price (a BUY priced at or above "
                      "the market would likely fill)")
    if limit_price == modify_price:
        raise Refusal("the modify price must differ from the limit price, or the modify proves nothing")
    cap = min(max_notional, risk_limit)
    for label, price in (("limit", limit_price), ("modify", modify_price)):
        if price * quantity > cap:
            raise Refusal(f"{label} price x quantity ({price * quantity}) exceeds the cap ({cap}): the smaller of max-notional and "
                          f"the operator-approved risk limit")
    try:
        ipaddress.ip_address(str(args.static_ip).strip())
    except ValueError:
        raise Refusal("static-ip is not a valid IP address")
    return Config(exchange, symbol, quantity, product, limit_price, modify_price, reference, max_notional, risk_limit,
                  str(args.static_ip).strip(), str(args.instrument_master))


def load_store(cfg: Config):
    """The instrument must resolve to exactly one cash-equity row that allows BUY, with a lot size of 1."""
    from app.services.groww.instruments import GrowwInstrumentStore

    if not os.path.isfile(cfg.instrument_master):
        raise Refusal("the instrument master file does not exist")
    store = GrowwInstrumentStore()
    try:
        loaded = store.load_file(cfg.instrument_master)
    except Exception as exc:
        raise Refusal(f"the instrument master could not be read ({type(exc).__name__})")
    if not loaded:
        raise Refusal("the instrument master contains no NSE/BSE cash-equity rows")
    try:
        inst = store.resolve(cfg.exchange, cfg.symbol)
    except Exception as exc:
        raise Refusal(f"the instrument does not resolve: {exc}")
    if inst.buy_allowed is not True:
        raise Refusal("Groww does not allow BUY for this instrument")
    if inst.lot_size > 1:
        raise Refusal("the instrument's lot size is above 1; a quantity-one order cannot be placed")
    return store


# ── live lifecycle ────────────────────────────────────────────────────────────────────────────

def _mask(order_id: str) -> str:
    s = str(order_id)
    return "***" if len(s) <= 8 else f"{s[:3]}...{s[-3:]}"


def classify(record: Any) -> str:
    """OPEN / PARTIAL / FILLED / CANCELLED / REJECTED / UNKNOWN from a parsed OrderRecord."""
    if record is None:
        return "UNKNOWN"
    status = str(getattr(record, "status", "")).upper()
    filled, qty = int(getattr(record, "filled_quantity", 0) or 0), int(getattr(record, "quantity", 0) or 0)
    if status == "COMPLETE" or (qty > 0 and filled >= qty):
        return "FILLED"
    if filled > 0:
        return "PARTIAL"
    if status == "CANCELLED":
        return "CANCELLED"
    if status == "REJECTED":
        return "REJECTED"
    if status in ("OPEN", "TRIGGER_PENDING"):
        return "OPEN"
    return "UNKNOWN"


PRECEDENCE = {0: 0, 6: 1, 5: 2, 4: 3}


class Run:
    """Collects the steps and the facts the later steps depend on."""

    def __init__(self, say: Callable[[str], None]):
        self.say = say
        self.steps: List[Dict[str, Any]] = []
        self.order_id: Optional[str] = None
        self.cancel_attempted = False
        self.cancel_confirmed = False
        self.final_state = "NOT_PLACED"
        self.exit_code = 0

    def rec(self, step: str, outcome: str, detail: str = "", **extra: Any) -> None:
        self.steps.append({"step": step, "outcome": outcome, "detail": detail, **extra})
        self.say(f"[{outcome}] {step}" + (f": {detail}" if detail else ""))

    def fail(self, code: int) -> None:
        """Keeps the most serious outcome: a fill (4) outranks an unconfirmed cancel (5), which outranks another failure (6)."""
        if PRECEDENCE[code] > PRECEDENCE[self.exit_code]:
            self.exit_code = code


async def _redacted_reads(client: Any) -> Dict[str, Any]:
    from app.services.angel_one.capture import redact_response
    from app.services.groww.contract import ROUTES

    out: Dict[str, Any] = {}
    for label, route, params in READ_ONLY_CALLS:
        try:
            status, data = await client._call("read", "GET", ROUTES[route], params=params)
            out[label] = redact_response(status, data)
        except Exception as exc:
            out[label] = {"unreadable": type(exc).__name__}
    return out


async def snapshot(client: Any, cfg: Config) -> Dict[str, Any]:
    """Read-only. The shapes are written to the report; the values (funds, net quantity) stay in memory for the reconciliation."""
    shapes = await _redacted_reads(client)
    net_qty: Optional[int] = None
    try:
        positions = await client.get_positions_or_none()
        if positions is not None:
            net_qty = sum(p.quantity for p in positions
                          if p.symbol.upper() in (cfg.symbol, cfg.symbol + "-EQ") and p.exchange.upper() == cfg.exchange
                          and str(p.product_type).upper() == cfg.product)
    except Exception:
        net_qty = None
    funds: Optional[Decimal] = None
    try:
        funds = (await client.get_funds()).available_margin
    except Exception:
        funds = None
    return {"shapes": shapes, "net_qty": net_qty, "available_margin": funds}


async def _reread(run: Run, client: Any, until: Callable[[Any, str], bool], sleep: Callable) -> Tuple[Any, str]:
    """Bounded read-only re-checks of the order until `until(record, state)` or the attempts run out. Never sends anything."""
    record, state = None, "UNKNOWN"
    for attempt in range(CONFIRM_ATTEMPTS):
        try:
            record = await client.get_order(run.order_id)
            state = classify(record)
        except Exception as exc:
            record, state = None, "UNKNOWN"
            run.say(f"    (order read failed: {type(exc).__name__})")
        if until(record, state):
            break
        if attempt < CONFIRM_ATTEMPTS - 1:
            await sleep(CONFIRM_INTERVAL_SECONDS)
    run.final_state = state
    return record, state


async def _cancel_once(run: Run, client: Any, cfg: Config, sleep: Callable) -> None:
    """Sends AT MOST ONE cancel, then only re-reads. A cancel is never repeated and never assumed to have worked."""
    if run.cancel_attempted or not run.order_id:
        return
    if run.final_state in ("FILLED", "PARTIAL"):       # a filled/part-filled order is managed by hand; never a blind cancel
        return
    run.cancel_attempted = True
    try:
        res = await client.cancel_order(run.order_id, cfg.symbol, cfg.exchange)
        run.rec("cancel request", res.category.value, "result is NOT trusted; the order is re-read")
    except Exception as exc:
        run.rec("cancel request", "ERROR", f"{type(exc).__name__}; the order is re-read")
    _, state = await _reread(run, client, lambda r, s: s in ("CANCELLED", "FILLED", "PARTIAL", "REJECTED"), sleep)
    if state == "CANCELLED":
        run.cancel_confirmed = True
        run.rec("cancel confirmed", "OK", "the order reads CANCELLED")
    elif state in ("FILLED", "PARTIAL"):
        run.rec("cancel confirmed", "FILLED" if state == "FILLED" else "PARTIAL",
                "the order filled before the cancel took effect; a position may exist - square it off by hand")
        run.fail(4)
    elif state == "REJECTED":
        run.cancel_confirmed = True
        run.rec("cancel confirmed", "OK", "the order reads REJECTED (nothing live)")
    else:
        run.rec("cancel confirmed", "NOT CONFIRMED", "state after the cancel: " + state + ". CANCEL BY HAND in the Groww app.")
        run.fail(5)


async def run_lifecycle(cfg: Config, client: Any, *, input_fn: Callable[[str], str], sleep: Callable = asyncio.sleep,
                        say: Callable[[str], None] = print) -> Tuple[int, Dict[str, Any]]:
    """The live sequence. `client` is a GrowwClient (or a fake). Returns (exit code, report)."""
    from app.services.alice_blue.types import OrderRequest, OrderResultCategory, OrderSide, OrderType, ProductType
    from app.services.angel_one.capture import redact_response
    from app.services.groww.client import GrowwAuthError, order_reference_for

    run = Run(say)
    report: Dict[str, Any] = {
        "note": "shapes and outcomes only; no key, secret, token, balance or full order id is written",
        "limitation": LIMITATION, "steps": run.steps,
        "inputs": {"exchange": cfg.exchange, "symbol": cfg.symbol, "quantity": cfg.quantity, "product": cfg.product,
                   "limit_price": str(cfg.limit_price), "modify_price": str(cfg.modify_price),
                   "reference_price": str(cfg.reference_price), "cap": str(cfg.cap)},
    }
    say("WARNING: " + LIMITATION)

    try:
        await client.login()
        run.rec("login", "OK")
    except GrowwAuthError as exc:
        run.rec("login", "FAILED", str(exc)[:120])
        return 2, {**report, "verdict": "login failed"}
    except Exception as exc:
        run.rec("login", "FAILED", type(exc).__name__)
        return 2, {**report, "verdict": "login failed"}

    before: Dict[str, Any] = {"shapes": {}, "net_qty": None, "available_margin": None}
    try:
        before = await snapshot(client, cfg)
        report["before"] = before["shapes"]
        run.rec("read-only capture (before)", "OK" if before["net_qty"] is not None and before["available_margin"] is not None else "PARTIAL",
                "account state read; values stay in memory only")

        if input_fn(f"Type exactly to place the order:\n  {cfg.confirm_place}\n> ").strip() != cfg.confirm_place:
            run.rec("confirmation", "ABORTED", "the placement confirmation did not match; nothing was sent")
            return 1, {**report, "verdict": "aborted before placing"}

        client_order_id = "JTLC" + uuid.uuid4().hex[:12]
        reference = order_reference_for(client_order_id)
        request = OrderRequest(symbol=cfg.symbol, exchange=cfg.exchange, side=OrderSide.BUY, quantity=cfg.quantity,
                               product_type=ProductType(cfg.product), order_type=OrderType.LIMIT, price=cfg.limit_price,
                               trigger_price=Decimal("0"), client_order_id=client_order_id)
        try:
            res = await client.place_order(request)        # exactly one send, ever
        except Exception as exc:                           # the adapter never raises; if it did, whether it sent is unknown
            run.rec("place order", "AMBIGUOUS", f"{type(exc).__name__} during the send. NOT resent, NOT cancelled. Check the Groww "
                    "app and resolve it by hand.")
            run.final_state = "AMBIGUOUS"
            return 3, {**report, "verdict": "placement ambiguous - manual inspection required"}
        report["place_response"] = redact_response(200, res.raw_response if isinstance(res.raw_response, dict) else {})

        if res.category == OrderResultCategory.REJECTED:
            run.rec("place order", "REJECTED", "Groww (or the adapter, before sending) refused it; nothing is live")
            run.fail(6)
        elif res.category != OrderResultCategory.SUCCESS or not res.broker_order_id:
            run.rec("place order", "AMBIGUOUS", f"category {res.category.value}. NOT resent, NOT cancelled. Check the Groww app "
                    "(orders and the order-by-reference lookup) and resolve it by hand.")
            run.final_state = "AMBIGUOUS"
            return 3, {**report, "verdict": "placement ambiguous - manual inspection required"}
        else:
            run.order_id = str(res.broker_order_id)
            run.rec("place order", "OK", f"order {_mask(run.order_id)} accepted by the adapter; its state is read next")

        if run.order_id:
            try:
                found = await client.find_by_client_reference(reference)
            except Exception as exc:
                found = None
                run.say(f"    (reference lookup failed: {type(exc).__name__})")
            ok = found is not None and found.broker_order_id == run.order_id and found.quantity == cfg.quantity
            run.rec("lookup by client reference", "OK" if ok else "MISMATCH",
                    "the reference resolves to the order just placed" if ok else "the reference did not resolve to this order")
            if not ok:
                run.fail(6)

            record, state = await _reread(run, client, lambda r, s: s != "UNKNOWN", sleep)
            report["order_state_after_place"] = state
            run.rec("order state after placing", state, "filled quantity " + str(getattr(record, "filled_quantity", "?")))

            if state == "FILLED":
                run.rec("lifecycle", "FILLED", "THE ORDER FILLED: a position of 1 exists. Square it off by hand in the Groww app.")
                run.fail(4)
            elif state == "PARTIAL":
                run.rec("lifecycle", "PARTIAL", "THE ORDER IS PART-FILLED: a position exists and the rest may still be working. "
                        "The sequence stops. In the Groww app, cancel the remainder and manage the position by hand.")
                run.fail(4)
            elif state in ("CANCELLED", "REJECTED"):
                run.rec("lifecycle", state, "the order is already not live; modify and cancel are skipped")
                run.cancel_confirmed = True
            else:
                if state == "OPEN":
                    if input_fn(f"Type exactly to modify the order:\n  {cfg.confirm_modify}\n> ").strip() == cfg.confirm_modify:
                        try:
                            mres = await client.modify_order(run.order_id, cfg.quantity, cfg.modify_price, Decimal("0"),
                                                             OrderType.LIMIT, cfg.symbol, cfg.exchange, ProductType(cfg.product))
                            run.rec("modify request", mres.category.value, "result is NOT trusted; the order is re-read")
                        except Exception as exc:
                            run.rec("modify request", "ERROR", f"{type(exc).__name__}; the order is re-read")
                        record, state = await _reread(
                            run, client, lambda r, s: s != "OPEN" or (r is not None and r.price == cfg.modify_price), sleep)
                        if state == "OPEN" and record is not None and record.price == cfg.modify_price:
                            run.rec("modify confirmed", "OK", "the order reads the new price")
                        elif state in ("FILLED", "PARTIAL"):
                            run.rec("modify confirmed", state, "the order filled while being modified; a position may exist")
                            run.fail(4)
                        else:
                            run.rec("modify confirmed", "NOT CONFIRMED", f"state {state}; the new price was not observed")
                            run.fail(6)
                    else:
                        run.rec("modify", "SKIPPED", "the modify confirmation did not match; the order is cancelled next")
                        run.fail(6)                                 # the modify was never observed
                else:
                    run.rec("modify", "SKIPPED", f"the order is {state}; it is not modified")
                    run.fail(6)
                await _cancel_once(run, client, cfg, sleep)
    except Exception as exc:
        run.rec("unexpected error", "ERROR", type(exc).__name__)
        run.fail(6)
        if run.order_id and not run.cancel_attempted:
            await _cancel_once(run, client, cfg, sleep)         # one guarded attempt, then only reads
    finally:
        if run.order_id and not run.cancel_confirmed and run.exit_code not in (4, 5):
            run.rec("cleanup", "NOT CONFIRMED", "the order is not confirmed cancelled. Check the Groww app.")
            run.fail(5)

    try:
        after = await snapshot(client, cfg)
        report["after"] = after["shapes"]
        net_ok = before["net_qty"] is not None and after["net_qty"] is not None and before["net_qty"] == after["net_qty"]
        funds_ok = (before["available_margin"] is not None and after["available_margin"] is not None
                    and before["available_margin"] == after["available_margin"])
        report["reconciliation"] = {"position_unchanged": net_ok, "funds_unchanged": funds_ok}
        if net_ok and funds_ok:
            run.rec("reconciliation", "OK", "position and available funds are as they were before")
        else:
            run.rec("reconciliation", "MISMATCH" if before["net_qty"] is not None else "NOT ESTABLISHED",
                    "position or funds differ, or could not be read. Compare with the Groww app by hand.")
            run.fail(6)
    except Exception as exc:
        run.rec("reconciliation", "ERROR", type(exc).__name__)
        run.fail(6)

    report["final_order_state"] = run.final_state
    report["verdict"] = {0: "every step observed as expected (this records evidence; it verifies nothing by itself)",
                         4: "FILLED - square off by hand", 5: "cancel NOT confirmed - cancel by hand",
                         6: "a step failed or did not reconcile"}.get(run.exit_code, "see steps")
    return run.exit_code, report


# ── command line ──────────────────────────────────────────────────────────────────────────────

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true", help="go live (default: dry run, nothing contacted)")
    ap.add_argument("--i-am-authorized", action="store_true", help="confirm this is your own Groww account")
    for name in ("exchange", "symbol", "quantity", "product", "limit-price", "modify-price", "reference-price", "max-notional",
                 "risk-limit", "static-ip", "instrument-master"):
        ap.add_argument("--" + name, default=None)          # no default for any risk input, ever
    ap.add_argument("--out", default="groww_capture_lifecycle.json")
    return ap


def plan_text(cfg: Config) -> str:
    return (f"Plan: log in once; read funds, positions and the order list; place ONE BUY LIMIT of {cfg.quantity} {cfg.symbol} "
            f"({cfg.exchange}, {cfg.product}) at {cfg.limit_price}; look it up by reference; modify it to {cfg.modify_price}; "
            f"cancel it once and confirm; read funds and positions again and reconcile. Cap {cfg.cap} "
            f"(smaller of max-notional {cfg.max_notional} and risk limit {cfg.risk_limit}).\n" + LIMITATION)


async def execute(cfg: Config, env: Dict[str, str], out_path: str, *, input_fn: Callable[[str], str], http_client: Any = None,
                  sleep: Callable = asyncio.sleep, say: Callable[[str], None] = print) -> int:
    """`http_client` and `sleep` exist so tests can inject a fake transport; main() never passes them."""
    from app.services.groww.client import GrowwClient

    store = load_store(cfg)
    client = GrowwClient(
        account_id="lifecycle", label="lifecycle", api_key=env["GROWW_API_KEY"], api_secret=env["GROWW_API_SECRET"],
        redis=MemoryRedis(), static_ip=cfg.static_ip, static_ip_valid_until=None, http_client=http_client,
        proxy_url=env.get("GROWW_PROXY_URL") or None, instruments=store,
    )
    try:
        code, report = await run_lifecycle(cfg, client, input_fn=input_fn, sleep=sleep, say=say)
    finally:
        try:
            await client.logout()
        finally:
            await client.aclose()
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    say(f"Wrote {out_path} (git-ignored). exit code {code}: {report.get('verdict')}")
    return code


def main(argv: Optional[List[str]] = None, env: Optional[Dict[str, str]] = None, stdin_is_tty: Optional[bool] = None,
         input_fn: Callable[[str], str] = input) -> int:
    args = _parser().parse_args(argv)
    env = dict(os.environ) if env is None else env
    missing = missing_inputs(args)
    if not args.execute:
        if missing:
            print("Dry run - nothing contacted. Inputs still required before a live run: "
                  + ", ".join("--" + m.replace("_", "-") for m in missing))
            return 0
        try:
            cfg = build_config(args)
        except Refusal as exc:
            print(f"Refusing: {exc}")
            return 1
        print(plan_text(cfg))
        print("Dry run - nothing contacted.")
        return 0
    if not args.i_am_authorized:
        print("Refusing: pass --i-am-authorized to confirm this is your own account.")
        return 1
    try:
        cfg = build_config(args)
    except Refusal as exc:
        print(f"Refusing: {exc}")
        return 1
    if not ipaddress.ip_address(cfg.static_ip).is_global:
        print("Refusing: --static-ip is not a public address. Documentation examples such as 203.0.113.x are placeholders, "
              "not an IP to use; give the static IP actually registered with Groww.")
        return 1
    absent = [k for k in REQUIRED_ENV if not env.get(k)]
    if absent:
        print(f"Refusing: missing environment variables: {', '.join(absent)}")
        return 1
    if not (sys.stdin.isatty() if stdin_is_tty is None else stdin_is_tty):
        print("Refusing: live mode needs an interactive terminal for the confirmations.")
        return 1
    print(plan_text(cfg))
    try:
        return asyncio.run(execute(cfg, env, args.out, input_fn=input_fn))
    except Refusal as exc:
        print(f"Refusing: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
