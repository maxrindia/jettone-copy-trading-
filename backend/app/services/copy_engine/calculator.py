import math
from decimal import Decimal
from typing import Optional, Protocol
from app.models.accounts import CopySetting, QuantityMode


def floor_to_lot(qty: float, lot_size: int = 1) -> int:
    """
    Rounds DOWN strictly to the nearest valid integer multiple of lot_size.
    Never rounds up to prevent over-allocation or fractional lot rejections.
    """
    if lot_size <= 0:
        lot_size = 1
    return int(math.floor(qty / lot_size)) * lot_size


class QuantityStrategy(Protocol):
    def calculate(
        self,
        master_qty: int,
        master_capital: Decimal,
        follower_capital: Decimal,
        lot_size: int,
        settings: CopySetting,
        reference_price: Decimal = Decimal("0"),
        multiplier: Optional[Decimal] = None,
    ) -> int:
        ...


class FixedQuantityStrategy:
    def calculate(
        self,
        master_qty: int,
        master_capital: Decimal,
        follower_capital: Decimal,
        lot_size: int,
        settings: CopySetting,
        reference_price: Decimal = Decimal("0"),
        multiplier: Optional[Decimal] = None,
    ) -> int:
        target_qty = settings.fixed_qty if settings.fixed_qty is not None else 1
        return floor_to_lot(float(target_qty), lot_size)


class MultiplierStrategy:
    """
    Child quantity = Master quantity x the Child's strategy multiplier
    (1x of 65 -> 65, 2x of 65 -> 130), floored - never rounded up.

    Uses exact Decimal arithmetic so the product cannot lose a unit to float
    error. Fails closed: with no usable multiplier the result is 0, which
    every caller treats as "skip this Child" (ZERO_LOT_RESULT).

    The engine has no per-instrument lot-size data (lot_size is 1 unless a
    lot map is configured), so a fractional multiplier on an F&O order can
    produce a quantity that is not a lot multiple; the broker then rejects
    it. A whole-number multiplier always yields a lot multiple because the
    Master's own quantity is one.
    """

    def calculate(
        self,
        master_qty: int,
        master_capital: Decimal,
        follower_capital: Decimal,
        lot_size: int,
        settings: CopySetting,
        reference_price: Decimal = Decimal("0"),
        multiplier: Optional[Decimal] = None,
    ) -> int:
        if multiplier is None:
            return 0
        try:
            factor = Decimal(str(multiplier))
        except (ArithmeticError, ValueError):
            return 0
        if not factor.is_finite() or factor <= 0 or master_qty <= 0:
            return 0
        raw_qty = int(Decimal(master_qty) * factor)  # truncation = floor for positives
        if lot_size <= 1:
            return raw_qty
        return (raw_qty // lot_size) * lot_size


class CopyRatioStrategy:
    def calculate(
        self,
        master_qty: int,
        master_capital: Decimal,
        follower_capital: Decimal,
        lot_size: int,
        settings: CopySetting,
        reference_price: Decimal = Decimal("0"),
        multiplier: Optional[Decimal] = None,
    ) -> int:
        ratio = float(settings.ratio) if settings.ratio is not None else 1.0
        raw_qty = master_qty * ratio
        return floor_to_lot(raw_qty, lot_size)


class CapitalProportionalStrategy:
    def calculate(
        self,
        master_qty: int,
        master_capital: Decimal,
        follower_capital: Decimal,
        lot_size: int,
        settings: CopySetting,
        reference_price: Decimal = Decimal("0"),
        multiplier: Optional[Decimal] = None,
    ) -> int:
        if master_capital <= Decimal("0"):
            return 0
        ratio = float(follower_capital) / float(master_capital)
        raw_qty = master_qty * ratio
        return floor_to_lot(raw_qty, lot_size)


class RiskBasedStrategy:
    def calculate(
        self,
        master_qty: int,
        master_capital: Decimal,
        follower_capital: Decimal,
        lot_size: int,
        settings: CopySetting,
        reference_price: Decimal = Decimal("0"),
        multiplier: Optional[Decimal] = None,
    ) -> int:
        # Base is capital proportional
        base_qty = CapitalProportionalStrategy().calculate(
            master_qty, master_capital, follower_capital, lot_size, settings, reference_price
        )
        
        # Hard-cap by max_trade_value if set and price is available
        if settings.max_trade_value and reference_price > Decimal("0"):
            max_qty_by_val = floor_to_lot(
                float(settings.max_trade_value) / float(reference_price),
                lot_size,
            )
            base_qty = min(base_qty, max_qty_by_val)

        return base_qty


class QuantityCalculator:
    """
    Dispatcher for pluggable quantity strategies based on follower configuration.
    """

    STRATEGIES: dict[QuantityMode, QuantityStrategy] = {
        QuantityMode.FIXED: FixedQuantityStrategy(),
        QuantityMode.MULTIPLIER: MultiplierStrategy(),
        QuantityMode.RATIO: CopyRatioStrategy(),
        QuantityMode.CAPITAL_PROPORTIONAL: CapitalProportionalStrategy(),
        QuantityMode.RISK_BASED: RiskBasedStrategy(),
    }

    @classmethod
    def calculate_quantity(
        cls,
        mode: QuantityMode,
        master_qty: int,
        master_capital: Decimal,
        follower_capital: Decimal,
        lot_size: int,
        settings: CopySetting,
        reference_price: Decimal = Decimal("0"),
        multiplier: Optional[Decimal] = None,
        derivative_lot: Optional[int] = None,
    ) -> int:
        """`derivative_lot` is the Child's lot size for a DERIVATIVE order whose lot is known (None for cash equity and whenever it is not
        known: then nothing here changes). FIXED and MULTIPLIER mean the same thing for F&O as for cash and are NEVER rounded to a lot:
          FIXED       `fixed_qty` is a quantity in units ("same quantity for every order"), exactly as the UI says;
          MULTIPLIER  Master quantity x multiplier, exactly as for cash (see `fractional_lot_problem` for the F&O lot check);
        whether the result is a valid whole number of the Child's lots is VALIDATED (cross-broker: fno_validation.LotMultiple, and
        `fractional_lot_problem` for a fractional product) and refused with a reason - the quantity is never adjusted. The other modes
        (not offered in the Copy Quantity UI) keep using the lot as their rounding unit."""
        if derivative_lot and derivative_lot > 1 and mode not in (QuantityMode.FIXED, QuantityMode.MULTIPLIER):
            lot_size = derivative_lot
        strategy = cls.STRATEGIES.get(mode, FixedQuantityStrategy())
        return strategy.calculate(
            master_qty=master_qty,
            master_capital=master_capital,
            follower_capital=follower_capital,
            lot_size=lot_size,
            settings=settings,
            reference_price=reference_price,
            multiplier=multiplier,
        )

    @classmethod
    def fractional_lot_problem(
        cls, mode: QuantityMode, master_qty: int, multiplier: Optional[Decimal], derivative_lot: Optional[int],
    ) -> Optional[str]:
        """MULTIPLIER only, F&O with a known Child lot: Master quantity x multiplier must be EXACTLY a whole number of the Child's lots
        (Master 3 lots x 0.5 = 1.5 lots is not). Returns the refusal code, or None. The caller refuses the Child with it; nothing is floored."""
        if mode != QuantityMode.MULTIPLIER or not derivative_lot or derivative_lot <= 1 or multiplier is None:
            return None
        try:
            exact = Decimal(master_qty) * Decimal(str(multiplier))
        except (ArithmeticError, ValueError):
            return None
        if not exact.is_finite() or exact <= 0:
            return None                                   # zero / unusable multipliers keep their existing ZERO_LOT_RESULT handling
        return "FNO_QUANTITY_NOT_LOT_MULTIPLE" if exact % derivative_lot != 0 else None
