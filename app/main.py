"""
FastAPI application entry point (security-hardened).

Startup sequence:
  1. Create database tables (idempotent)
  2. Ingest knowledge base documents into ChromaDB
  3. Mount rate-limit middleware
  4. Mount routers (webhook + auth)
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.middleware.rate_limiter import RateLimitMiddleware
from app.models.database import init_db
from app.routes.auth import router as auth_router
from app.routes.webhook import router as webhook_router
from app.services.rag_service import get_rag_service
from app.utils.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting HR Chatbot", env=settings.app_env, model=settings.claude_model)

    await init_db()
    logger.info("Database initialised")

    rag_svc = get_rag_service()
    total_chunks = rag_svc.ingest_all()
    logger.info("Knowledge base ready", total_chunks=total_chunks)

    yield

    logger.info("Shutting down HR Chatbot")


app = FastAPI(
    title="HR Chatbot API (SPBT)",
    description="Secure LINE Chatbot powered by Claude + RAG for HR queries",
    version="2.0.0",
    lifespan=lifespan,
    # Hide docs in production
    docs_url="/docs" if settings.app_env == "development" else None,
    redoc_url="/redoc" if settings.app_env == "development" else None,
)

# ── Middleware (order matters: outermost = first to run) ───────────────────────

# Rate limiter (before CORS so blocked requests never reach app logic)
app.add_middleware(RateLimitMiddleware)

# CORS — restrict origins in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.app_env == "development" else [],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Routers ────────────────────────────────────────────────────────────────────

app.include_router(webhook_router, tags=["LINE Webhook"])
app.include_router(auth_router, tags=["Account Linkage"])


@app.get("/")
async def root():
    return {
        "service": "HR Chatbot API",
        "version": "2.0.0",
        "bot_name": settings.bot_name,
        "security": "IAM + PII masking + rate limiting + audit logging",
    }
