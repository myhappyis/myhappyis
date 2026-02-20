"""
FastAPI application entry point.

Startup sequence:
  1. Create database tables
  2. Ingest knowledge base documents into ChromaDB (if any new files)
  3. Mount routers
"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.models.database import init_db
from app.routes.webhook import router as webhook_router
from app.services.rag_service import get_rag_service
from app.utils.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run startup / shutdown tasks."""
    logger.info("Starting HR Chatbot", env=settings.app_env, model=settings.claude_model)

    # Init DB
    await init_db()
    logger.info("Database initialised")

    # Ingest knowledge base documents
    rag_svc = get_rag_service()
    total_chunks = rag_svc.ingest_all()
    logger.info("Knowledge base ready", total_chunks=total_chunks)

    yield

    logger.info("Shutting down HR Chatbot")


app = FastAPI(
    title="HR Chatbot API (SPBT)",
    description="LINE Chatbot powered by Claude + RAG for HR queries",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS – restrict in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.app_env == "development" else [],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# Routers
app.include_router(webhook_router, tags=["LINE Webhook"])


@app.get("/")
async def root():
    return {
        "service": "HR Chatbot API",
        "version": "1.0.0",
        "bot_name": settings.bot_name,
        "docs": "/docs",
    }
