"""
STT Engine wrapper - faster-whisper (Whisper + CTranslate2).

Kenapa faster-whisper: open source (lisensi MIT), gratis, akurasi bagus untuk
Bahasa Indonesia (model large-v3), dan jauh lebih cepat dibanding Whisper asli.
Whisper bukan model streaming native, jadi kita pakai pola "chunked streaming":
audio dipotong per segmen ucapan (hasil VAD di audio_capture.py), lalu tiap
segmen ditranskripsi begitu selesai diucapkan (near real-time).

Catatan produksi: model di-load sekali saat startup (bukan per-request) karena
loading model itu berat. Untuk banyak panggilan bersamaan, jalankan service ini
di server dengan GPU (set STT_DEVICE=cuda) demi latensi yang stabil.
"""
import io
import logging
import threading
import wave

import numpy as np
from faster_whisper import WhisperModel

from app.config import settings

logger = logging.getLogger("stt_engine")


class STTEngine:
    def __init__(self):
        logger.info(
            "Loading faster-whisper model=%s device=%s compute_type=%s cpu_threads=%s num_workers=%s ...",
            settings.STT_MODEL_SIZE, settings.STT_DEVICE, settings.STT_COMPUTE_TYPE,
            settings.STT_CPU_THREADS, settings.STT_NUM_WORKERS,
        )
        self._model = WhisperModel(
            settings.STT_MODEL_SIZE,
            device=settings.STT_DEVICE,
            compute_type=settings.STT_COMPUTE_TYPE,
            cpu_threads=settings.STT_CPU_THREADS,
            # PENTING: num_workers>1 membuat faster-whisper/CTranslate2
            # mengelola beberapa "replika" model secara internal supaya
            # transcribe() bisa dipanggil BERSAMAAN dari beberapa thread
            # tanpa saling nunggu -- ini cara RESMI untuk transkripsi
            # konkuren, jauh lebih baik dibanding lock manual yang
            # sebelumnya bikin semua panggilan (interim maupun final)
            # antre satu-satu meskipun CPU masih punya kapasitas kosong.
            num_workers=settings.STT_NUM_WORKERS,
        )

    def transcribe_pcm16(self, pcm_bytes: bytes, sample_rate: int = 16000, beam_size: int = 5) -> str:
        """
        Transkripsi satu segmen audio PCM 16-bit mono (hasil VAD) menjadi teks.

        beam_size lebih kecil = lebih cepat tapi sedikit kurang akurat.
        Dipakai beam_size=1 (greedy, tercepat) untuk transkrip INTERIM/
        sementara (lihat pipeline.py: process_interim_audio_segment), dan
        beam_size default (5) untuk transkrip FINAL yang lebih diutamakan
        akurat karena itu yang dipakai KB search & disimpan ke DB.
        """
        if not pcm_bytes:
            return ""
        audio_np = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0

        segments, _info = self._model.transcribe(
            audio_np,
            language=settings.STT_LANGUAGE,
            vad_filter=False,  # VAD sudah dilakukan sebelumnya di audio_capture.py
            beam_size=beam_size,
        )
        text = " ".join(seg.text.strip() for seg in segments).strip()
        return text

    def transcribe_wav_file(self, path: str) -> str:
        """Helper untuk testing offline (lihat scripts/test_pipeline_offline.py)."""
        with wave.open(path, "rb") as wf:
            assert wf.getsampwidth() == 2, "WAV harus 16-bit PCM"
            pcm_bytes = wf.readframes(wf.getnframes())
            sample_rate = wf.getframerate()
        return self.transcribe_pcm16(pcm_bytes, sample_rate)


# Lazy singleton -> supaya import module ini tidak langsung men-download model
_engine: STTEngine | None = None
_engine_lock = threading.Lock()


def get_stt_engine() -> STTEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = STTEngine()
    return _engine
