"""
Database models and async engine setup.

Tables:
  - employees             : registered employee roster (seeded by HR)
  - employee_verifications: short-lived OTP tokens for account linkage
  - chat_sessions         : one row per LINE user, stores context metadata
  - chat_messages         : full conversation log per session (for analytics)
  - knowledge_docs        : metadata for documents loaded into ChromaDB
  - access_logs           : immutable audit trail for every API interaction
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
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


# ── IAM: Employee Registry ─────────────────────────────────────────────────────

class Employee(Base):
    """
    Master employee table seeded/managed by HR.

    role_level controls which knowledge-base categories the bot will answer:
      1 = standard employee  (HR policy, benefits, general KPI)
      2 = supervisor         (+ team KPI data, headcount)
      3 = HR staff           (+ salary band details, disciplinary records)
    """

    __tablename__ = "employees"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    employee_id = Column(String(20), unique=True, nullable=False, index=True)
    full_name = Column(String(200), nullable=False)
    department = Column(String(100), nullable=True)
    role_level = Column(Integer, default=1)          # 1 | 2 | 3
    is_active = Column(Boolean, default=True)
    # Linked LINE account (None until Account Linkage completes)
    line_user_id = Column(String(100), unique=True, nullable=True, index=True)
    linked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, server_default=func.now())

    verifications = relationship(
        "EmployeeVerification", back_populates="employee", cascade="all, delete-orphan"
    )
    session = relationship("ChatSession", back_populates="employee", uselist=False)


class EmployeeVerification(Base):
    """
    Short-lived OTP record for Account Linkage.

    Flow: HR generates an OTP → employee receives it via internal email →
    employee enters OTP in the LINE Mini Web / LIFF page →
    system links employee.line_user_id ↔ Employee.id
    """

    __tablename__ = "employee_verifications"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    employee_id = Column(String(20), ForeignKey("employees.employee_id"), nullable=False)
    # Store bcrypt hash of the 6-digit OTP – never the raw value
    otp_hash = Column(String(128), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    is_used = Column(Boolean, default=False)
    attempt_count = Column(Integer, default=0)       # Lock after 5 failed attempts
    created_at = Column(DateTime, default=datetime.utcnow, server_default=func.now())

    employee = relationship("Employee", back_populates="verifications")


# ── Chat Data ──────────────────────────────────────────────────────────────────

class ChatSession(Base):
    """Tracks one LINE user's conversation state (after successful linkage)."""

    __tablename__ = "chat_sessions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    line_user_id = Column(String(100), unique=True, nullable=False, index=True)
    # FK to employees (set when linkage completes)
    employee_db_id = Column(String(36), ForeignKey("employees.id"), nullable=True)
    display_name = Column(String(200), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, server_default=func.now())
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    message_count = Column(Integer, default=0)

    messages = relationship(
        "ChatMessage", back_populates="session", cascade="all, delete-orphan"
    )
    employee = relationship("Employee", back_populates="session")


class ChatMessage(Base):
    """
    Stores every message exchanged with a user (analytics + context window).

    IMPORTANT: content here stores the *masked* version of user messages
    (PII already removed).  Raw originals are never persisted.
    """

    __tablename__ = "chat_messages"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("chat_sessions.id"), nullable=False)
    role = Column(String(20), nullable=False)         # "user" | "assistant"
    content = Column(Text, nullable=False)            # Always PII-masked
    timestamp = Column(DateTime, default=datetime.utcnow, server_default=func.now())
    # RAG metadata
    rag_sources = Column(Text, nullable=True)         # JSON list of doc names
    rag_score = Column(Float, nullable=True)          # Top similarity score
    tokens_used = Column(Integer, nullable=True)
    # PII audit
    pii_detected = Column(Boolean, default=False)     # True if PII was masked
    pii_types = Column(String(200), nullable=True)    # Comma-separated label names

    session = relationship("ChatSession", back_populates="messages")


# ── Security Audit Log ─────────────────────────────────────────────────────────

class AccessLog(Base):
    """
    Immutable audit trail.  Never deleted — even by data-retention jobs.
    Records key security events: logins, link attempts, rate-limit hits, etc.
    """

    __tablename__ = "access_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    event_type = Column(String(50), nullable=False)   # "otp_request" | "otp_success"
                                                      # "otp_fail" | "rate_limited"
                                                      # "auth_denied" | "chat"
    line_user_id = Column(String(100), nullable=True, index=True)
    employee_id = Column(String(20), nullable=True)
    ip_address = Column(String(45), nullable=True)    # IPv4 or IPv6
    detail = Column(String(500), nullable=True)       # e.g., failure reason
    timestamp = Column(DateTime, default=datetime.utcnow, server_default=func.now())


# ── Knowledge-base Tracker ─────────────────────────────────────────────────────

class KnowledgeDoc(Base):
    """Tracks documents that have been ingested into ChromaDB."""

    __tablename__ = "knowledge_docs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    filename = Column(String(500), nullable=False)
    doc_type = Column(String(50), nullable=True)      # "hr_policy" | "kpi" | etc.
    chunk_count = Column(Integer, default=0)
    ingested_at = Column(DateTime, default=datetime.utcnow, server_default=func.now())
    checksum = Column(String(64), nullable=True)      # SHA-256 to detect changes


# ── DB lifecycle ───────────────────────────────────────────────────────────────

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
