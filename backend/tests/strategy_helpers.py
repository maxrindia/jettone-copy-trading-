"""Explicit strategy setup for tests.

The copy engine no longer falls back to "every active FOLLOWER at 1.0x" when
no strategy has the order's account as Master - it copies to nobody. Tests
that used to rely on that fallback link their master and followers here
instead. With multiplier 1.0 (the fallback's own value) and each follower's
existing CopySetting untouched, the copied quantities are identical, so the
tests' assertions stay exactly as they were.
"""
import uuid
from decimal import Decimal
from typing import Iterable, Optional, Tuple, Union

from app.models.accounts import Account
from app.models.strategies import (
    STRATEGY_ROLE_CHILD,
    STRATEGY_ROLE_MASTER,
    GroupStrategy,
    StrategyAccountRole,
    StrategyMember,
)

ChildSpec = Union[Account, Tuple[Account, Union[Decimal, float, int]]]

# conftest's sample_accounts master always has this id, so test-built
# MasterTradeEvents can name their originating account the way real
# detector events do (routing now requires the event to come from the
# strategy's Master).
SAMPLE_MASTER_ID = uuid.UUID("00000000-0000-4000-8000-00000000a001")
SAMPLE_MASTER_ACCOUNT_ID = str(SAMPLE_MASTER_ID)


async def link_strategy(
    session,
    master: Account,
    children: Iterable[ChildSpec],
    *,
    name: str = "Test Strategy",
    trading_enabled: bool = True,
    member_trading_enabled: bool = True,
    commit: bool = True,
) -> GroupStrategy:
    strategy = GroupStrategy(
        id=uuid.uuid4(),
        name=name,
        master_account_id=master.id,
        trading_enabled=trading_enabled,
    )
    session.add(strategy)
    await session.flush()
    session.add(StrategyAccountRole(strategy_id=strategy.id, account_id=master.id, role=STRATEGY_ROLE_MASTER))
    for spec in children:
        account, multiplier = spec if isinstance(spec, tuple) else (spec, Decimal("1.0"))
        session.add(
            StrategyMember(
                strategy_id=strategy.id,
                account_id=account.id,
                multiplier=Decimal(str(multiplier)),
                trading_enabled=member_trading_enabled,
            )
        )
        session.add(StrategyAccountRole(strategy_id=strategy.id, account_id=account.id, role=STRATEGY_ROLE_CHILD))
    if commit:
        await session.commit()
    else:
        await session.flush()
    return strategy


async def add_child(
    session,
    strategy: GroupStrategy,
    account: Account,
    multiplier: Union[Decimal, float, int] = Decimal("1.0"),
    *,
    trading_enabled: bool = True,
) -> None:
    """Adds one more Child (e.g. a test's extra follower) to a strategy."""
    session.add(
        StrategyMember(
            strategy_id=strategy.id,
            account_id=account.id,
            multiplier=Decimal(str(multiplier)),
            trading_enabled=trading_enabled,
        )
    )
    session.add(StrategyAccountRole(strategy_id=strategy.id, account_id=account.id, role=STRATEGY_ROLE_CHILD))
    await session.commit()


async def link_sample_accounts(session, sample_accounts: dict, **kwargs) -> Optional[GroupStrategy]:
    """Links conftest's sample_accounts master -> follower1 at 1.0x."""
    return await link_strategy(session, sample_accounts["master"], [sample_accounts["follower1"]], **kwargs)
