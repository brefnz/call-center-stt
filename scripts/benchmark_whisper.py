"""
Benchmark isolasi -- jalanin ini TERPISAH dari server (gak ada panggilan
aktif sama sekali), biar dapat angka RTF yang BERSIH tanpa gangguan
antrian/kontensi dari request lain. Tujuannya misahin dua kemungkinan:

  A. Kalau single-call di sini AJA sudah lambat (RTF > 1) -> masalahnya di
     konfigurasi/CPU (cpu_threads, compute_type, atau CPU-nya emang berat
     buat model ini) -- bukan soal antrian.
  B. Kalau single-call di sini CEPAT (RTF jauh < 1) tapi pas dipakai live
     lambat -> masalahnya di ANTRIAN (num_workers=2 kebanjiran request
     dari feed()+finalize() dua speaker leg sekaligus, request numpuk
     nunggu giliran worker kosong).

Cara pakai:
    python scripts/benchmark_whisper.py
"""
import sys
import time

sys.path.insert(0, ".")

import numpy as np

from app.config import settings
from app.stt_engine import get_stt_engine


def make_synthetic_audio(seconds: float, sample_rate: int = 16000) -> np.ndarray:
    """Audio sintetis (bukan diam total, ada sedikit noise) -- cukup buat
    ukur kecepatan komputasi murni, bukan buat cek akurasi teks."""
    rng = np.random.default_rng(42)
    t = np.linspace(0, seconds, int(seconds * sample_rate), endpoint=False)
    tone = 0.05 * np.sin(2 * np.pi * 200 * t)  # nada rendah pelan
    noise = 0.01 * rng.standard_normal(len(t))
    return (tone + noise).astype(np.float32)


def run_single_call_benchmark():
    print(f"Config: STT_MODEL_SIZE={settings.STT_MODEL_SIZE} "
          f"STT_DEVICE={settings.STT_DEVICE} STT_COMPUTE_TYPE={settings.STT_COMPUTE_TYPE} "
          f"STT_CPU_THREADS={settings.STT_CPU_THREADS} STT_NUM_WORKERS={settings.STT_NUM_WORKERS}")
    print("Loading model (sekali, di luar hitungan waktu)...")
    engine = get_stt_engine()

    print("\n--- Warm-up (hasil pertama biasanya lebih lambat, dibuang dari hitungan) ---")
    warmup_audio = make_synthetic_audio(2.0)
    engine._raw_transcribe(warmup_audio, beam_size=1)

    print("\n--- Benchmark single-call (TANPA request lain berbarengan) ---")
    print("--- Rentang durasi dilebarkan (termasuk yang sangat pendek) untuk ---")
    print("--- membuktikan apakah ada fixed-cost per panggilan atau tidak ---")
    for seconds, beam in [(0.5, 1), (1.0, 1), (2.0, 1), (3.0, 1), (4.0, 3), (4.0, 5), (10.0, 1), (20.0, 1)]:
        audio = make_synthetic_audio(seconds)
        start = time.perf_counter()
        engine._raw_transcribe(audio, beam_size=beam)
        elapsed = time.perf_counter() - start
        rtf = elapsed / seconds
        verdict = "OK (lebih cepat dari real-time)" if rtf < 1.0 else "LAMBAT (lebih lambat dari real-time)"
        print(f"  audio={seconds:.1f}s beam={beam} -> inference={elapsed:.3f}s RTF={rtf:.2f}x [{verdict}]")

    print("\nKalau inference-nya FLAT/sama aja di semua baris (gak peduli audio")
    print("0.5s atau 20s) -> itu BUKTI ada fixed-cost per panggilan (kemungkinan")
    print("besar Whisper encoder selalu proses window 30 detik penuh, gak peduli")
    print("real durasi audionya). Kalau inference-nya naik proporsional sama")
    print("durasi audio -> berarti bukan itu masalahnya, perlu dicari penyebab lain.")


if __name__ == "__main__":
    run_single_call_benchmark()
