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
from app.kb_search import kb_search_engine
from app.stt_engine import get_stt_engine
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
    safe_call_id = call_id.replace(":", "_").replace("/", "_")
    fname = f"{DEBUG_WAV_DIR}/{safe_call_id}_{speaker}_{len(pcm_bytes)}.wav"
    with wave.open(fname, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    logger.info("DEBUG: segmen audio disimpan ke %s", fname)


async def process_audio_segment(call_id: str, speaker: str, pcm_bytes: bytes):
    """Dipanggil setiap satu segmen ucapan (hasil VAD) selesai direkam."""
    if DEBUG_SAVE_WAV:
        try:
            _save_debug_wav(call_id, speaker, pcm_bytes, sample_rate=16000)
        except Exception:
            logger.exception("DEBUG: gagal simpan wav")

    stt = get_stt_engine()
    # PENTING: transcribe_pcm16 itu CPU-bound & blocking (menjalankan model
    # Whisper). Kalau dipanggil langsung di sini, dia akan mem-block SELURUH
    # event loop asyncio -- termasuk socket UDP yang lagi nerima paket RTP
    # audio BARU secara bersamaan. Akibatnya paket yang datang PAS proses
    # transkripsi berjalan bisa didrop di level OS (buffer socket penuh),
    # yang kedengar sebagai suara kresek/putus-putus tepat pas ada yang
    # ngomong terus-menerus. run_in_executor menjalankannya di thread
    # terpisah supaya event loop tetap bebas menerima audio.
    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, stt.transcribe_pcm16, pcm_bytes, 16000)
    text = text.strip()
    if not text:
        return

    logger.info("[%s][%s] %s", call_id, speaker, text)

    await process_transcript_text(call_id, speaker, text)


async def broadcast_live_transcript(call_id: str, speaker: str, text: str, is_final: bool):
    """
    Broadcast hasil dari Vosk (preview instan) -- BEDA dari
    process_transcript_text yang menyimpan ke DB dan memicu KB search.
    Fungsi ini murni untuk tampilan real-time di sisi Epic/CRM, dijalankan
    per potongan kecil audio yang terus mengalir (lihat vosk_engine.py &
    ari_client.py). Vosk sengaja TIDAK dipakai untuk memicu KB search --
    akurasinya lebih rendah dibanding faster-whisper, jadi keputusan
    "ucapan apa yang resmi dikatakan customer" tetap dari
    process_transcript_text (dipanggil dari process_audio_segment).

    is_final=False -> Vosk masih menganggap ucapan berlangsung (partial
        result, bisa berubah di panggilan berikutnya).
    is_final=True  -> endpointing internal Vosk mendeteksi ucapan selesai
        (versi akhir MENURUT VOSK -- tetap bukan yang dipakai KB search,
        cuma dikunci tampilannya di UI supaya tidak "loncat-loncat" lagi).
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
