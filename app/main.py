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
from app.stt_engine import get_stt_engine

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

    # PENTING: load model faster-whisper DI SINI (startup), BUKAN lazy
    # pas panggilan pertama masuk. Loading model itu proses BERAT &
    # BLOCKING (bisa beberapa detik, apalagi kalau masih perlu download).
    # Kalau dibiarkan lazy, dia akan ke-trigger dari DALAM event handler
    # ARI (app/ari_client.py: _setup_capture_for_channel) yang jalan di
    # event loop yang SAMA dengan listener WebSocket ARI -- selama model
    # loading, event loop gak bisa balas ping dari Asterisk (bikin ARI
    # WebSocket dianggap mati/reconnect) DAN event ARI yang masuk PAS
    # momen itu (mis. ChannelStateChange ke "Up" saat agent jawab telpon)
    # bisa KELEWAT sama sekali kalau reconnect terjadi di waktu bersamaan
    # -- akibatnya capture untuk panggilan itu tidak pernah dimulai.
    # run_in_executor supaya loading (walau lama) tidak memblokir event
    # loop startup FastAPI itu sendiri.
    logger.info("Memuat model faster-whisper di awal (supaya tidak blocking saat panggilan pertama masuk) ...")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, get_stt_engine)
    logger.info("Model faster-whisper siap.")

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
