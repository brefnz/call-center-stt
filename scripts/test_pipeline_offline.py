"""
Simulasi end-to-end (STT -> KB search -> log) dari file audio .wav, TANPA
perlu koneksi ke Asterisk. Berguna untuk uji coba akurasi STT dan integrasi
KB sebelum sistem disambungkan ke FreePBX asli.

Syarat file audio: WAV, mono, 16-bit PCM (sample rate bebas, disarankan 16kHz).
Contoh convert dari format lain pakai ffmpeg:
    ffmpeg -i rekaman.mp3 -ac 1 -ar 16000 -sample_fmt s16 rekaman.wav

Jalankan:
    python -m scripts.test_pipeline_offline path/ke/rekaman.wav
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import db  # noqa: E402
from app.pipeline import process_transcript_text  # noqa: E402
from app.stt_engine import get_stt_engine  # noqa: E402


async def main(wav_path: str):
    db.init_db()
    print(f"Memuat model faster-whisper (pertama kali akan mengunduh model, butuh internet) ...")
    stt = get_stt_engine()

    print(f"Mentranskripsi {wav_path} ...")
    text = stt.transcribe_wav_file(wav_path)
    print(f"\nHasil transkrip:\n  {text}")

    if not text:
        print("Tidak ada teks terdeteksi, berhenti.")
        return

    result = await process_transcript_text(call_id="demo-call-offline", speaker="customer", text=text)
    print("\nSaran KB:")
    if not result["kb_suggestions"]:
        print("  (tidak ada artikel relevan ditemukan / skor di bawah threshold)")
    for s in result["kb_suggestions"]:
        print(f"  - [{s['score']}] {s['title']}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Penggunaan: python -m scripts.test_pipeline_offline path/ke/rekaman.wav")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
