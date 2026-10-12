import asyncio
import logging
from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.models.accounts import Account, AccountType, CopySetting, QuantityMode

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fix_follower")

async def fix():
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Account).where(Account.deleted_at.is_(None)))
        accounts = res.scalars().all()
        
        logger.info(f"Found {len(accounts)} accounts in DB:")
        for acc in accounts:
            logger.info(f" - Account {acc.display_name} ({acc.alice_user_id}): type={acc.account_type.value}, is_active={acc.is_active}, session_valid={acc.session_valid}")
            
        masters = [acc for acc in accounts if acc.account_type == AccountType.MASTER]
        if len(masters) > 1:
            logger.info(f"Multiple MASTER accounts detected ({len(masters)}). Fixing follower account types...")
            for acc in masters:
                if acc.alice_user_id == '2679228' or acc.display_name.lower() == 'vikash':
                    acc.account_type = AccountType.FOLLOWER
                    logger.info(f"Updated {acc.display_name} ({acc.alice_user_id}) account_type to FOLLOWER.")
                    
                    set_res = await session.execute(select(CopySetting).where(CopySetting.follower_account_id == acc.id))
                    setting = set_res.scalar_one_or_none()
                    if not setting:
                        setting = CopySetting(
                            follower_account_id=acc.id,
                            copy_enabled=True,
                            quantity_mode=QuantityMode.RATIO,
                            ratio=1.0,
                        )
                        session.add(setting)
                        logger.info(f"Created CopySetting for {acc.display_name} with copy_enabled=True.")
                    else:
                        setting.copy_enabled = True
                        logger.info(f"Updated CopySetting for {acc.display_name} to copy_enabled=True.")
                        
            await session.commit()
            logger.info("Successfully updated account types in DB!")
        else:
            followers = [acc for acc in accounts if acc.account_type == AccountType.FOLLOWER]
            for f in followers:
                set_res = await session.execute(select(CopySetting).where(CopySetting.follower_account_id == f.id))
                setting = set_res.scalar_one_or_none()
                if not setting:
                    setting = CopySetting(
                        follower_account_id=f.id,
                        copy_enabled=True,
                        quantity_mode=QuantityMode.RATIO,
                        ratio=1.0,
                    )
                    session.add(setting)
                    logger.info(f"Created CopySetting for {f.display_name} with copy_enabled=True.")
                else:
                    setting.copy_enabled = True
                    logger.info(f"Updated CopySetting for {f.display_name} to copy_enabled=True.")
            await session.commit()
            logger.info("Checked and updated all follower CopySettings.")

if __name__ == "__main__":
    asyncio.run(fix())
