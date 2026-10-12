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
    ) -> int:
        target_qty = settings.fixed_qty if settings.fixed_qty is not None else 1
        return floor_to_lot(float(target_qty), lot_size)


class CopyRatioStrategy:
    def calculate(
        self,
        master_qty: int,
        master_capital: Decimal,
        follower_capital: Decimal,
        lot_size: int,
        settings: CopySetting,
        reference_price: Decimal = Decimal("0"),
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
    ) -> int:
        strategy = cls.STRATEGIES.get(mode, FixedQuantityStrategy())
        return strategy.calculate(
            master_qty=master_qty,
            master_capital=master_capital,
            follower_capital=follower_capital,
            lot_size=lot_size,
            settings=settings,
            reference_price=reference_price,
        )
