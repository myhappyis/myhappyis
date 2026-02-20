#!/usr/bin/env python3
"""
Seed the employees table with sample data.

Run once before first use:
  python scripts/seed_employees.py

Safe to re-run (uses INSERT OR IGNORE semantics via upsert).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select

from app.models.database import AsyncSessionLocal, Employee, init_db

SAMPLE_EMPLOYEES = [
    # (employee_id, full_name, department, role_level)
    ("EMP001", "สมชาย ใจดี",       "ฝ่ายผลิต",          1),
    ("EMP002", "สมหญิง รักงาน",    "ฝ่ายผลิต",          1),
    ("EMP003", "วิชัย มั่นใจ",      "ฝ่ายผลิต",          1),
    ("SUP001", "อนันต์ นำทีม",     "ฝ่ายผลิต",          2),
    ("SUP002", "ประภา ดูแล",       "ฝ่ายคลังสินค้า",    2),
    ("HR001",  "นภา ดูแลพนักงาน",  "ฝ่าย HR",           3),
    ("HR002",  "กานต์ ช่วยเหลือ",  "ฝ่าย HR",           3),
]


async def seed() -> None:
    await init_db()

    async with AsyncSessionLocal() as db:
        added = 0
        for emp_id, name, dept, level in SAMPLE_EMPLOYEES:
            result = await db.execute(
                select(Employee).where(Employee.employee_id == emp_id)
            )
            existing = result.scalar_one_or_none()
            if existing is None:
                db.add(
                    Employee(
                        employee_id=emp_id,
                        full_name=name,
                        department=dept,
                        role_level=level,
                    )
                )
                added += 1
                print(f"  ✅  Added  {emp_id} – {name} ({dept}, level {level})")
            else:
                print(f"  ⏭️   Skip   {emp_id} – already exists")

        await db.commit()

    print(f"\nDone. {added} employees added.")
    print("\nEmployee IDs for testing (use with /auth/verify):")
    for emp_id, name, _, level in SAMPLE_EMPLOYEES:
        print(f"  {emp_id}  ({name}, role_level={level})")


if __name__ == "__main__":
    asyncio.run(seed())
