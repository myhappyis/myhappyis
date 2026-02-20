"""
Employee Account Linkage web endpoints.

These pages are opened inside LINE's in-app browser (LIFF / webview)
when the user taps the "Link Account" button sent by the bot.

Routes:
  GET  /auth/verify          → HTML login form (Employee ID input)
  POST /auth/request-otp     → Issue OTP (JSON API)
  POST /auth/confirm-otp     → Verify OTP & link account (JSON API)
  GET  /auth/success          → Success confirmation page
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.middleware.rate_limiter import _get_client_ip
from app.models.database import get_db
from app.services.auth_service import confirm_otp, is_linked, request_otp
from app.utils.logger import get_logger

router = APIRouter(prefix="/auth", tags=["Account Linkage"])
logger = get_logger(__name__)


# ── HTML helpers ───────────────────────────────────────────────────────────────

_BASE_STYLE = """
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'Segoe UI', sans-serif; background: #f0f4f8;
         display: flex; justify-content: center; align-items: center;
         min-height: 100vh; padding: 16px; }
  .card { background: white; border-radius: 16px; padding: 32px 24px;
          max-width: 380px; width: 100%; box-shadow: 0 4px 24px rgba(0,0,0,.10); }
  h1 { font-size: 1.4rem; color: #1a1a2e; margin-bottom: 8px; }
  p  { color: #666; font-size: .9rem; margin-bottom: 20px; line-height: 1.5; }
  label { display: block; font-size: .85rem; color: #444;
          margin-bottom: 6px; font-weight: 600; }
  input { width: 100%; padding: 12px 14px; border: 1.5px solid #d0d7de;
          border-radius: 8px; font-size: 1rem; outline: none; transition: .2s; }
  input:focus { border-color: #06C755; }
  button { width: 100%; padding: 13px; background: #06C755; color: white;
           border: none; border-radius: 8px; font-size: 1rem; font-weight: 700;
           cursor: pointer; margin-top: 12px; transition: .2s; }
  button:hover { background: #05a847; }
  .err { color: #c0392b; font-size: .85rem; margin-top: 8px; }
  .note { font-size: .78rem; color: #999; margin-top: 16px; text-align: center; }
  .logo { text-align: center; margin-bottom: 20px; }
  .logo span { font-size: 2rem; }
</style>
"""


def _html(body: str) -> str:
    return f"<!DOCTYPE html><html lang='th'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>SPBT HR – ยืนยันตัวตน</title>{_BASE_STYLE}</head><body>{body}</body></html>"


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.get("/verify", response_class=HTMLResponse)
async def verify_page(line_user_id: str, db: AsyncSession = Depends(get_db)):
    """
    Step 1: Show the Employee-ID input form.

    The LINE user_id is passed as a query param so the page knows which
    LINE account to link after OTP success.
    """
    if not line_user_id:
        return HTMLResponse("<p>Invalid link. Please return to LINE.</p>", status_code=400)

    # Already linked?
    if await is_linked(line_user_id, db):
        return HTMLResponse(
            _html(
                "<div class='card'>"
                "<div class='logo'><span>✅</span></div>"
                "<h1>บัญชีถูกผูกแล้ว</h1>"
                "<p>LINE ของคุณเชื่อมกับระบบ HR เรียบร้อยแล้ว "
                "กลับไปที่ LINE เพื่อถามคำถาม HR ได้เลยค่ะ</p>"
                "</div>"
            )
        )

    form_html = f"""
    <div class="card">
      <div class="logo"><span>🔐</span></div>
      <h1>ยืนยันตัวตนพนักงาน</h1>
      <p>กรอกรหัสพนักงานของคุณเพื่อเชื่อมบัญชี LINE กับระบบ HR Assistant ของ SPBT</p>

      <form id="empForm">
        <input type="hidden" id="lineUserId" value="{line_user_id}">
        <label for="empId">รหัสพนักงาน (Employee ID)</label>
        <input type="text" id="empId" placeholder="เช่น EMP001" autocomplete="off"
               inputmode="text" maxlength="20">
        <div class="err" id="err1"></div>
        <button type="submit">ขอรหัส OTP →</button>
      </form>

      <div class="note">ระบบจะส่งรหัส OTP ไปยังอีเมลบริษัทของคุณ</div>
    </div>

    <script>
    document.getElementById('empForm').addEventListener('submit', async (e) => {{
      e.preventDefault();
      const empId = document.getElementById('empId').value.trim();
      const uid   = document.getElementById('lineUserId').value;
      const errEl = document.getElementById('err1');
      errEl.textContent = '';
      if (!empId) {{ errEl.textContent = 'กรุณากรอกรหัสพนักงาน'; return; }}

      const btn = e.target.querySelector('button');
      btn.disabled = true; btn.textContent = 'กำลังส่ง OTP...';

      try {{
        const res  = await fetch('/auth/request-otp', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ employee_id: empId, line_user_id: uid }})
        }});
        const data = await res.json();
        if (data.ok) {{
          sessionStorage.setItem('empId', empId);
          sessionStorage.setItem('uid',   uid);
          // Replace form with OTP entry
          document.querySelector('.card').innerHTML = `
            <div class="logo"><span>📩</span></div>
            <h1>ป้อนรหัส OTP</h1>
            <p>ระบบส่งรหัส OTP ไปยังอีเมลบริษัทของคุณแล้ว
               (หมดอายุใน ${{Math.floor(data.expires_in / 60)}} นาที)</p>
            ${{data.otp ? '<p style="background:#fff3cd;padding:10px;border-radius:8px;font-size:.85rem;">🧪 Dev mode – OTP: <b>' + data.otp + '</b></p>' : ''}}
            <form id="otpForm">
              <label for="otp">รหัส OTP (6 หลัก)</label>
              <input type="text" id="otp" placeholder="000000"
                     inputmode="numeric" maxlength="6" autocomplete="one-time-code">
              <div class="err" id="err2"></div>
              <button type="submit">ยืนยัน →</button>
            </form>
            <div class="note">ไม่ได้รับ OTP? ติดต่อ IT/HR โดยตรง</div>
          `;
          document.getElementById('otpForm').addEventListener('submit', submitOtp);
        }} else {{
          errEl.textContent = data.error || 'เกิดข้อผิดพลาด';
          btn.disabled = false; btn.textContent = 'ขอรหัส OTP →';
        }}
      }} catch {{
        errEl.textContent = 'ไม่สามารถเชื่อมต่อได้ กรุณาลองใหม่';
        btn.disabled = false; btn.textContent = 'ขอรหัส OTP →';
      }}
    }});

    async function submitOtp(e) {{
      e.preventDefault();
      const otp   = document.getElementById('otp').value.trim();
      const empId = sessionStorage.getItem('empId');
      const uid   = sessionStorage.getItem('uid');
      const errEl = document.getElementById('err2');
      errEl.textContent = '';
      if (otp.length !== 6) {{ errEl.textContent = 'กรุณากรอก OTP 6 หลัก'; return; }}

      const btn = e.target.querySelector('button');
      btn.disabled = true; btn.textContent = 'กำลังตรวจสอบ...';

      try {{
        const res  = await fetch('/auth/confirm-otp', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{ employee_id: empId, otp: otp, line_user_id: uid }})
        }});
        const data = await res.json();
        if (data.ok) {{
          window.location.href = '/auth/success';
        }} else {{
          errEl.textContent = data.error || 'OTP ไม่ถูกต้อง';
          btn.disabled = false; btn.textContent = 'ยืนยัน →';
        }}
      }} catch {{
        errEl.textContent = 'ไม่สามารถเชื่อมต่อได้ กรุณาลองใหม่';
        btn.disabled = false; btn.textContent = 'ยืนยัน →';
      }}
    }}
    </script>
    """
    return HTMLResponse(_html(form_html))


@router.post("/request-otp")
async def request_otp_endpoint(
    request: Request, db: AsyncSession = Depends(get_db)
):
    """Issue an OTP for the given employee_id."""
    body = await request.json()
    employee_id = (body.get("employee_id") or "").strip().upper()
    line_user_id = (body.get("line_user_id") or "").strip()

    if not employee_id or not line_user_id:
        return JSONResponse({"ok": False, "error": "ข้อมูลไม่ครบถ้วน"}, status_code=400)

    ip = _get_client_ip(request)
    result = await request_otp(employee_id, line_user_id, db, ip_address=ip)
    await db.commit()
    return JSONResponse(result)


@router.post("/confirm-otp")
async def confirm_otp_endpoint(
    request: Request, db: AsyncSession = Depends(get_db)
):
    """Verify OTP and link the LINE account to the employee record."""
    body = await request.json()
    employee_id = (body.get("employee_id") or "").strip().upper()
    otp = (body.get("otp") or "").strip()
    line_user_id = (body.get("line_user_id") or "").strip()

    if not all([employee_id, otp, line_user_id]):
        return JSONResponse({"ok": False, "error": "ข้อมูลไม่ครบถ้วน"}, status_code=400)

    ip = _get_client_ip(request)
    result = await confirm_otp(employee_id, otp, line_user_id, db, ip_address=ip)
    await db.commit()

    if result["ok"]:
        emp = result["employee"]
        return JSONResponse({
            "ok": True,
            "employee_id": emp.employee_id,
            "name": emp.full_name,
            "department": emp.department,
        })
    return JSONResponse(result, status_code=200)


@router.get("/success", response_class=HTMLResponse)
async def success_page():
    """Shown after successful account linkage."""
    return HTMLResponse(
        _html(
            "<div class='card'>"
            "<div class='logo'><span>🎉</span></div>"
            "<h1>เชื่อมบัญชีสำเร็จ!</h1>"
            "<p>ตอนนี้คุณสามารถถามคำถามด้าน HR ผ่าน LINE ได้แล้วค่ะ<br>"
            "กลับไปที่แอป LINE แล้วพิมพ์คำถามของคุณได้เลย 😊</p>"
            "<div class='note'>ปิดหน้านี้แล้วกลับไปที่ LINE</div>"
            "</div>"
        )
    )
