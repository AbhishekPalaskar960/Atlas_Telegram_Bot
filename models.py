from sqlalchemy import Column, Integer, String, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
from database import Base


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(String, unique=True, index=True, nullable=False)
    name = Column(String, nullable=True)

    onboarded = Column(Boolean, default=False)
    role = Column(String, nullable=True)               # e.g. "Analyst", "Investor"
    sectors = Column(Text, nullable=True)               # comma-separated, kept simple for MVP
    watchlist = Column(Text, nullable=True)              # comma-separated tickers
    briefing_time = Column(String, nullable=True)        # e.g. "08:00"
    briefing_timezone = Column(String, default="Asia/Kolkata")
    last_briefing_date = Column(String(10), nullable=True)  # YYYY-MM-DD (de-dupe)

    # Google (Gmail + Calendar) OAuth — null until the user connects their account
    google_access_token = Column(Text, nullable=True)
    google_refresh_token = Column(Text, nullable=True)
    google_token_expiry = Column(String, nullable=True)     # ISO-8601 UTC datetime
    oauth_state = Column(String, nullable=True)              # pending CSRF state while connecting

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    messages = relationship("Message", back_populates="user", cascade="all, delete-orphan")
    memories = relationship("MemoryFact", back_populates="user", cascade="all, delete-orphan")
    alerts = relationship("Alert", back_populates="user", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="user", cascade="all, delete-orphan")
    sheets = relationship("Sheet", back_populates="user", cascade="all, delete-orphan")


class Message(Base):
    """Rolling conversation history, used to build LLM context."""
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    role = Column(String)          # "user" or "assistant"
    content = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="messages")


class MemoryFact(Base):
    """Long-term learned facts about the user, distinct from raw chat history.
    e.g. 'Prefers short answers', 'Interested in biotech since June'."""
    __tablename__ = "memory_facts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    fact = Column(Text)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="memories")


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    ticker = Column(String)
    condition = Column(String)      # e.g. "price_move_pct_5", "news", "filing"
    last_seen_id = Column(String, nullable=True)  # dedupe marker for news/filing watches
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="alerts")


class Document(Base):
    """Uploaded financial documents (PDF etc.). Extracted text is kept so the
    LLM can answer follow-up questions about the document at any time."""
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    filename = Column(String)
    content = Column(Text)          # extracted text, truncated to a safe size
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="documents")


class Sheet(Base):
    """Uploaded or linked financial spreadsheets (CSV/XLSX/Google Sheets).
    Rows are flattened to text so the LLM can reason over the data."""
    __tablename__ = "sheets"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    name = Column(String)
    content = Column(Text)          # flattened tabular data, truncated to a safe size
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    user = relationship("User", back_populates="sheets")