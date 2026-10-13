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
    ) -> int:
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
