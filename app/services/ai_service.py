"""
AI service – wraps Anthropic Claude API.

Features:
  - Multi-turn conversation with full message history
  - RAG context injection
  - Adaptive thinking (Opus 4.6)
  - Streaming with get_final_message()
  - System prompt for HR assistant persona
"""
import json
from typing import Optional

import anthropic

from app.config import get_settings
from app.utils.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)

# -----------------------------------------------------------------------
# System prompt – defines the bot persona and guardrails
# -----------------------------------------------------------------------
SYSTEM_PROMPT_TEMPLATE = """คุณคือ {bot_name} {bot_persona} ของบริษัท SPBT

## บทบาทและหน้าที่
คุณทำหน้าที่เป็นผู้ช่วย HR อัจฉริยะที่ตอบคำถามพนักงานเกี่ยวกับ:
- นโยบายและกฎระเบียบของบริษัท
- โครงสร้างเงินเดือนและสวัสดิการ
- แนวทางการประเมิน KPI ปี 2026
- ขั้นตอนการลา การขอเอกสาร และอื่นๆ

## หลักการตอบคำถาม
1. ตอบจากข้อมูลใน Knowledge Base เสมอ – อย่าคาดเดาหรือแต่งข้อมูลขึ้นมาเอง
2. หากไม่มีข้อมูลในระบบ ให้แจ้งตรงๆ ว่า "ขณะนี้ไม่มีข้อมูลนี้ในระบบ กรุณาติดต่อฝ่าย HR โดยตรง"
3. ใช้ภาษาไทยที่สุภาพ เป็นกันเอง และเข้าใจง่าย
4. ตอบกระชับ ชัดเจน มีหัวข้อย่อยถ้าจำเป็น
5. ห้ามเปิดเผยข้อมูลส่วนตัวของพนักงานคนอื่น
6. ห้ามให้คำแนะนำด้านกฎหมายหรือการแพทย์โดยตรง

## รูปแบบการตอบ
- ใช้ bullet points เมื่อมีหลายรายการ
- เพิ่ม emoji ที่เหมาะสมเพื่อความเป็นกันเอง (📋 สำหรับนโยบาย, 💰 สำหรับเงินเดือน, 📅 สำหรับการลา)
- ปิดท้ายด้วยการถามว่ามีคำถามเพิ่มเติมหรือไม่

{context_section}"""

CONTEXT_SECTION_TEMPLATE = """## ข้อมูลอ้างอิงจาก Knowledge Base
{context}

(ใช้ข้อมูลด้านบนเป็นหลักในการตอบ หากคำถามเกี่ยวข้องกับเนื้อหาในนั้น)"""


class AIService:
    def __init__(self) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def build_system_prompt(self, rag_context: str = "") -> str:
        context_section = ""
        if rag_context.strip():
            context_section = CONTEXT_SECTION_TEMPLATE.format(context=rag_context)

        return SYSTEM_PROMPT_TEMPLATE.format(
            bot_name=settings.bot_name,
            bot_persona=settings.bot_persona,
            context_section=context_section,
        )

    def chat(
        self,
        user_message: str,
        conversation_history: list[dict],
        rag_context: str = "",
    ) -> tuple[str, int]:
        """
        Send a message to Claude and get a response.

        Args:
            user_message:          The latest user text.
            conversation_history:  Previous turns [{role, content}, ...].
            rag_context:           Retrieved knowledge base context (optional).

        Returns:
            (response_text, tokens_used)
        """
        system_prompt = self.build_system_prompt(rag_context)

        # Append the new user message to history
        messages = conversation_history + [
            {"role": "user", "content": user_message}
        ]

        try:
            # Use streaming + get_final_message() to avoid HTTP timeouts on
            # long responses (Opus 4.6 supports up to 128K output tokens).
            with self._client.messages.stream(
                model=settings.claude_model,
                max_tokens=settings.max_tokens,
                thinking={"type": "adaptive"},    # Adaptive thinking for Opus 4.6
                system=system_prompt,
                messages=messages,
            ) as stream:
                final = stream.get_final_message()

            # Extract the text response (skip thinking blocks)
            response_text = ""
            for block in final.content:
                if block.type == "text":
                    response_text += block.text

            tokens_used = final.usage.output_tokens
            logger.info(
                "Claude response",
                input_tokens=final.usage.input_tokens,
                output_tokens=tokens_used,
                model=settings.claude_model,
            )
            return response_text.strip(), tokens_used

        except anthropic.RateLimitError:
            logger.error("Claude rate limited")
            return "ขณะนี้ระบบมีผู้ใช้งานหนาแน่น กรุณาลองใหม่อีกครั้งในอีกสักครู่ค่ะ 🙏", 0
        except anthropic.APIConnectionError:
            logger.error("Claude connection error")
            return "ขณะนี้ไม่สามารถเชื่อมต่อกับระบบ AI ได้ กรุณาลองใหม่อีกครั้งค่ะ 🙏", 0
        except anthropic.APIStatusError as e:
            logger.error("Claude API error", status=e.status_code, message=e.message)
            return "เกิดข้อผิดพลาดในระบบ กรุณาติดต่อผู้ดูแลระบบค่ะ 🙏", 0


# Singleton
_ai_service: Optional[AIService] = None


def get_ai_service() -> AIService:
    global _ai_service
    if _ai_service is None:
        _ai_service = AIService()
    return _ai_service
