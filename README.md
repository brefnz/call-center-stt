# Real-Time STT & Knowledge Base Assist untuk Call Center

Prototype/skeleton kode sesuai PRD (`PRD_STT_KB_CallCenter.docx`): menyadap audio
panggilan yang sedang berlangsung di **FreePBX v17 / Asterisk v20.20.1**, mentranskripsi
secara real-time pakai **faster-whisper** (open source, gratis), lalu mencari
**Knowledge Base** yang relevan dan menampilkannya lewat WebSocket ke aplikasi agent
(**Epic**).

> Status: prototype/reference implementation untuk validasi arsitektur & pilot,
> BUKAN kode production-hardened. Lihat bagian "Batasan & yang perlu disesuaikan"
> di bawah sebelum dipakai di lingkungan produksi.

## Arsitektur singkat

```
Panggilan (FreePBX/Asterisk 20.20.1)
   -> Stasis app "stt-kb" (ARI) memasang Snoop channel (spy in/out => customer/agent terpisah)
   -> ARI externalMedia channel mem-fork tiap arah sebagai RTP (slin16) ke server ini
   -> RTPReceiver (audio_capture.py) terima RTP + VAD (webrtcvad) potong per ucapan
   -> STT Engine (faster-whisper) transkripsi tiap segmen
   -> KB Search Engine (TF-IDF) cari artikel relevan (hanya dipicu ucapan CUSTOMER)
   -> Simpan log (SQLite: kb_articles, call_transcripts)
   -> Broadcast hasil ke Epic lewat WebSocket /ws/{call_id}
```

Detail perbandingan opsi (SIPREC vs ARI vs AudioSocket) dan justifikasi ada di PRD Bab 7.

## Struktur folder

```
app/
  main.py            entry point FastAPI
  config.py          semua konfigurasi (baca dari .env)
  db.py              layer SQLite (kb_articles, call_transcripts)
  kb_search.py        KB search engine (TF-IDF, gampang diganti embedding/vector)
  stt_engine.py       wrapper faster-whisper
  audio_capture.py    penerima RTP + segmentasi VAD
  ari_client.py        koneksi ke Asterisk ARI (bikin snoop + externalMedia)
  pipeline.py          penyambung semua komponen (STT -> KB -> DB -> WS)
  ws_manager.py         manajer koneksi WebSocket per call_id
  routers/
    kb.py               REST API admin KB (CRUD + search)
    calls.py             WebSocket /ws/{call_id} + endpoint testing manual
scripts/
  seed_kb.py                isi contoh data KB
  test_pipeline_text.py     testing KB search TANPA audio/Asterisk (paling cepat)
  test_pipeline_offline.py  testing STT+KB dari file .wav TANPA Asterisk
  demo_epic_panel.html       halaman statis simulasi tampilan panel di Epic
asterisk_config/
  ari.conf.sample                    contoh config ARI user
  http.conf.sample                    contoh config HTTP server Asterisk
  extensions_ari_sample.conf          contoh dialplan hook ke Stasis app kita
```

## Cara menjalankan (development / testing tanpa Asterisk dulu)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# (opsional dulu) edit .env kalau sudah mau sambung ke Asterisk asli

# 1) isi contoh data KB
python -m scripts.seed_kb

# 2) tes cepat KB search TANPA perlu model STT / internet
python -m scripts.test_pipeline_text "kenapa tagihan saya belum masuk ya?"

# 3) jalankan server (ARI listener akan retry-connect di background;
#    kalau Asterisk belum ada, server tetap jalan normal untuk testing manual)
uvicorn app.main:app --reload
```

Buka `http://localhost:8000/docs` untuk Swagger UI (coba endpoint KB & testing manual).

Untuk simulasi tampilan di sisi agent, buka `scripts/demo_epic_panel.html` di browser,
lalu di endpoint `/docs` panggil `POST /calls/transcript-event` dengan body:

```json
{ "call_id": "demo-call-1", "speaker": "customer", "text": "saya mau tanya soal tagihan yang belum masuk" }
```

Hasil transkrip & saran KB akan langsung muncul real-time di halaman demo tsb.

## Cara tes STT dari file audio (tanpa Asterisk)

```bash
# audio harus mono, 16-bit PCM (convert dulu pakai ffmpeg kalau perlu):
ffmpeg -i rekaman.mp3 -ac 1 -ar 16000 -sample_fmt s16 rekaman.wav

python -m scripts.test_pipeline_offline rekaman.wav
```

Percobaan pertama akan mengunduh model faster-whisper (butuh internet) sesuai
`STT_MODEL_SIZE` di `.env` (default `large-v3`). Untuk laptop/CPU tanpa GPU,
disarankan turunkan sementara ke `STT_MODEL_SIZE=medium` atau `small` supaya
lebih ringan saat development, lalu naikkan lagi ke `large-v3` di server GPU.

## Cara menyambungkan ke Asterisk/FreePBX asli

1. Pastikan `http.conf` & `ari.conf` sesuai contoh di `asterisk_config/` (ARI +
   HTTP server aktif). Samakan `ARI_USERNAME` / `ARI_PASSWORD` dengan `.env`.
2. Tambahkan dialplan hook seperti `asterisk_config/extensions_ari_sample.conf`
   ke context yang sesuai kebutuhan Anda (mis. sebelum masuk Queue agent).
3. Pastikan port range `MEDIA_PORT_RANGE_START`-`MEDIA_PORT_RANGE_END` di `.env`
   terbuka di firewall antara Asterisk dan server aplikasi ini (arah RTP masuk).
4. Jalankan `uvicorn app.main:app` di server yang bisa diakses Asterisk lewat
   `MEDIA_HOST`. Cek log: harus muncul "Terhubung ke ARI, app=stt-kb".
5. Lakukan panggilan uji -> pantau log server, transkrip harusnya mulai muncul
   per beberapa detik, dan cek `GET /calls/{call_id}/transcripts`.

## Batasan & yang perlu disesuaikan sebelum production

- **Placeholder dialplan**: `extensions_ari_sample.conf` masih contoh generik -
  perlu disesuaikan dengan struktur dialplan FreePBX yang sesungguhnya (Inbound
  Route / Queue / Ring Group Anda), idealnya dikerjakan bareng tim telephony.
- **KB search masih TF-IDF** (bukan semantic/embedding search) - cukup untuk
  desain sementara sesuai PRD, tapi disarankan upgrade ke embedding model +
  vector index kalau volume artikel KB sudah besar.
- **Autentikasi/keamanan**: endpoint REST & WebSocket di prototype ini belum
  diberi autentikasi - wajib ditambahkan (API key/JWT) sebelum dipakai nyata,
  apalagi karena berisi transkrip percakapan pelanggan.
- **Penyimpanan audio**: prototype ini hanya memproses audio in-memory per
  segmen (tidak menyimpan file audio mentah) - tambahkan kebijakan retensi
  eksplisit kalau ke depan perlu menyimpan rekaman untuk QA.
- **Scaling**: satu proses `STTEngine` memuat satu model di memori dengan lock
  (supaya thread-safe) - untuk banyak panggilan bersamaan, pertimbangkan
  menjalankan beberapa worker STT terpisah (mis. lewat queue/Celery) atau
  pindah ke GPU (`STT_DEVICE=cuda`).
- **Migrasi KB ke CRM**: skema `kb_articles` di `db.py` sengaja dibuat generik
  (title/content/tags/category) supaya gampang dipetakan saat migrasi ke CRM
  sesuai roadmap fase 3 di PRD.
