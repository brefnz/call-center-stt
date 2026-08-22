"""
Pipeline inti yang menyatukan semua komponen:

    audio segmen (VAD)  --STT-->  teks  --KB search-->  saran artikel
        --simpan ke DB (call_transcripts)-->  broadcast ke Epic via WebSocket

Dipakai baik oleh alur real (app/ari_client.py, saat ada panggilan asli di
FreePBX) maupun oleh mode simulasi/offline (scripts/test_pipeline_offline.py)
sehingga logikanya konsisten dan bisa dites tanpa Asterisk yang sesungguhnya.
"""
import asyncio
import logging
import os
import wave

from app import db
from app.config import settings
from app.kb_search import kb_search_engine
from app.ws_manager import ws_manager

logger = logging.getLogger("pipeline")

# --- DEBUG SEMENTARA: simpan tiap segmen audio jadi .wav untuk diperiksa ---
# Set DEBUG_SAVE_WAV=0 di .env (atau hapus baris ini) untuk mematikan lagi
# setelah masalah audio selesai didiagnosis.
DEBUG_SAVE_WAV = os.getenv("DEBUG_SAVE_WAV", "1") == "1"
DEBUG_WAV_DIR = "./debug_audio"
if DEBUG_SAVE_WAV:
    os.makedirs(DEBUG_WAV_DIR, exist_ok=True)


def _save_debug_wav(call_id: str, speaker: str, pcm_bytes: bytes, sample_rate: int):
    """
    Dipanggil dari app/ari_client.py._process_final_segment tiap satu
    segmen ucapan (hasil VAD) selesai -- dipindah ke sini (bukan lagi
    dari process_audio_segment, karena fungsi itu sudah dihapus setelah
    pindah ke StreamingSession, lihat app/stt_engine.py) supaya dev bisa
    dengerin langsung hasil rekaman tiap ucapan untuk debugging kualitas
    audio (lihat riwayat diagnosis reconnect loop & kualitas audio).
    """
    if not DEBUG_SAVE_WAV or not pcm_bytes:
        return
    try:
        safe_call_id = call_id.replace(":", "_").replace("/", "_")
        fname = f"{DEBUG_WAV_DIR}/{safe_call_id}_{speaker}_{len(pcm_bytes)}.wav"
        with wave.open(fname, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        logger.info("DEBUG: segmen audio disimpan ke %s", fname)
    except Exception:
        logger.exception("DEBUG: gagal simpan wav")


async def broadcast_live_transcript(call_id: str, speaker: str, text: str, is_final: bool):
    """
    Broadcast hasil PREVIEW dari StreamingSession (lihat stt_engine.py) --
    BEDA dari process_transcript_text yang menyimpan ke DB dan memicu KB
    search. Fungsi ini murni untuk tampilan real-time yang terus "nyempurna"
    di sisi Epic/CRM (mis. "halo" -> "halo selamat" -> "halo selamat pagi"),
    dipanggil tiap StreamingSession.feed() menghasilkan hipotesis baru.

    is_final selalu False dari StreamingSession.feed() (preview masih bisa
    berubah) -- parameter ini dipertahankan untuk kompatibilitas pesan WS
    yang sudah ada (tipe "transcript_live_final" disediakan kalau nanti
    ada sumber lain yang perlu mengunci tampilan tanpa lewat
    process_transcript_text).
    """
    await ws_manager.broadcast(call_id, {
        "type": "transcript_interim" if not is_final else "transcript_live_final",
        "call_id": call_id,
        "speaker": speaker,
        "text": text,
    })


async def process_transcript_text(call_id: str, speaker: str, text: str):
    """
    Bagian ini dipisah dari process_audio_segment supaya bisa dipanggil
    langsung dengan teks (mis. dari script testing) tanpa perlu audio.
    """
    # PENTING: kb_search_engine.search() (TF-IDF + cosine similarity) dan
    # db.transcript_add() (buka koneksi SQLite + INSERT + commit/fsync)
    # SAMA-SAMA blocking/sinkron. Kalau dipanggil langsung di sini (event
    # loop asyncio), keduanya mem-block event loop -- ini PERSIS pola bug
    # yang sama yang sebelumnya bikin faster-whisper & sherpa-onnx harus
    # dipindah ke run_in_executor (lihat komentar di process_audio_segment
    # & ari_client._process_live_audio): event loop yang sibuk bikin
    # ping/pong WebSocket ke client (panel Epic) telat, sehingga koneksi
    # dianggap putus dan client reconnect terus-menerus selama panggilan
    # berlangsung. Dua panggilan ini kelewatan waktu itu -- dibungkus di
    # sini supaya event loop tetap responsif.
    loop = asyncio.get_event_loop()

    # Sesuai PRD Bab 7.4: KB search idealnya hanya dipicu oleh ucapan
    # pelanggan, bukan ucapan agent sendiri.
    kb_results = []
    if speaker == "customer":
        kb_results = await loop.run_in_executor(None, kb_search_engine.search, text)

    row = await loop.run_in_executor(
        None,
        lambda: db.transcript_add(
            call_id=call_id,
            speaker=speaker,
            text=text,
            kb_suggested_ids=[r["id"] for r in kb_results],
        ),
    )

    await ws_manager.broadcast(call_id, {
        "type": "transcript",
        "call_id": call_id,
        "speaker": speaker,
        "text": text,
        "ts": row["id"],
    })

    if kb_results:
        await ws_manager.broadcast(call_id, {
            "type": "kb_suggestions",
            "call_id": call_id,
            "trigger_text": text,
            "suggestions": kb_results,
        })

    return {"transcript": row, "kb_suggestions": kb_results}
