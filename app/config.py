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
    # FIX: HARUS 16000, bukan 8000 -- externalMedia di ari_client.py minta
    # format="slin16" ke Asterisk, dan di penamaan Asterisk "slin16" itu
    # artinya 16kHz (bukan cuma "16-bit"). Audio yang beneran mengalir
    # SELALU 16000 Hz selama format itu gak diubah. Kalau nilai ini gak
    # cocok, audio_capture.py salah ngitung durasi frame VAD (frame
    # dianggap 20ms padahal cuma ~10ms audio asli) -- akibatnya VAD
    # mendeteksi "akhir ucapan" ~2x lebih cepat dari SEGMENT_SILENCE_MS/
    # SEGMENT_MAX_SECONDS yang di-set, yang bikin StreamingSession keseringan
    # di-reset (finalize()) sebelum sempat ngumpulin cukup audio buat
    # ngirim preview live pertama -- gejalanya: transkrip final tetap
    # muncul, tapi preview/live gak pernah nongol. Kalau nanti BENERAN mau
    # pindah ke narrowband 8kHz, ubah JUGA format="slin16" jadi "slin" di
    # ari_client.py (_setup_capture_for_channel) -- dua-duanya harus jalan
    # bareng, gak cukup ubah salah satu.
    AUDIO_SAMPLE_RATE: int = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))

    # Segmentasi VAD -- makin kecil, makin cepat terasa "real-time" (segmen
    # dikirim ke STT lebih sering), tapi konteks tiap segmen jadi lebih
    # pendek (bisa sedikit menurunkan akurasi kalau kalimatnya kepotong
    # di tengah). Nilai default di bawah dipilih untuk terasa responsif
    # tanpa terlalu sering memotong kalimat.
    SEGMENT_SILENCE_MS: int = int(os.getenv("SEGMENT_SILENCE_MS", "300"))  # jeda diam dianggap akhir ucapan
    SEGMENT_MAX_SECONDS: float = float(os.getenv("SEGMENT_MAX_SECONDS", "5.0"))  # batas atas paksa per segmen

    # Level agresivitas webrtcvad, 0-3. Makin tinggi = makin ketat nge-
    # filter "ini bukan ucapan" (bagus untuk kurangi halusinasi dari
    # noise/dengung line telepon), TAPI makin gampang ikut motong
    # konsonan pelan di awal/akhir kata (mis. "s", "f", "t") yang bikin
    # STT salah tebak kata. Kalau transkrip sering salah padahal audio
    # (cek debug_audio/*.wav) kedengaran jelas, coba turunkan ke 2 dulu.
    VAD_AGGRESSIVENESS: int = int(os.getenv("VAD_AGGRESSIVENESS", "3"))

    # STT streaming (preview real-time) -- lihat StreamingSession di
    # stt_engine.py. Ini menggantikan sherpa-onnx: SATU model faster-
    # whisper dipakai baik untuk preview (re-run tiap
    # STT_STREAM_CHUNK_SECONDS detik, beam_size=1/greedy) MAUPUN final
    # (sekali di akhir ucapan, beam_size=STT_BEAM_SIZE). Makin kecil nilai
    # ini, makin sering preview di-update (makin "real-time" kerasanya),
    # tapi makin sering juga model dipanggil ulang (beban CPU naik).
    STT_STREAM_CHUNK_SECONDS: float = float(os.getenv("STT_STREAM_CHUNK_SECONDS", "1"))

    # FIX: batas darurat panjang buffer PREVIEW di StreamingSession.feed().
    # Beda dari SEGMENT_MAX_SECONDS (itu batas audio FINAL yang disimpan ke
    # DB, dikontrol VAD di audio_capture.py) -- ini khusus buat buffer
    # internal preview live. Sebelumnya buffer ini TIDAK dibatasi sama
    # sekali: kalau speaker ngomong panjang tanpa jeda, tiap
    # STT_STREAM_CHUNK_SECONDS whisper harus re-transcribe SELURUH audio
    # dari awal ucapan, makin lama makin berat (biaya tumbuh gak
    # terkendali). Sekarang buffer preview dipotong, cuma nyimpen
    # STT_STREAM_MAX_BUFFER_SECONDS detik TERAKHIR -- preview mungkin
    # "lupa" kata-kata di awal kalimat yang sangat panjang, tapi biaya
    # re-transcribe-nya jadi konstan (gak terus membengkak). Ini gak
    # mempengaruhi hasil FINAL (itu tetap dari finalize(), pakai audio
    # utuh dari VAD).
    STT_STREAM_MAX_BUFFER_SECONDS: float = float(os.getenv("STT_STREAM_MAX_BUFFER_SECONDS", "8.0"))

    # STT (faster-whisper)
    STT_MODEL_SIZE: str = os.getenv("STT_MODEL_SIZE", "small")
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
    # beam_size untuk transkrip FINAL (yang dipakai KB search & disimpan
    # ke DB) -- default faster-whisper adalah 5. Di CPU (apalagi tanpa
    # GPU), turunkan ke 2-3 untuk latensi jauh lebih baik dengan sedikit
    # trade-off akurasi. 1 = greedy decoding, tercepat tapi paling kurang
    # akurat -- biasanya dipakai khusus untuk interim/preview, bukan final.
    STT_BEAM_SIZE: int = int(os.getenv("STT_BEAM_SIZE", "3"))

    # KB search
    KB_TOP_K: int = int(os.getenv("KB_TOP_K", "5"))
    KB_MIN_SCORE: float = float(os.getenv("KB_MIN_SCORE", "0.05"))

    # DB
    DB_PATH: str = os.getenv("DB_PATH", "./data/app.db")

    # App
    APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
    APP_PORT: int = int(os.getenv("APP_PORT", "8000"))


settings = Settings()
