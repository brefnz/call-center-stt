"""
Entry point aplikasi.

Jalankan dengan:
    uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

Mode:
  - ARI_ENABLED=1 (default kalau .env berisi kredensial ARI valid): akan
    mencoba konek ke Asterisk sungguhan dan menjalankan pipeline real-time.
  - Kalau Asterisk belum tersedia / lagi development, pipeline tetap bisa
    dites lewat endpoint POST /calls/transcript-event atau
    scripts/test_pipeline_offline.py (simulasi dari file audio .wav).
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app import db
from app.ari_client import run_ari_listener
from app.routers import calls, kb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("main")

_ari_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _ari_task
    db.init_db()
    logger.info("Database siap.")

    try:
        _ari_task = asyncio.create_task(run_ari_listener())
        logger.info("ARI listener dijalankan sebagai background task.")
    except Exception:
        logger.exception(
            "Tidak bisa menjalankan ARI listener (Asterisk belum tersedia?). "
            "Server tetap jalan -- gunakan endpoint /calls/transcript-event "
            "atau scripts/test_pipeline_offline.py untuk testing tanpa Asterisk."
        )

    yield

    if _ari_task:
        _ari_task.cancel()


app = FastAPI(
    title="Real-Time STT & KB Assist untuk Call Center",
    description="Prototype sesuai PRD: FreePBX/Asterisk -> ARI -> STT (faster-whisper) -> KB search -> Epic",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # persempit di production sesuai origin Epic
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(kb.router)
app.include_router(calls.router)

# Serve halaman demo ticketing + widget JS langsung dari server yang sama
# (http://localhost:8000/demo/index.html) supaya testing tidak perlu Live
# Server/dev-server terpisah lagi -- ini juga menghilangkan resiko mixed
# content, karena origin backend & frontend jadi PERSIS SAMA.
_DEMO_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "demo_frontend"
if _DEMO_FRONTEND_DIR.is_dir():
    app.mount("/demo", StaticFiles(directory=str(_DEMO_FRONTEND_DIR), html=True), name="demo")


@app.get("/health")
def health():
    return {"status": "ok"}
