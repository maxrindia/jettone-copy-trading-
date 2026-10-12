from dataclasses import dataclass
from decimal import Decimal
from typing import List, Optional
from app.models.accounts import Account, CopySetting
from app.services.alice_blue.types import FundsData


@dataclass
class RiskCheckResult:
    passed: bool
    failures: List[str]  # List of rejection reason codes


class RiskEngine:
    """
    Evaluates 8 independent risk constraints for a follower before order submission.
    Runs all checks to accumulate complete audit reasons (no short-circuiting).
    """

    @staticmethod
    def evaluate(
        account: Account,
        settings: CopySetting,
        symbol: str,
        quantity: int,
        price: Decimal,
        funds: FundsData,
        daily_loss_today: Decimal = Decimal("0"),
        open_exposure: Decimal = Decimal("0"),
        follower_runtime_paused: bool = False,
        global_emergency_stop: bool = False,
    ) -> RiskCheckResult:
        failures: List[str] = []

        # Check 1: Global emergency stop
        if global_emergency_stop:
            failures.append("EMERGENCY_STOP_ACTIVE")

        # Check 2: Account-specific emergency stop (DB display/audit flag)
        if account.is_emergency_stopped:
            failures.append("FOLLOWER_EMERGENCY_STOP")

        # Check 2b: Runtime pause from the Redis emergency-stop authority
        if follower_runtime_paused:
            failures.append("FOLLOWER_EMERGENCY_STOP")

        # Check 3: Follower copy-trading toggle
        if not settings.copy_enabled:
            failures.append("FOLLOWER_PAUSED")

        # Check 4: Allowed symbols for this follower
        if settings.allowed_symbols is not None and len(settings.allowed_symbols) > 0:
            if symbol not in settings.allowed_symbols:
                failures.append("SYMBOL_NOT_ALLOWED_FOR_FOLLOWER")

        # Estimated order value
        order_value = Decimal(str(quantity)) * price

        # Check 5: Sufficient margin (funds check)
        # Margin estimated roughly as order_value for MIS or required span
        if funds.available_margin < order_value and order_value > Decimal("0"):
            # Margin breach
            failures.append("INSUFFICIENT_MARGIN")

        # Check 6: Max trade value limit
        if settings.max_trade_value and order_value > settings.max_trade_value:
            failures.append("MAX_TRADE_VALUE_BREACHED")

        # Check 7: Max daily loss limit
        if settings.max_daily_loss and daily_loss_today >= settings.max_daily_loss:
            failures.append("MAX_DAILY_LOSS_BREACHED")

        # Check 8: Max exposure limit
        if settings.max_exposure and (open_exposure + order_value) > settings.max_exposure:
            failures.append("MAX_EXPOSURE_BREACHED")

        return RiskCheckResult(
            passed=len(failures) == 0,
            failures=failures,
        )
