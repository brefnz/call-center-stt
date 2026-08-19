"""
sherpa-onnx streaming STT engine -- untuk transkrip LIVE/instan (kata
muncul sambil diucapkan). Menggantikan percobaan sebelumnya pakai Vosk
(dibatalkan karena Vosk TIDAK punya model Bahasa Indonesia resmi sama
sekali -- baik streaming maupun batch).

Model yang dipakai (bookbot/sherpa-onnx-pruned-transducer-stateless7-
streaming-id) memprediksi FONEM, bukan kata langsung -- lihat
phoneme_to_text.py untuk konversinya ke tulisan Bahasa Indonesia.

PEMBAGIAN PERAN (sama seperti rencana semula, cuma ganti mesinnya):
  - sherpa-onnx (file ini) -> preview instan yang ditampilkan real-time ke
                              agent. Approximate (karena lewat konversi
                              fonem), tapi delay-nya genuinely rendah.
  - faster-whisper           -> tetap "sumber kebenaran": disimpan ke DB,
    (stt_engine.py)             memicu KB search. Delay beberapa detik di
                              sini tidak masalah karena bukan yang dibaca
                              real-time oleh mata -- yang penting akurat.
"""
import logging
import os

import numpy as np
import sherpa_onnx

from app.config import settings
from app.phoneme_to_text import phonemes_to_indonesian_text

logger = logging.getLogger("sherpa_engine")

_recognizer: sherpa_onnx.OnlineRecognizer | None = None


def _get_recognizer() -> sherpa_onnx.OnlineRecognizer:
    global _recognizer
    if _recognizer is None:
        model_dir = settings.SHERPA_MODEL_DIR
        paths = {
            "tokens": os.path.join(model_dir, "tokens.txt"),
            "encoder": os.path.join(model_dir, "encoder.onnx"),
            "decoder": os.path.join(model_dir, "decoder.onnx"),
            "joiner": os.path.join(model_dir, "joiner.onnx"),
        }
        missing = [name for name, p in paths.items() if not os.path.isfile(p)]
        if missing:
            raise RuntimeError(
                f"File model sherpa-onnx tidak ditemukan: {missing} di folder "
                f"{model_dir}. Download dari "
                "https://huggingface.co/bookbot/sherpa-onnx-pruned-transducer-stateless7-streaming-id "
                "(pakai versi .int8.onnx yang lebih kecil), lalu RENAME jadi "
                "persis: encoder.onnx, decoder.onnx, joiner.onnx, tokens.txt "
                "-- taruh semua di satu folder itu, set SHERPA_MODEL_DIR di .env."
            )
        logger.info("Loading model sherpa-onnx dari %s ...", model_dir)
        _recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=paths["tokens"],
            encoder=paths["encoder"],
            decoder=paths["decoder"],
            joiner=paths["joiner"],
            num_threads=settings.SHERPA_NUM_THREADS,
            sample_rate=16000,
            feature_dim=80,
            enable_endpoint_detection=True,
            decoding_method="greedy_search",
            provider="cpu",
        )
    return _recognizer


class LiveRecognizer:
    """
    Satu instance per leg audio (per speaker per panggilan) -- sherpa-onnx
    OnlineStream itu stateful, harus tetap sama dari awal sampai akhir
    satu aliran audio yang sama (mirip Vosk, beda dari faster-whisper yang
    stateless per panggilan .transcribe()).
    """

    def __init__(self, sample_rate: int = 16000):
        self._recognizer = _get_recognizer()
        self._stream = self._recognizer.create_stream()
        self._sample_rate = sample_rate
        self._last_partial = ""

    def feed(self, pcm_bytes: bytes) -> tuple[str | None, str | None]:
        """
        Kasih makan potongan audio PCM16 mono. Return (partial_text,
        final_text) -- salah satu (atau keduanya) bisa None kalau tidak
        ada perubahan yang perlu dilaporkan. Teks yang dikembalikan sudah
        dalam bentuk perkiraan tulisan Indonesia (bukan fonem mentah).
        """
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        self._stream.accept_waveform(self._sample_rate, samples)

        while self._recognizer.is_ready(self._stream):
            self._recognizer.decode_stream(self._stream)

        raw_result = self._recognizer.get_result(self._stream)
        is_endpoint = self._recognizer.is_endpoint(self._stream)

        partial_out = None
        final_out = None

        if is_endpoint:
            text = phonemes_to_indonesian_text(raw_result)
            if text:
                final_out = text
            self._recognizer.reset(self._stream)
            self._last_partial = ""
        else:
            text = phonemes_to_indonesian_text(raw_result)
            if text and text != self._last_partial:
                partial_out = text
                self._last_partial = text

        return partial_out, final_out

    def close(self) -> str | None:
        """Panggil pas panggilan selesai -- flush sisa audio yang mungkin
        belum sempat dilaporkan sebagai final."""
        try:
            self._stream.input_finished()
            while self._recognizer.is_ready(self._stream):
                self._recognizer.decode_stream(self._stream)
            raw_result = self._recognizer.get_result(self._stream)
            return phonemes_to_indonesian_text(raw_result) or None
        except Exception:
            logger.exception("Gagal flush hasil akhir sherpa-onnx saat close()")
            return None
