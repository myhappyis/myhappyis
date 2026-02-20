"""
LINE Webhook endpoint.

Flow for each incoming text message:
  1. Verify LINE signature
  2. Fetch / create ChatSession for the user
  3. Load recent conversation history (last 10 turns)
  4. Run RAG retrieval for relevant HR documents
  5. Call Claude with history + RAG context
  6. Save messages to DB
  7. Reply via LINE Messaging API
"""
import json

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import ChatMessage, ChatSession, get_db
from app.services.ai_service import get_ai_service
from app.services.line_service import get_line_service
from app.services.rag_service import get_rag_service
from app.utils.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)

# Max conversation turns to include in Claude context (saves tokens)
MAX_HISTORY_TURNS = 10


async def _handle_message(
    user_id: str,
    reply_token: str,
    user_text: str,
    db: AsyncSession,
) -> None:
    """Core message-handling logic (runs in background task)."""

    line_svc = get_line_service()
    ai_svc = get_ai_service()
    rag_svc = get_rag_service()

    # ---- 1. Fetch or create session --------------------------------
    result = await db.execute(
        select(ChatSession).where(ChatSession.line_user_id == user_id)
    )
    session = result.scalar_one_or_none()

    if session is None:
        # Grab the display name from LINE profile on first contact
        profile = await line_svc.get_user_profile(user_id)
        display_name = profile.get("displayName", "") if profile else ""
        session = ChatSession(line_user_id=user_id, display_name=display_name)
        db.add(session)
        await db.flush()

    # ---- 2. Load recent history ------------------------------------
    history_result = await db.execute(
        select(ChatMessage)
        .where(ChatMessage.session_id == session.id)
        .order_by(ChatMessage.timestamp.desc())
        .limit(MAX_HISTORY_TURNS * 2)  # user + assistant pairs
    )
    recent_messages = list(reversed(history_result.scalars().all()))
    conversation_history = [
        {"role": msg.role, "content": msg.content} for msg in recent_messages
    ]

    # ---- 3. RAG retrieval ------------------------------------------
    rag_context, sources = rag_svc.build_context(user_text)
    top_score = 0.0
    if rag_svc.retrieve(user_text):
        top_score = rag_svc.retrieve(user_text)[0]["score"]

    # ---- 4. Call Claude --------------------------------------------
    bot_reply, tokens_used = ai_svc.chat(
        user_message=user_text,
        conversation_history=conversation_history,
        rag_context=rag_context,
    )

    # ---- 5. Persist messages ---------------------------------------
    user_msg = ChatMessage(
        session_id=session.id,
        role="user",
        content=user_text,
    )
    assistant_msg = ChatMessage(
        session_id=session.id,
        role="assistant",
        content=bot_reply,
        rag_sources=json.dumps(sources, ensure_ascii=False) if sources else None,
        rag_score=top_score if top_score else None,
        tokens_used=tokens_used,
    )
    db.add(user_msg)
    db.add(assistant_msg)

    session.message_count = (session.message_count or 0) + 1
    await db.commit()

    # ---- 6. Reply via LINE -----------------------------------------
    await line_svc.reply_text(reply_token, bot_reply)
    logger.info(
        "Message handled",
        user_id=user_id,
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
    """Receive events from LINE and dispatch message handling."""
    body = await request.body()

    line_svc = get_line_service()
    if not line_svc.verify_signature(body, x_line_signature):
        raise HTTPException(status_code=403, detail="Invalid LINE signature")

    payload = await request.json()
    events = line_svc.parse_events(payload)

    for event in events:
        event_type = event.get("type")

        if event_type == "message":
            user_id = line_svc.get_user_id(event)
            reply_token = line_svc.get_reply_token(event)
            user_text = line_svc.get_text_from_event(event)

            if not (user_id and reply_token and user_text):
                continue

            logger.info("Incoming message", user_id=user_id, preview=user_text[:60])

            # Run in background so LINE's 5-second timeout isn't breached
            background_tasks.add_task(
                _handle_message, user_id, reply_token, user_text, db
            )

        elif event_type == "follow":
            # Welcome message when user adds the bot
            user_id = line_svc.get_user_id(event)
            reply_token = line_svc.get_reply_token(event)
            if reply_token:
                welcome = (
                    "สวัสดีค่ะ! ฉันคือน้องแอร์ ผู้ช่วย HR ของบริษัท SPBT 👋\n\n"
                    "ฉันพร้อมช่วยตอบคำถามเกี่ยวกับ:\n"
                    "📋 นโยบายและกฎระเบียบ\n"
                    "💰 โครงสร้างเงินเดือนและสวัสดิการ\n"
                    "📊 การประเมิน KPI ปี 2026\n"
                    "📅 การลาและขอเอกสาร\n\n"
                    "ถามมาได้เลยค่ะ 😊"
                )
                await line_svc.reply_text(reply_token, welcome)

        elif event_type == "unfollow":
            logger.info("User unfollowed", user_id=line_svc.get_user_id(event))

    return {"status": "ok"}


@router.get("/health")
async def health():
    """Health check endpoint (used by Cloud Run / load balancer)."""
    return {"status": "healthy", "service": "hr-chatbot"}
