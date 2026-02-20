"""
LINE Messaging API service.

Handles:
  - Webhook signature verification
  - Parsing incoming events (text messages, follow/unfollow)
  - Sending reply messages
  - Fetching user profile
"""
import hashlib
import hmac
import json
from base64 import b64decode, b64encode
from typing import Optional

import httpx

from app.config import get_settings
from app.utils.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)

LINE_API_BASE = "https://api.line.me/v2/bot"
REPLY_URL = f"{LINE_API_BASE}/message/reply"
PROFILE_URL = f"{LINE_API_BASE}/profile"


class LineService:
    def __init__(self) -> None:
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.line_channel_access_token}",
        }

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------

    def verify_signature(self, body: bytes, x_line_signature: str) -> bool:
        """Verify that the request is genuinely from LINE."""
        hash_value = hmac.new(
            settings.line_channel_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected = b64encode(hash_value).decode("utf-8")
        return hmac.compare_digest(expected, x_line_signature)

    # ------------------------------------------------------------------
    # Event parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def parse_events(payload: dict) -> list[dict]:
        return payload.get("events", [])

    @staticmethod
    def get_text_from_event(event: dict) -> Optional[str]:
        msg = event.get("message", {})
        if msg.get("type") == "text":
            return msg.get("text", "").strip()
        return None

    @staticmethod
    def get_user_id(event: dict) -> Optional[str]:
        return event.get("source", {}).get("userId")

    @staticmethod
    def get_reply_token(event: dict) -> Optional[str]:
        return event.get("replyToken")

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------

    async def get_user_profile(self, user_id: str) -> Optional[dict]:
        """Fetch the LINE display name and profile picture of a user."""
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(
                    f"{PROFILE_URL}/{user_id}",
                    headers=self._headers,
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    return resp.json()
            except httpx.RequestError as e:
                logger.warning("Failed to fetch LINE profile", error=str(e))
        return None

    async def reply_text(self, reply_token: str, text: str) -> bool:
        """Send a plain text reply to the user."""
        payload = {
            "replyToken": reply_token,
            "messages": [{"type": "text", "text": text}],
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    REPLY_URL,
                    headers=self._headers,
                    json=payload,
                    timeout=10.0,
                )
                if resp.status_code != 200:
                    logger.error(
                        "LINE reply failed",
                        status=resp.status_code,
                        body=resp.text[:200],
                    )
                    return False
                return True
            except httpx.RequestError as e:
                logger.error("LINE reply request error", error=str(e))
                return False

    async def reply_flex(self, reply_token: str, flex_contents: dict) -> bool:
        """Send a Flex Message reply."""
        payload = {
            "replyToken": reply_token,
            "messages": [
                {
                    "type": "flex",
                    "altText": "ข้อความจาก HR Assistant",
                    "contents": flex_contents,
                }
            ],
        }
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    REPLY_URL,
                    headers=self._headers,
                    json=payload,
                    timeout=10.0,
                )
                return resp.status_code == 200
            except httpx.RequestError as e:
                logger.error("LINE flex reply error", error=str(e))
                return False


# Singleton
_line_service: Optional[LineService] = None


def get_line_service() -> LineService:
    global _line_service
    if _line_service is None:
        _line_service = LineService()
    return _line_service
