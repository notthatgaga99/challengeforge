"""Development identity.

Replace this module with a real authentication adapter that still yields
`CurrentUser`. Route handlers should depend on `CurrentUser`, never on
how that identity was established.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from challengeforge.domain.enums import UserRole
from challengeforge.domain.models import User

# Stable seeded identities for local development and tests.
ORGANIZER_ID = UUID("11111111-1111-4111-8111-111111111111")
PARTICIPANT_ID = UUID("22222222-2222-4222-8222-222222222222")
PARTICIPANT_2_ID = UUID("33333333-3333-4333-8333-333333333333")

SEEDED_USERS: tuple[tuple[UUID, str, UserRole], ...] = (
    (ORGANIZER_ID, "Ada Organizer", UserRole.ORGANIZER),
    (PARTICIPANT_ID, "Pat Participant", UserRole.PARTICIPANT),
    (PARTICIPANT_2_ID, "Riley Participant", UserRole.PARTICIPANT),
)


@dataclass(frozen=True)
class CurrentUser:
    id: UUID
    display_name: str
    role: UserRole

    @classmethod
    def from_user(cls, user: User) -> CurrentUser:
        return cls(id=user.id, display_name=user.display_name, role=user.role)

    @property
    def is_organizer(self) -> bool:
        return self.role == UserRole.ORGANIZER

    @property
    def is_participant(self) -> bool:
        return self.role == UserRole.PARTICIPANT
