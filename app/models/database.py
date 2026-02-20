"""
Database models and async engine setup.

Tables:
  - chat_sessions   : one row per LINE user, stores context metadata
  - chat_messages   : full conversation log per session (for analytics)
  - knowledge_docs  : metadata for documents loaded into ChromaDB
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, relationship

from app.config import get_settings

settings = get_settings()

engine = create_async_engine(
    settings.database_url,
    echo=(settings.app_env == "development"),
)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


class ChatSession(Base):
    """Tracks one LINE user's session state."""

    __tablename__ = "chat_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    line_user_id = Column(String(100), unique=True, nullable=False, index=True)
    display_name = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    message_count = Column(Integer, default=0)

    messages = relationship(
        "ChatMessage", back_populates="session", cascade="all, delete-orphan"
    )


class ChatMessage(Base):
    """Stores every message exchanged with a user (analytics + context)."""

    __tablename__ = "chat_messages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("chat_sessions.id"), nullable=False)
    role = Column(String(20), nullable=False)  # "user" | "assistant"
    content = Column(Text, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, server_default=func.now())
    # RAG metadata
    rag_sources = Column(Text, nullable=True)          # JSON list of retrieved doc IDs
    rag_score = Column(Float, nullable=True)           # Top similarity score
    tokens_used = Column(Integer, nullable=True)

    session = relationship("ChatSession", back_populates="messages")


class KnowledgeDoc(Base):
    """Tracks documents that have been ingested into ChromaDB."""

    __tablename__ = "knowledge_docs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    filename = Column(String(500), nullable=False)
    doc_type = Column(String(50), nullable=True)       # "hr_policy" | "kpi" | etc.
    chunk_count = Column(Integer, default=0)
    ingested_at = Column(DateTime, default=datetime.utcnow, server_default=func.now())
    checksum = Column(String(64), nullable=True)       # SHA-256 to detect changes


async def init_db() -> None:
    """Create all tables (idempotent)."""
    import os

    os.makedirs("./data", exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_db():
    """FastAPI dependency: yields an async DB session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
