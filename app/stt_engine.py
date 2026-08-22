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
import time

import numpy as np
from faster_whisper import WhisperModel

from app.config import settings

logger = logging.getLogger("stt_engine")

# Frasa yang sudah dikenal luas sebagai "halusinasi" khas Whisper saat
# dikasih audio nyaris diam/noise (dilatih dari banyak data YouTube, jadi
# suka ngarang kalimat penutup video kayak gini). Dicocokkan longgar
# (lowercase, tanda baca dibuang) supaya varian kecil tetap ke-tangkep.
_HALLUCINATION_PHRASES = [
    "terima kasih telah menonton",
    "terima kasih kerana menonton",
    "terima kasih karena telah menonton",
    "terima kasih sudah menonton",
    "jangan lupa like dan subscribe",
    "sampai jumpa di video selanjutnya",
    "terima kasih",  # kalau ini SATU-SATUNYA isi segmen (lihat _looks_like_hallucination)
]


def _looks_like_hallucination(text: str) -> bool:
    """
    Cek longgar apakah teks ini kemungkinan besar halusinasi Whisper,
    bukan ucapan customer/agent beneran. Sengaja hanya cocok kalau teksnya
    PENDEK dan MIRIP PERSIS salah satu frasa umum ini -- supaya kalimat
    panjang yang KEBETULAN mengandung kata "terima kasih" (mis. "terima
    kasih pak, saya mau tanya soal tagihan") tetap lolos apa adanya.
    """
    normalized = text.strip().lower().rstrip(".!?, ")
    if len(normalized) > 40:
        return False  # terlalu panjang untuk jadi false positive dari frasa pendek di atas
    return normalized in _HALLUCINATION_PHRASES


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

    def _raw_transcribe(self, audio_np: np.ndarray, beam_size: int, sample_rate: int = 16000) -> str:
        """
        Inti transkripsi, terima audio yang SUDAH dalam bentuk float32
        [-1, 1] (dipakai bersama oleh transcribe_pcm16 dan StreamingSession
        supaya logic hallucination-filtering tidak terduplikasi).

        FIX: sebelumnya ada blok pengukuran waktu (time.perf_counter() +
        logger.info("WHISPER: ...")) yang DITARUH SETELAH `return` di
        fungsi ini -- jadi itu kode MATI, gak pernah kejalanin sama
        sekali. Selama ini gak ada angka pasti soal seberapa lambat
        whisper beneran jalan di server. Sekarang pengukurannya beneran
        jalan (dan sekalian dihitung RTF -- real-time factor -- biar
        langsung kelihatan apakah CPU "kekejar" kecepatan bicara orang
        atau enggak: RTF < 1 berarti lebih cepat dari real-time/aman,
        RTF > 1 berarti proses lebih lambat dari lama ucapannya sendiri,
        alias makin lama makin nge-lag/numpuk).
        """
        if audio_np.size == 0:
            return ""

        start = time.perf_counter()

        segments, _info = self._model.transcribe(
            audio_np,
            language=settings.STT_LANGUAGE,
            vad_filter=False,  # VAD sudah dilakukan sebelumnya di audio_capture.py
            beam_size=beam_size,
            # PENTING (fix halusinasi "terima kasih karena telah menonton"
            # dkk): Whisper dilatih dari banyak data YouTube, jadi kalau
            # dikasih segmen yang SEBENARNYA nyaris diam/noise (VAD kita
            # kadang masih meloloskan sedikit residual noise/dengung line
            # telepon), dia suka "ngarang" kalimat penutup video seperti
            # itu -- bug yang sudah terkenal luas di komunitas Whisper,
            # bukan cuma di sini.
            #
            # condition_on_previous_text=False: jangan pakai transkrip
            # segmen SEBELUMNYA sebagai konteks. Kalau dibiarkan default
            # (True), begitu SEKALI halusinasi muncul, dia cenderung
            # "keterusan" halu di segmen-segmen berikutnya juga karena ikut
            # kekontaminasi konteks yang salah.
            condition_on_previous_text=False,
        )

        parts = []
        for seg in segments:
            text = seg.text.strip()
            if not text:
                continue
            # no_speech_prob tinggi = model sendiri menganggap segmen ini
            # KEMUNGKINAN BESAR bukan ucapan sama sekali (diam/noise) --
            # buang, jangan sampai teks ngarang ini lolos ke KB search/DB.
            if getattr(seg, "no_speech_prob", 0.0) > 0.6:
                logger.info("Buang segmen (no_speech_prob=%.2f): %r", seg.no_speech_prob, text)
                continue
            if _looks_like_hallucination(text):
                logger.info("Buang segmen (terdeteksi pola halusinasi umum): %r", text)
                continue
            parts.append(text)

        result = " ".join(parts).strip()

        elapsed = time.perf_counter() - start
        duration = audio_np.size / sample_rate
        rtf = (elapsed / duration) if duration > 0 else 0.0
        logger.info(
            "WHISPER: audio=%.2fs beam=%d inference=%.3fs RTF=%.2fx%s -> %r",
            duration, beam_size, elapsed, rtf,
            " [LEBIH LAMBAT DARI REAL-TIME]" if rtf > 1.0 else "",
            result,
        )

        return result

    def transcribe_pcm16(self, pcm_bytes: bytes, sample_rate: int = 16000, beam_size: int = 5) -> str:
        """
        Transkripsi satu segmen audio PCM 16-bit mono (hasil VAD) menjadi teks.

        beam_size lebih kecil = lebih cepat tapi sedikit kurang akurat.
        """
        if not pcm_bytes:
            return ""
        audio_np = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        return self._raw_transcribe(audio_np, beam_size=beam_size, sample_rate=sample_rate)

    def transcribe_wav_file(self, path: str) -> str:
        """Helper untuk testing offline (lihat scripts/test_pipeline_offline.py)."""
        with wave.open(path, "rb") as wf:
            assert wf.getsampwidth() == 2, "WAV harus 16-bit PCM"
            pcm_bytes = wf.readframes(wf.getnframes())
            sample_rate = wf.getframerate()
        return self.transcribe_pcm16(pcm_bytes, sample_rate)


class StreamingSession:
    """
    Transkrip real-time SATU speaker (agent ATAU customer) dalam satu
    panggilan, pakai SATU model faster-whisper -- pengganti kombinasi lama
    sherpa-onnx (preview cepat tapi cuma tebak fonem, gak akurat) +
    faster-whisper (final, tapi diproses ULANG dari nol setelah preview,
    kerja dobel).

    IDE DASARNYA (diadaptasi dari teknik "local agreement" a.k.a.
    whisper_streaming, Machacek dkk. 2023 -- disederhanakan karena kita
    SUDAH punya batas ucapan yang jelas dari VAD, beda dari paper aslinya
    yang menyasar audio panjang tanpa batas):

      1. Selama speaker masih ngomong, `feed()` dipanggil terus per frame
         kecil (~20ms). Tiap sudah kekumpul ~STT_STREAM_CHUNK_SECONDS
         detik audio BARU, seluruh buffer (dari awal ucapan ini) di-
         transkrip ulang pakai beam_size=1 (greedy, PALING cepat) --
         hasilnya dikirim sebagai preview yang terus "nyempurna"
         (mis. "halo" -> "halo selamat" -> "halo selamat pagi").
      2. Begitu VAD bilang speaker BERHENTI ngomong (akhir ucapan),
         `finalize()` dipanggil SEKALI dengan audio final dari VAD --
         transkrip ulang TERAKHIR kali pakai beam_size lebih besar
         (settings.STT_BEAM_SIZE) untuk hasil paling akurat, itulah yang
         disimpan sebagai transkrip FINAL (masuk DB + KB search).

    Kenapa ini LEBIH RINGAN dibanding sherpa+whisper terpisah, padahal
    sama-sama "transcribe berkali-kali": preview pakai beam_size=1 pada
    buffer yang pendek (durasi satu ucapan, biasanya beberapa detik --
    bukan seluruh panggilan), dan re-run cuma tiap STT_STREAM_CHUNK_SECONDS
    (bukan tiap frame 20ms kayak sherpa) -- jadi total kerja CPU-nya jauh
    lebih kecil dibanding menjalankan DUA model penuh (sherpa tiap frame +
    whisper penuh di akhir).

    FIX: sebelumnya buffer `_audio` di sini TIDAK ADA batas panjangnya
    sama sekali -- kalau speaker ngomong panjang tanpa jeda, tiap
    STT_STREAM_CHUNK_SECONDS whisper harus re-transcribe SELURUH audio
    dari awal ucapan itu, makin lama makin berat (biaya CPU per re-run
    tumbuh terus, bisa bikin proses ketinggalan jauh dari kecepatan orang
    ngomong). Sekarang buffer dipotong ke STT_STREAM_MAX_BUFFER_SECONDS
    detik TERAKHIR sebelum tiap re-run -- preview mungkin "lupa" kata di
    awal kalimat yang SANGAT panjang, tapi biaya re-transcribe-nya jadi
    konstan. Ini tidak memengaruhi hasil FINAL (finalize() tetap pakai
    audio utuh dari VAD, bukan buffer internal ini).
    """

    def __init__(self, engine: "STTEngine", sample_rate: int = 16000):
        self._engine = engine
        self._sample_rate = sample_rate
        self._audio = np.zeros(0, dtype=np.float32)
        self._samples_since_last_run = 0
        self._min_chunk_samples = int(settings.STT_STREAM_CHUNK_SECONDS * sample_rate)
        self._max_buffer_samples = int(settings.STT_STREAM_MAX_BUFFER_SECONDS * sample_rate)

    def feed(self, pcm_bytes: bytes) -> str:
        """
        Terima potongan audio PCM16 kecil (dipanggil terus selama speaker
        ngomong). Return teks preview TERBARU (menggantikan preview
        sebelumnya, BUKAN ditambahkan) kalau baru saja re-run, atau ""
        kalau belum cukup audio baru untuk re-run lagi (supaya caller
        tahu kapan HARUS broadcast update, dan kapan tidak perlu).
        """
        if not pcm_bytes:
            return ""
        chunk = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        self._audio = np.concatenate([self._audio, chunk])
        self._samples_since_last_run += len(chunk)

        if self._samples_since_last_run < self._min_chunk_samples:
            return ""
        self._samples_since_last_run = 0

        # FIX: pangkas buffer ke N detik terakhir SEBELUM re-transcribe,
        # supaya biaya greedy-decode gak terus membengkak di kalimat
        # panjang tanpa jeda (lihat catatan FIX di docstring class ini).
        if len(self._audio) > self._max_buffer_samples:
            self._audio = self._audio[-self._max_buffer_samples:]
            logger.info(
                "StreamingSession: buffer preview dipangkas ke %.1fs (kalimat panjang tanpa jeda)",
                settings.STT_STREAM_MAX_BUFFER_SECONDS,
            )

        try:
            return self._engine._raw_transcribe(self._audio, beam_size=1, sample_rate=self._sample_rate)
        except Exception:
            logger.exception("Gagal transcribe preview streaming")
            return ""

    def finalize(self, final_pcm_bytes: bytes) -> str:
        """
        Dipanggil SEKALI saat VAD mendeteksi ucapan selesai. Transkrip
        ULANG dari audio final yang dikasih VAD (BUKAN dari buffer
        internal `feed()` -- audio dari VAD itu otoritatif/lengkap, jadi
        lebih aman dipakai langsung daripada mengandalkan akumulasi
        internal yang timing-nya bisa sedikit beda), pakai beam_size lebih
        besar untuk akurasi maksimal karena ini yang disimpan sebagai
        transkrip final. Reset buffer internal untuk ucapan berikutnya.
        """
        self._audio = np.zeros(0, dtype=np.float32)
        self._samples_since_last_run = 0
        if not final_pcm_bytes:
            return ""
        return self._engine.transcribe_pcm16(
            final_pcm_bytes, sample_rate=self._sample_rate, beam_size=settings.STT_BEAM_SIZE
        )

    def close(self):
        """Dipanggil saat panggilan selesai -- tidak ada resource eksternal
        yang perlu dilepas (beda dari sherpa-onnx yang punya native handle),
        tapi disediakan untuk kompatibilitas API dengan pemanggilnya."""
        self._audio = np.zeros(0, dtype=np.float32)


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
