"""
LINE Webhook endpoint (security-hardened).

Auth gate flow:
  ┌──────────────┐   not linked    ┌─────────────────────────────┐
  │  User message│ ──────────────► │  Send Flex "Link Account"   │
  └──────┬───────┘                 └─────────────────────────────┘
         │ linked (Employee verified)
         ▼
  ┌──────────────┐
  │  PII masking │  (before any external API call)
  └──────┬───────┘
         ▼
  ┌──────────────────────────────┐
  │  RAG retrieval (ChromaDB)    │
  └──────┬───────────────────────┘
         ▼
  ┌──────────────────────────────┐
  │  Claude Opus 4.6 (streaming) │
  └──────┬───────────────────────┘
         ▼
  ┌──────────────────────────────┐
  │  Persist masked message + log│
  └──────┬───────────────────────┘
         ▼
  ┌──────────────────────────────┐
  │  LINE reply                  │
  └──────────────────────────────┘
"""
import json

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.database import AccessLog, ChatMessage, ChatSession, get_db
from app.services.ai_service import get_ai_service
from app.services.auth_service import get_linked_employee, is_linked
from app.services.line_service import get_line_service
from app.services.rag_service import get_rag_service
from app.utils.logger import get_logger
from app.utils.pii_masker import mask_pii

settings = get_settings()
router = APIRouter()
logger = get_logger(__name__)

MAX_HISTORY_TURNS = 10


def _build_link_account_flex(line_user_id: str, base_url: str) -> dict:
    """
    Build the LINE Flex Message that prompts the user to link their account.
    The button opens the auth web page inside LINE's built-in browser.
    """
    link_url = f"{base_url}/auth/verify?line_user_id={line_user_id}"
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "🔐 ยืนยันตัวตนพนักงาน",
                    "weight": "bold",
                    "size": "lg",
                    "color": "#ffffff",
                }
            ],
            "backgroundColor": "#06C755",
            "paddingAll": "16px",
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "text",
                    "text": "สวัสดีค่ะ! ฉันคือน้องแอร์ HR Assistant ของ SPBT",
                    "wrap": True,
                    "size": "sm",
                    "color": "#333333",
                },
                {
                    "type": "text",
                    "text": "เพื่อความปลอดภัยของข้อมูล กรุณายืนยันตัวตนด้วยรหัสพนักงานก่อนนะคะ 😊",
                    "wrap": True,
                    "size": "sm",
                    "color": "#555555",
                    "margin": "md",
                },
            ],
            "paddingAll": "16px",
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [
                {
                    "type": "button",
                    "action": {
                        "type": "uri",
                        "label": "ผูกบัญชีพนักงาน →",
                        "uri": link_url,
                    },
                    "style": "primary",
                    "color": "#06C755",
                }
            ],
            "paddingAll": "12px",
        },
    }


async def _handle_message(
    user_id: str,
    reply_token: str,
    user_text: str,
    base_url: str,
    db: AsyncSession,
) -> None:
    """Core message-handling logic (runs in a FastAPI background task)."""

    line_svc = get_line_service()
    ai_svc = get_ai_service()
    rag_svc = get_rag_service()

    # ── 1. Auth gate: employee must be linked ──────────────────────────────
    employee = await get_linked_employee(user_id, db)
    if employee is None:
        flex = _build_link_account_flex(user_id, base_url)
        await line_svc.reply_flex(reply_token, flex)
        db.add(AccessLog(
            event_type="auth_denied",
            line_user_id=user_id,
            detail="Unverified user attempted to chat",
        ))
        await db.commit()
        return

    # ── 2. PII masking (before ANY external API call) ──────────────────────
    masked_text, pii_report = mask_pii(user_text)

    if pii_report.has_pii:
        logger.warning(
            "PII detected and masked",
            user_id=user_id,
            detections=pii_report.detections,
        )
        db.add(AccessLog(
            event_type="pii_masked",
            line_user_id=user_id,
            employee_id=employee.employee_id,
            detail=json.dumps(pii_report.detections),
        ))

    # ── 3. Fetch recent conversation history ───────────────────────────────
    sess_result = await db.execute(
        select(ChatSession).where(ChatSession.line_user_id == user_id)
    )
    session = sess_result.scalar_one_or_none()

    if session is None:
        session = ChatSession(
            line_user_id=user_id,
            employee_db_id=employee.id,
            display_name=employee.full_name,
        )
        db.add(session)
        await db.flush()

    history_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.timestamp.desc())
        .limit(MAX_HISTORY_TURNS * 2)
    )
    recent_messages = list(reversed(history_result.scalars().all()))
    conversation_history = [
        {"role": m.role, "content": m.content} for m in recent_messages
    ]

    # ── 4. RAG retrieval ───────────────────────────────────────────────────
    rag_context, sources = rag_svc.build_context(masked_text)
    top_chunks = rag_svc.retrieve(masked_text)
    top_score = top_chunks[0]["score"] if top_chunks else 0.0

    # ── 5. Call Claude (with role-level context) ───────────────────────────
    bot_reply, tokens_used = ai_svc.chat(
        user_message=masked_text,
        conversation_history=conversation_history,
        rag_context=rag_context,
        employee_role_level=employee.role_level,
    )

    # ── 6. Persist (always store PII-masked version) ───────────────────────
    user_msg = ChatMessage(
        session_id=session.id,
        role="user",
        content=masked_text,
        pii_detected=pii_report.has_pii,
        pii_types=",".join(pii_report.detections.keys()) if pii_report.has_pii else None,
    )
    assistant_msg = ChatMessage(
        session_id=session.id,
        role="assistant",
        content=bot_reply,
        rag_sources=json.dumps(sources, ensure_ascii=False) if sources else None,
        rag_score=top_score or None,
        tokens_used=tokens_used,
    )
    db.add(user_msg)
    db.add(assistant_msg)
    session.message_count = (session.message_count or 0) + 1

    db.add(AccessLog(
        event_type="chat",
        line_user_id=user_id,
        employee_id=employee.employee_id,
        detail=f"tokens={tokens_used} rag_sources={len(sources)}",
    ))

    await db.commit()

    # ── 7. Reply via LINE ──────────────────────────────────────────────────
    await line_svc.reply_text(reply_token, bot_reply)

    logger.info(
        "Message handled",
        employee_id=employee.employee_id,
        department=employee.department,
        pii_masked=pii_report.has_pii,
        rag_sources=sources,
        tokens=tokens_used,
    )


@router.post("/webhook")
async def webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_line_signature: str = Header(alias="X-Line-Signature"),
    db: AsyncSession = Depends(get_db),
):
    """Receive events from LINE, verify signature, dispatch handlers."""
    body = await request.body()

    line_svc = get_line_service()
    if not line_svc.verify_signature(body, x_line_signature):
        raise HTTPException(status_code=403, detail="Invalid LINE signature")

    payload = await request.json()
    events = line_svc.parse_events(payload)

    # Determine the public base URL for Account Linkage links
    base_url = str(request.base_url).rstrip("/")
    if "X-Forwarded-Host" in request.headers:
        scheme = request.headers.get("X-Forwarded-Proto", "https")
        host = request.headers["X-Forwarded-Host"]
        base_url = f"{scheme}://{host}"

    for event in events:
        event_type = event.get("type")

        if event_type == "message":
            user_id = line_svc.get_user_id(event)
            reply_token = line_svc.get_reply_token(event)
            user_text = line_svc.get_text_from_event(event)

            if not (user_id and reply_token and user_text):
                continue

            logger.info("Incoming message", user_id=user_id, preview=user_text[:60])

            background_tasks.add_task(
                _handle_message, user_id, reply_token, user_text, base_url, db
            )

        elif event_type == "follow":
            user_id = line_svc.get_user_id(event)
            reply_token = line_svc.get_reply_token(event)
            if reply_token and user_id:
                linked = await is_linked(user_id, db)
                if linked:
                    await line_svc.reply_text(
                        reply_token,
                        "ยินดีต้อนรับกลับค่ะ! 👋 ถามคำถาม HR ได้เลยนะคะ 😊",
                    )
                else:
                    flex = _build_link_account_flex(user_id, base_url)
                    await line_svc.reply_flex(reply_token, flex)

        elif event_type == "unfollow":
            logger.info("User unfollowed", user_id=line_svc.get_user_id(event))

    return {"status": "ok"}


@router.get("/health")
async def health():
    return {"status": "healthy", "service": "hr-chatbot"}
