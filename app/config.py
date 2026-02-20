"""
Application configuration using Pydantic Settings.
All settings are loaded from environment variables (or .env file).
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # LINE Messaging API
    line_channel_secret: str = ""
    line_channel_access_token: str = ""

    # Anthropic Claude API
    anthropic_api_key: str = ""

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/chatbot.db"

    # Vector Database
    chroma_persist_dir: str = "./data/chroma_db"

    # Application
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    log_level: str = "INFO"

    # Bot Persona
    bot_name: str = "น้องแอร์"
    bot_persona: str = "ผู้ช่วย HR ที่ตอบคำถามด้วยความสุภาพและชัดเจน"

    # RAG Settings
    rag_top_k: int = 5
    rag_score_threshold: float = 0.3
    chunk_size: int = 1000
    chunk_overlap: int = 200

    # Claude Model
    claude_model: str = "claude-opus-4-6"
    max_tokens: int = 2048


@lru_cache
def get_settings() -> Settings:
    return Settings()
