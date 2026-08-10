from sqlalchemy.orm import Session

from models import MemoryFact, User


def get_facts(session: Session, user: User) -> list[str]:
    """Return all long-term facts learned about a user, newest first."""
    rows = (
        session.query(MemoryFact)
        .filter(MemoryFact.user_id == user.id)
        .order_by(MemoryFact.created_at.desc())
        .all()
    )
    return [row.fact for row in rows]


def remember(session: Session, user: User, fact: str) -> None:
    """Store one long-term fact about the user (e.g. 'Watches Nifty banks heavily')."""
    session.add(MemoryFact(user_id=user.id, fact=fact))
    session.commit()