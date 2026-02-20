# HR Chatbot – SPBT (LINE × Claude × RAG)

LINE Chatbot สำหรับตอบคำถามพนักงานด้าน HR ขับเคลื่อนด้วย **Claude Opus 4.6** และ **RAG (Retrieval-Augmented Generation)**

---

## สถาปัตยกรรมระบบ

```
LINE Official Account
        │  (Webhook HTTPS POST)
        ▼
┌─────────────────────────────────────────────┐
│           FastAPI (app/main.py)             │
│  POST /webhook  ←→  GET /health             │
└───────────────────┬─────────────────────────┘
                    │
        ┌───────────▼───────────┐
        │   Message Handler     │
        │  (routes/webhook.py)  │
        └──┬────────────────┬───┘
           │                │
     ┌─────▼──────┐  ┌──────▼──────────┐
     │ RAG Service│  │   AI Service    │
     │ (ChromaDB) │  │ (Claude Opus    │
     │            │  │  4.6 + Stream)  │
     └─────┬──────┘  └──────┬──────────┘
           │                │
     ┌─────▼────────────────▼──────────┐
     │         SQLite Database         │
     │  chat_sessions / chat_messages  │
     └─────────────────────────────────┘
```

### ส่วนประกอบหลัก

| Component | Technology | หน้าที่ |
|-----------|-----------|--------|
| Web Framework | FastAPI + Uvicorn | รับ-ส่ง HTTP |
| AI Engine | Claude Opus 4.6 (Anthropic) | สร้างคำตอบ |
| Vector DB | ChromaDB | เก็บ embeddings เอกสาร HR |
| Relational DB | SQLite (dev) / PostgreSQL (prod) | ประวัติการแชท |
| LINE Integration | LINE Messaging API v3 | ช่องทาง user |

---

## การติดตั้งและรันในเครื่อง (Local Development)

### 1. Clone และติดตั้ง dependencies

```bash
git clone <repo-url>
cd myhappyis
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. ตั้งค่า environment variables

```bash
cp .env.example .env
# แก้ไข .env ด้วย editor ที่ถนัด:
#   LINE_CHANNEL_SECRET=...
#   LINE_CHANNEL_ACCESS_TOKEN=...
#   ANTHROPIC_API_KEY=...
```

### 3. รันเซิร์ฟเวอร์

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

เปิด http://localhost:8000/docs เพื่อดู Swagger UI

### 4. ทดสอบ Webhook ด้วย ngrok

```bash
ngrok http 8000
# นำ URL ที่ได้ (https://xxxx.ngrok-free.app/webhook)
# ไปตั้งใน LINE Developers Console → Messaging API → Webhook URL
```

---

## การจัดการ Knowledge Base

### เพิ่มเอกสาร HR

วางไฟล์ `.md` หรือ `.txt` ลงในโฟลเดอร์ `knowledge_base/` แล้วรัน:

```bash
python scripts/ingest_docs.py           # เพิ่มเอกสารใหม่
python scripts/ingest_docs.py --reset   # Re-index ทั้งหมด
```

เอกสารที่รองรับ:
- Markdown (`.md`) – แนะนำ
- Plain text (`.txt`)

---

## Deploy บน Docker

```bash
cp .env.example .env     # ตั้งค่า secrets
docker compose up -d
docker compose logs -f   # ดู logs
```

---

## Deploy บน Google Cloud Run

```bash
# Build และ push image
gcloud builds submit --tag gcr.io/YOUR_PROJECT/hr-chatbot

# Deploy
gcloud run deploy hr-chatbot \
  --image gcr.io/YOUR_PROJECT/hr-chatbot \
  --platform managed \
  --region asia-southeast1 \
  --allow-unauthenticated \
  --set-env-vars "LINE_CHANNEL_SECRET=...,LINE_CHANNEL_ACCESS_TOKEN=...,ANTHROPIC_API_KEY=..."
```

---

## โครงสร้างไฟล์

```
myhappyis/
├── app/
│   ├── main.py              # FastAPI app + lifespan
│   ├── config.py            # Settings (pydantic-settings)
│   ├── models/
│   │   └── database.py      # SQLAlchemy models + async engine
│   ├── routes/
│   │   └── webhook.py       # LINE webhook handler
│   ├── services/
│   │   ├── ai_service.py    # Claude API integration
│   │   ├── line_service.py  # LINE Messaging API
│   │   └── rag_service.py   # ChromaDB RAG
│   └── utils/
│       └── logger.py        # structlog structured logging
├── knowledge_base/
│   ├── hr_policy.md         # นโยบาย HR
│   └── kpi_2026.md          # แนวทาง KPI 2026
├── scripts/
│   └── ingest_docs.py       # Document ingestion CLI
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LINE_CHANNEL_SECRET` | ✅ | – | LINE channel secret |
| `LINE_CHANNEL_ACCESS_TOKEN` | ✅ | – | LINE channel access token |
| `ANTHROPIC_API_KEY` | ✅ | – | Anthropic API key |
| `DATABASE_URL` | – | `sqlite+aiosqlite:///./data/chatbot.db` | DB connection string |
| `CHROMA_PERSIST_DIR` | – | `./data/chroma_db` | ChromaDB storage path |
| `CLAUDE_MODEL` | – | `claude-opus-4-6` | Claude model ID |
| `RAG_TOP_K` | – | `5` | Number of retrieved chunks |
| `BOT_NAME` | – | `น้องแอร์` | Bot display name |

---

## แผนพัฒนาต่อ (Roadmap)

- [ ] Phase 2: BI Dashboard เชื่อม PostgreSQL → Power BI
- [ ] Phase 3: รองรับไฟล์ PDF / Excel ใน knowledge base
- [ ] Phase 4: Flex Message สำหรับตารางและกราฟ
- [ ] Phase 5: LINE Login + RBAC (พนักงาน/หัวหน้า/HR)
