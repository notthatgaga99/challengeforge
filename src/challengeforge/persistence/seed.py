from sqlalchemy.ext.asyncio import AsyncSession

from challengeforge.identity import SEEDED_USERS
from challengeforge.persistence.mapping import new_user_row
from challengeforge.persistence.models import UserRow


async def seed_dev_users(session: AsyncSession) -> None:
    for user_id, display_name, role in SEEDED_USERS:
        existing = await session.get(UserRow, user_id)
        if existing is None:
            session.add(new_user_row(user_id, display_name, role))
    await session.commit()
