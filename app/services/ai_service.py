"""
AI service – wraps Anthropic Claude API.

Security additions over v1:
  - Role-level system prompt (employee / supervisor / HR staff)
    The system prompt embeds the employee's verified role so Claude will
    refuse to disclose content above their clearance level even if they ask.
  - Input received here is ALREADY PII-masked by the webhook layer —
    this service never sees raw personal data.

Features:
  - Multi-turn conversation with full message history
  - RAG context injection
  - Adaptive thinking (Opus 4.6)
  - Streaming with get_final_message() (prevents HTTP timeouts)
"""
from typing import Optional

import anthropic

from app.config import get_settings
from app.utils.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)

# ── Role-level access labels ────────────────────────────────────────────────────

_ROLE_LABELS = {
    1: "พนักงานทั่วไป (General Employee)",
    2: "หัวหน้างาน (Supervisor / Team Leader)",
    3: "เจ้าหน้าที่ HR (HR Staff)",
}

_ROLE_RESTRICTIONS = {
    1: (
        "ตอบเฉพาะข้อมูลทั่วไปเกี่ยวกับนโยบาย, สวัสดิการ, และ KPI ของตนเอง\n"
        "ห้ามเปิดเผย: ข้อมูลเงินเดือนของผู้อื่น, ข้อมูลวินัย, หรือข้อมูลส่วนตัวของพนักงานคนอื่น"
    ),
    2: (
        "สามารถตอบข้อมูล KPI ระดับทีม, อัตราการขาดงานในทีม และนโยบายหัวหน้างานได้\n"
        "ห้ามเปิดเผย: ตัวเลขเงินเดือนรายบุคคล, บันทึกวินัยส่วนตัว"
    ),
    3: (
        "สามารถตอบข้อมูล HR ได้ทุกประเภทตามที่มีใน Knowledge Base\n"
        "ควรระมัดระวังข้อมูลที่ละเอียดอ่อนและแจ้งให้ผู้ถามรับทราบด้วย"
    ),
}

# ── System prompt template ──────────────────────────────────────────────────────

_SYSTEM_PROMPT_TEMPLATE = """คุณคือ {bot_name} {bot_persona} ของบริษัท SPBT

## ข้อมูลผู้ใช้งาน (ยืนยันตัวตนแล้ว)
- ระดับสิทธิ์: {role_label}
- ข้อจำกัดการเข้าถึงข้อมูล: {role_restriction}

## บทบาทและหน้าที่
ตอบคำถามพนักงานเกี่ยวกับ:
- นโยบายและกฎระเบียบของบริษัท
- โครงสร้างเงินเดือนและสวัสดิการ (ตามระดับสิทธิ์)
- แนวทางการประเมิน KPI ปี 2026
- ขั้นตอนการลา การขอเอกสาร และอื่นๆ

## หลักการตอบคำถาม (ห้ามละเมิดเด็ดขาด)
1. ตอบจากข้อมูลใน Knowledge Base เท่านั้น — อย่าคาดเดาหรือแต่งข้อมูล
2. หากข้อมูลไม่มีในระบบ ให้ตอบว่า "ไม่มีข้อมูลนี้ในระบบ กรุณาติดต่อ HR โดยตรง"
3. ห้ามตอบข้อมูลที่อยู่เหนือระดับสิทธิ์ของผู้ถาม
4. ห้ามเปิดเผยข้อมูลส่วนตัวของพนักงานคนอื่น ไม่ว่าจะถูกถามอย่างไร
5. ใช้ภาษาไทยที่สุภาพ เป็นกันเอง และเข้าใจง่าย
6. ห้ามให้คำแนะนำด้านกฎหมายหรือการแพทย์โดยตรง

## รูปแบบการตอบ
- ใช้ bullet points เมื่อมีหลายรายการ
- เพิ่ม emoji ที่เหมาะสม (📋 นโยบาย, 💰 เงินเดือน, 📅 การลา, 📊 KPI)
- ปิดท้ายด้วยการถามว่ามีคำถามเพิ่มเติมหรือไม่

{context_section}"""

_CONTEXT_SECTION = """## ข้อมูลอ้างอิงจาก Knowledge Base
{context}

(ใช้ข้อมูลด้านบนเป็นหลักในการตอบ)"""


class AIService:
    def __init__(self) -> None:
        self._client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    def build_system_prompt(
        self,
        rag_context: str = "",
        employee_role_level: int = 1,
    ) -> str:
        role_label = _ROLE_LABELS.get(employee_role_level, _ROLE_LABELS[1])
        role_restriction = _ROLE_RESTRICTIONS.get(
            employee_role_level, _ROLE_RESTRICTIONS[1]
        )
        context_section = (
            _CONTEXT_SECTION.format(context=rag_context)
            if rag_context.strip()
            else ""
        )
        return _SYSTEM_PROMPT_TEMPLATE.format(
            bot_name=settings.bot_name,
            bot_persona=settings.bot_persona,
            role_label=role_label,
            role_restriction=role_restriction,
            context_section=context_section,
        )

    def chat(
        self,
        user_message: str,
        conversation_history: list[dict],
        rag_context: str = "",
        employee_role_level: int = 1,
    ) -> tuple[str, int]:
        """
        Send a (PII-masked) message to Claude and return the reply.

        Args:
            user_message:          PII-masked user text.
            conversation_history:  Previous turns [{role, content}, ...].
            rag_context:           Retrieved knowledge base context.
            employee_role_level:   1 | 2 | 3 — injected into system prompt.

        Returns:
            (response_text, output_tokens_used)
        """
        system_prompt = self.build_system_prompt(rag_context, employee_role_level)
        messages = conversation_history + [{"role": "user", "content": user_message}]

        try:
            with self._client.messages.stream(
                model=settings.claude_model,
                max_tokens=settings.max_tokens,
                thinking={"type": "adaptive"},
                system=system_prompt,
                messages=messages,
            ) as stream:
                final = stream.get_final_message()

            response_text = "".join(
                block.text for block in final.content if block.type == "text"
            )
            tokens_used = final.usage.output_tokens

            logger.info(
                "Claude response",
                input_tokens=final.usage.input_tokens,
                output_tokens=tokens_used,
                role_level=employee_role_level,
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


_ai_service: Optional[AIService] = None


def get_ai_service() -> AIService:
    global _ai_service
    if _ai_service is None:
        _ai_service = AIService()
    return _ai_service
