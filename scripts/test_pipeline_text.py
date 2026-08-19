"""
Testing cepat untuk KB search + pipeline TANPA perlu Asterisk maupun model
STT (jadi bisa dites langsung tanpa koneksi internet/GPU). Berguna untuk
memvalidasi kualitas pencarian KB sebelum menyambungkan audio asli.

Jalankan:
    python -m scripts.test_pipeline_text "bagaimana cara reset password saya?"
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import db  # noqa: E402
from app.pipeline import process_transcript_text  # noqa: E402


async def main(text: str):
    db.init_db()
    result = await process_transcript_text(call_id="demo-call-1", speaker="customer", text=text)
    print("\nTeks pelanggan:", text)
    print("\nSaran KB:")
    if not result["kb_suggestions"]:
        print("  (tidak ada artikel relevan ditemukan / skor di bawah threshold)")
    for s in result["kb_suggestions"]:
        print(f"  - [{s['score']}] {s['title']}")


if __name__ == "__main__":
    text = " ".join(sys.argv[1:]) or "saya mau tanya kenapa tagihan saya belum masuk bulan ini"
    asyncio.run(main(text))
