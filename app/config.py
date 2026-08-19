"""
Konfigurasi terpusat, dibaca dari environment variables (lihat .env.example).
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    # ARI
    ARI_BASE_URL: str = os.getenv("ARI_BASE_URL", "http://127.0.0.1:8088")
    ARI_WS_URL: str = os.getenv("ARI_WS_URL", "ws://127.0.0.1:8088")
    ARI_USERNAME: str = os.getenv("ARI_USERNAME", "stt_kb_app")
    ARI_PASSWORD: str = os.getenv("ARI_PASSWORD", "change_me")
    ARI_APP_NAME: str = os.getenv("ARI_APP_NAME", "stt-kb")
    # Daftar extension agent yang mau disadap langsung (bukan lewat Queue/IVR).
    # Isi dipisah koma, contoh: "201,202,203,204"
    AGENT_EXTENSIONS: list[str] = [
        e.strip() for e in os.getenv("AGENT_EXTENSIONS", "").split(",") if e.strip()
    ]
    AGENT_CHANNEL_TECH: str = os.getenv("AGENT_CHANNEL_TECH", "PJSIP")

    # Media / RTP capture
    MEDIA_HOST: str = os.getenv("MEDIA_HOST", "127.0.0.1")
    MEDIA_PORT_RANGE_START: int = int(os.getenv("MEDIA_PORT_RANGE_START", "40000"))
    MEDIA_PORT_RANGE_END: int = int(os.getenv("MEDIA_PORT_RANGE_END", "40100"))
    AUDIO_SAMPLE_RATE: int = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))

    # Segmentasi VAD -- makin kecil, makin cepat terasa "real-time" (segmen
    # dikirim ke STT lebih sering), tapi konteks tiap segmen jadi lebih
    # pendek (bisa sedikit menurunkan akurasi kalau kalimatnya kepotong
    # di tengah). Nilai default di bawah dipilih untuk terasa responsif
    # tanpa terlalu sering memotong kalimat.
    SEGMENT_SILENCE_MS: int = int(os.getenv("SEGMENT_SILENCE_MS", "300"))  # jeda diam dianggap akhir ucapan
    SEGMENT_MAX_SECONDS: float = float(os.getenv("SEGMENT_MAX_SECONDS", "5.0"))  # batas atas paksa per segmen

    # Interim/live transcript: selagi masih ngomong (belum ada jeda diam),
    # kirim "cicilan" audio tiap INTERIM_FLUSH_MS untuk ditranskripsi sebagai
    # teks SEMENTARA (bisa berubah), supaya kerasa langsung muncul alih-alih
    # nunggu satu kalimat penuh selesai. INTERIM_WINDOW_SECONDS membatasi
    # jumlah audio yang ditranskripsi ulang tiap kali (ambil beberapa detik
    # terakhir saja) supaya beban CPU tidak makin berat kalau orangnya
    # ngomong panjang tanpa jeda.
    INTERIM_FLUSH_MS: int = int(os.getenv("INTERIM_FLUSH_MS", "1000"))
    INTERIM_WINDOW_SECONDS: float = float(os.getenv("INTERIM_WINDOW_SECONDS", "3.0"))

    # --- sherpa-onnx (live/instan preview, streaming asli) ---
    # Folder berisi encoder.onnx, decoder.onnx, joiner.onnx, tokens.txt
    # (rename file hasil download sesuai nama itu persis -- lihat
    # sherpa_engine.py untuk link download & instruksi lengkap)
    SHERPA_MODEL_DIR: str = os.getenv("SHERPA_MODEL_DIR", "./models/sherpa-id-streaming")
    SHERPA_ENABLED: bool = os.getenv("SHERPA_ENABLED", "true").lower() == "true"
    SHERPA_NUM_THREADS: int = int(os.getenv("SHERPA_NUM_THREADS", "2"))

    # STT (faster-whisper)
    STT_MODEL_SIZE: str = os.getenv("STT_MODEL_SIZE", "large-v3")
    STT_DEVICE: str = os.getenv("STT_DEVICE", "cpu")
    STT_COMPUTE_TYPE: str = os.getenv("STT_COMPUTE_TYPE", "int8")
    STT_LANGUAGE: str = os.getenv("STT_LANGUAGE", "id")
    # 0 = biarkan CTranslate2 pilih otomatis (biasanya = jumlah core CPU).
    # Isi manual kalau mau batasi (mis. sisain 1 core buat proses lain).
    STT_CPU_THREADS: int = int(os.getenv("STT_CPU_THREADS", "0"))
    # >1 supaya transkrip interim & final bisa jalan BERSAMAAN (lihat
    # penjelasan lengkap di stt_engine.py). Tiap worker tambahan pakai RAM
    # lebih (proporsional ke ukuran model), 2 biasanya cukup untuk pola
    # pemakaian kita (1 interim + 1 final berbarengan per panggilan aktif).
    STT_NUM_WORKERS: int = int(os.getenv("STT_NUM_WORKERS", "2"))

    # KB search
    KB_TOP_K: int = int(os.getenv("KB_TOP_K", "5"))
    KB_MIN_SCORE: float = float(os.getenv("KB_MIN_SCORE", "0.05"))

    # DB
    DB_PATH: str = os.getenv("DB_PATH", "./data/app.db")

    # App
    APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
    APP_PORT: int = int(os.getenv("APP_PORT", "8000"))


settings = Settings()
