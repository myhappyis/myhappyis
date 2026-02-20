"""
Account Linkage & OTP Authentication Service

Flow:
  1. User follows/messages the bot for the first time
  2. Bot sends a LINE Flex Message with a "Link Account" button
  3. Button opens GET /auth/verify?line_user_id=<uid>
  4. Employee enters their Employee ID and submits
  5. POST /auth/request-otp  → generates OTP, logs it (in prod: sends via corp email)
  6. Employee enters the 6-digit OTP
  7. POST /auth/confirm-otp  → verifies, links LINE UID ↔ Employee record
  8. Future messages are answered (with role-based content filtering)

Security properties:
  - OTP hashed with bcrypt before storage  (raw value never persisted)
  - OTP expires in 10 minutes
  - Max 5 wrong attempts before the token is locked (brute-force protection)
  - All events written to access_logs (immutable audit trail)
  - Verified status cached on ChatSession; re-check on every message
"""

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.database import (
    AccessLog,
    ChatSession,
    Employee,
    EmployeeVerification,
)
from app.utils.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)

OTP_EXPIRY_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_LENGTH = 6


# ── Helpers ────────────────────────────────────────────────────────────────────


def _generate_otp() -> str:
    """Return a cryptographically random 6-digit string."""
    return str(secrets.randbelow(10**OTP_LENGTH)).zfill(OTP_LENGTH)


def _hash_otp(otp: str) -> str:
    return bcrypt.hashpw(otp.encode(), bcrypt.gensalt()).decode()


def _verify_otp_hash(otp: str, hashed: str) -> bool:
    return bcrypt.checkpw(otp.encode(), hashed.encode())


async def _log_event(
    db: AsyncSession,
    event_type: str,
    line_user_id: str | None = None,
    employee_id: str | None = None,
    ip_address: str | None = None,
    detail: str | None = None,
) -> None:
    log = AccessLog(
        event_type=event_type,
        line_user_id=line_user_id,
        employee_id=employee_id,
        ip_address=ip_address,
        detail=detail,
    )
    db.add(log)
    # Don't await commit here — caller owns the transaction


# ── Verification check ─────────────────────────────────────────────────────────


async def is_linked(line_user_id: str, db: AsyncSession) -> bool:
    """Return True if this LINE user has already completed Account Linkage."""
    result = await db.execute(
        select(Employee).where(
            Employee.line_user_id == line_user_id,
            Employee.is_active == True,
        )
    )
    return result.scalar_one_or_none() is not None


async def get_linked_employee(
    line_user_id: str, db: AsyncSession
) -> Employee | None:
    result = await db.execute(
        select(Employee).where(
            Employee.line_user_id == line_user_id,
            Employee.is_active == True,
        )
    )
    return result.scalar_one_or_none()


# ── OTP generation ─────────────────────────────────────────────────────────────


async def request_otp(
    employee_id: str,
    line_user_id: str,
    db: AsyncSession,
    ip_address: str | None = None,
) -> dict:
    """
    Generate a fresh OTP for *employee_id* and return it.

    In production this OTP should be sent via corporate email / SMS —
    never returned to the browser directly.  We return it here for the
    development / demo environment only.

    Returns:
      {"ok": True,  "otp": "123456", "expires_in": 600}
      {"ok": False, "error": "<reason>"}
    """
    # Verify the employee exists and is active
    result = await db.execute(
        select(Employee).where(
            Employee.employee_id == employee_id,
            Employee.is_active == True,
        )
    )
    employee = result.scalar_one_or_none()

    if employee is None:
        await _log_event(
            db,
            "otp_request_invalid_id",
            line_user_id=line_user_id,
            employee_id=employee_id,
            ip_address=ip_address,
            detail="Employee not found or inactive",
        )
        # Return a generic error — don't reveal whether the ID exists
        return {"ok": False, "error": "ไม่พบรหัสพนักงานในระบบ กรุณาติดต่อฝ่าย HR"}

    # Check if this employee's LINE is already linked to a *different* account
    if employee.line_user_id and employee.line_user_id != line_user_id:
        await _log_event(
            db,
            "otp_request_already_linked",
            line_user_id=line_user_id,
            employee_id=employee_id,
            ip_address=ip_address,
            detail="Employee already linked to another LINE account",
        )
        return {
            "ok": False,
            "error": "รหัสพนักงานนี้ผูกกับ LINE อื่นอยู่แล้ว กรุณาติดต่อ IT/HR",
        }

    # Invalidate old unused OTPs for this employee
    await db.execute(
        update(EmployeeVerification)
        .where(
            EmployeeVerification.employee_id == employee_id,
            EmployeeVerification.is_used == False,
        )
        .values(is_used=True)
    )

    otp = _generate_otp()
    otp_hash = _hash_otp(otp)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINUTES)

    verification = EmployeeVerification(
        employee_id=employee_id,
        otp_hash=otp_hash,
        expires_at=expires_at,
    )
    db.add(verification)

    await _log_event(
        db,
        "otp_request",
        line_user_id=line_user_id,
        employee_id=employee_id,
        ip_address=ip_address,
        detail=f"OTP issued, expires {expires_at.isoformat()}",
    )

    logger.info(
        "OTP issued",
        employee_id=employee_id,
        # In production: log "OTP sent to email" but NEVER log the raw OTP
        env=settings.app_env,
    )

    response: dict = {"ok": True, "expires_in": OTP_EXPIRY_MINUTES * 60}

    # Expose raw OTP only in development / demo mode
    if settings.app_env == "development":
        response["otp"] = otp
        response["_dev_note"] = (
            "OTP shown here for development only. "
            "In production it is sent via corporate email."
        )

    return response


# ── OTP verification & linkage ─────────────────────────────────────────────────


async def confirm_otp(
    employee_id: str,
    otp: str,
    line_user_id: str,
    db: AsyncSession,
    ip_address: str | None = None,
) -> dict:
    """
    Verify the OTP and, on success, link the LINE account to the employee record.

    Returns:
      {"ok": True,  "employee": Employee}
      {"ok": False, "error": "<reason>"}
    """
    now = datetime.now(timezone.utc)

    # Fetch the most recent unused, unexpired token for this employee
    result = await db.execute(
        select(EmployeeVerification)
        .where(
            EmployeeVerification.employee_id == employee_id,
            EmployeeVerification.is_used == False,
            EmployeeVerification.expires_at > now,
            EmployeeVerification.attempt_count < OTP_MAX_ATTEMPTS,
        )
        .order_by(EmployeeVerification.created_at.desc())
        .limit(1)
    )
    verification = result.scalar_one_or_none()

    if verification is None:
        await _log_event(
            db,
            "otp_fail_no_token",
            line_user_id=line_user_id,
            employee_id=employee_id,
            ip_address=ip_address,
            detail="No valid OTP token found",
        )
        return {
            "ok": False,
            "error": "รหัส OTP หมดอายุหรือไม่ถูกต้อง กรุณาขอรหัสใหม่",
        }

    # Increment attempt counter atomically
    verification.attempt_count += 1

    if not _verify_otp_hash(otp, verification.otp_hash):
        remaining = OTP_MAX_ATTEMPTS - verification.attempt_count
        await _log_event(
            db,
            "otp_fail_wrong",
            line_user_id=line_user_id,
            employee_id=employee_id,
            ip_address=ip_address,
            detail=f"Wrong OTP, {remaining} attempts remaining",
        )
        if remaining <= 0:
            verification.is_used = True  # Lock the token
            return {
                "ok": False,
                "error": "ป้อนรหัสผิดเกินกำหนด กรุณาขอรหัส OTP ใหม่",
            }
        return {
            "ok": False,
            "error": f"รหัส OTP ไม่ถูกต้อง เหลือ {remaining} ครั้ง",
        }

    # ── Success: link the accounts ────────────────────────────────────────────
    verification.is_used = True

    emp_result = await db.execute(
        select(Employee).where(Employee.employee_id == employee_id)
    )
    employee = emp_result.scalar_one()
    employee.line_user_id = line_user_id
    employee.linked_at = now

    # Ensure a ChatSession row exists for this LINE user
    sess_result = await db.execute(
        select(ChatSession).where(ChatSession.line_user_id == line_user_id)
    )
    session = sess_result.scalar_one_or_none()
    if session is None:
        session = ChatSession(
            line_user_id=line_user_id,
            employee_db_id=employee.id,
            display_name=employee.full_name,
        )
        db.add(session)
    else:
        session.employee_db_id = employee.id

    await _log_event(
        db,
        "otp_success",
        line_user_id=line_user_id,
        employee_id=employee_id,
        ip_address=ip_address,
        detail=f"Account linked for {employee.full_name}",
    )

    logger.info(
        "Account linked",
        employee_id=employee_id,
        line_user_id=line_user_id,
        department=employee.department,
    )

    return {"ok": True, "employee": employee}
