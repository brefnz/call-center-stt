"""
Audio capture dari Asterisk lewat ARI externalMedia (RTP/UDP, format slin16).

Alur:
  1. Untuk tiap call, kita minta satu port UDP dari PortAllocator.
  2. ari_client.py membuat externalMedia channel di Asterisk yang diarahkan
     mengirim RTP audio ke MEDIA_HOST:port tsb (lihat app/ari_client.py).
  3. RTPReceiver di sini mendengarkan port itu, melepas header RTP (12 byte),
     lalu audio PCM mentahnya dilempar ke VAD segmenter.
  4. Begitu VAD mendeteksi akhir satu ucapan (jeda diam), segmen audio
     dikirim ke STT engine -> hasil teks -> KB search -> broadcast ke Epic.

Catatan pemisahan speaker (diarization): sesuai PRD Bab 7.4, cara utama yang
direkomendasikan adalah dual-channel capture (kanal terpisah agent vs
pelanggan), BUKAN model ML diarization. Class RTPReceiver di bawah ini
menerima parameter `speaker_label` per port/channel -> jadi cukup buat dua
RTPReceiver (satu untuk leg agent, satu untuk leg customer) yang masing-masing
mengirim ke UDP port berbeda dari Asterisk.
"""
import asyncio
import audioop
import logging
import struct
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional

import webrtcvad

from app.config import settings

logger = logging.getLogger("audio_capture")

RTP_HEADER_LEN = 12
FRAME_MS = 20  # ukuran frame VAD: 10/20/30 ms
BYTES_PER_SAMPLE = 2  # 16-bit PCM
# Dibaca dari settings (.env) supaya bisa di-tuning tanpa ubah kode --
# lihat app/config.py untuk penjelasan trade-off nilai ini.
SILENCE_FRAMES_TO_END_SEGMENT = max(1, round(settings.SEGMENT_SILENCE_MS / FRAME_MS))
MAX_SEGMENT_FRAMES = max(1, round(settings.SEGMENT_MAX_SECONDS * 1000 / FRAME_MS))
INTERIM_FLUSH_FRAMES = max(1, round(settings.INTERIM_FLUSH_MS / FRAME_MS))
INTERIM_WINDOW_FRAMES = max(1, round(settings.INTERIM_WINDOW_SECONDS * 1000 / FRAME_MS))

# Jitter buffer: RTP jalan di atas UDP, yang TIDAK menjamin urutan paket
# sampai. Kalau audio langsung disambung sesuai urutan KEDATANGAN (bukan
# sesuai sequence number aslinya), paket yang datang telat/kepotong bikin
# audio jadi acak/berisik meski isi tiap paketnya sendiri valid. Buffer ini
# menahan sebentar paket yang masuk, susun ulang berdasar sequence number,
# baru dilepas ke VAD -- correctness diprioritaskan di atas latensi sekecil
# mungkin (~beberapa puluh ms tambahan, tidak masalah untuk STT non-live-caption).
JITTER_BUFFER_SIZE = 20  # tahan sampai 20 paket (~400ms) sebelum mulai paksa lompat
JITTER_MAX_WAIT_PACKETS = 30  # kalau satu seq "hilang" > ini, anggap benar2 hilang, lanjut


def _parse_rtp_header(data: bytes):
    """
    Parse header RTP secara benar (bukan asumsi 12 byte tetap), termasuk
    CSRC list dan extension header kalau bit X di-set -- supaya payload
    audio yang diambil tidak ikut kepotong/kegeser kalau Asterisk mengirim
    header lebih panjang dari 12 byte standar.

    Return (sequence_number, payload_bytes) atau None kalau paket tidak valid.
    """
    if len(data) < RTP_HEADER_LEN:
        return None

    first_byte = data[0]
    version = (first_byte >> 6) & 0x03
    if version != 2:
        return None  # bukan RTP versi standar, kemungkinan bukan RTP sama sekali

    has_extension = bool((first_byte >> 4) & 0x01)
    csrc_count = first_byte & 0x0F

    seq_num = struct.unpack("!H", data[2:4])[0]

    offset = RTP_HEADER_LEN + (csrc_count * 4)
    if len(data) < offset:
        return None

    if has_extension:
        if len(data) < offset + 4:
            return None
        ext_len_words = struct.unpack("!H", data[offset + 2:offset + 4])[0]
        offset += 4 + (ext_len_words * 4)
        if len(data) < offset:
            return None

    return seq_num, data[offset:]


def _rtp_payload_to_pcm_le(payload: bytes) -> bytes:
    """
    Payload audio RTP (format slin16) dikirim Asterisk dalam NETWORK BYTE
    ORDER (big-endian) untuk tiap sampel 16-bit. Python/numpy/modul `wave`
    semuanya berasumsi little-endian (byte order host x86) secara default.
    Tanpa konversi ini, tiap pasang byte sampel terbaca TERBALIK -- audio
    yang sebenarnya bersih akan terdengar seperti noise/static acak
    (angka melompat liar, bukan gelombang halus). audioop.byteswap
    membalik byte order tiap sampel 16-bit (parameter kedua = lebar
    sampel dalam byte).
    """
    if len(payload) % 2 != 0:
        payload = payload[:-1]  # buang 1 byte ganjil sisa (jaga-jaga, seharusnya tak terjadi)
    return audioop.byteswap(payload, 2)


class PortAllocator:
    """Alokasi port UDP per call dari range yang dikonfigurasi di .env."""

    def __init__(self, start: int, end: int):
        self._free = set(range(start, end + 1, 2))  # step 2: sisakan slot RTCP
        self._lock = threading.Lock()

    def acquire(self) -> int:
        with self._lock:
            if not self._free:
                raise RuntimeError("Tidak ada port UDP tersedia di MEDIA_PORT_RANGE")
            port = min(self._free)
            self._free.remove(port)
            return port

    def release(self, port: int):
        with self._lock:
            self._free.add(port)


port_allocator = PortAllocator(settings.MEDIA_PORT_RANGE_START, settings.MEDIA_PORT_RANGE_END)


@dataclass
class _VADState:
    # Level 3 (paling ketat/agresif dari 0-3): kurangi noise/diam yang
    # salah kedeteksi sebagai "ada yang ngomong" -- ini penyebab utama
    # transkrip ngaco/halusinasi ("terima kasih kerana menonton" dst)
    # yang muncul dari segmen yang isinya sebenarnya bukan ucapan jelas.
    vad: webrtcvad.Vad = field(default_factory=lambda: webrtcvad.Vad(3))
    buffer: bytearray = field(default_factory=bytearray)
    speech_frames: list = field(default_factory=list)
    silence_run: int = 0
    in_speech: bool = False
    # Jitter buffer: seq_num -> payload_bytes, untuk paket yang sudah
    # diterima tapi belum dilepas ke VAD karena menunggu paket lain yang
    # seharusnya datang lebih dulu (berdasar sequence number).
    jitter_pending: dict = field(default_factory=dict)
    next_expected_seq: Optional[int] = None


class RTPAudioProtocol(asyncio.DatagramProtocol):
    """
    asyncio UDP protocol: menerima paket RTP, strip header, jalankan VAD,
    dan panggil `on_segment(pcm_bytes)` tiap kali satu segmen ucapan selesai
    (dipakai faster-whisper -- akurat, sedikit delay).

    `on_live_audio(frame_bytes)` -- kalau diisi -- dipanggil untuk SETIAP
    frame 20ms, apa adanya, terlepas dari status VAD (dipakai Vosk untuk
    preview instan/real-time; lihat vosk_engine.py).
    """

    def __init__(
        self,
        sample_rate: int,
        on_segment: Callable[[bytes], None],
        on_live_audio: Optional[Callable[[bytes], None]] = None,
    ):
        self.sample_rate = sample_rate
        self.on_segment = on_segment
        self.on_live_audio = on_live_audio
        self.frame_bytes = int(sample_rate * (FRAME_MS / 1000.0) * BYTES_PER_SAMPLE)
        self._state = _VADState()

    def datagram_received(self, data: bytes, addr):
        parsed = _parse_rtp_header(data)
        if parsed is None:
            return
        seq_num, payload = parsed
        if not payload:
            return
        payload = _rtp_payload_to_pcm_le(payload)
        self._push_jitter(seq_num, payload)

    def _push_jitter(self, seq_num: int, payload: bytes):
        """
        Simpan paket ke jitter buffer, lalu keluarkan paket-paket yang
        sudah berurutan (sesuai sequence number) ke VAD. Kalau buffer
        kepenuhan (paket yang ditunggu gak kunjung datang -- kemungkinan
        hilang), lompat maju ke paket tertua yang tersedia supaya tidak
        macet menunggu selamanya.
        """
        st = self._state

        if st.next_expected_seq is not None:
            gap = self._seq_gap(st.next_expected_seq, seq_num)
            if gap > 32768:
                # seq_num ini "di belakang" next_expected_seq (paket basi:
                # entah duplikat, entah paket yang sudah kita anggap hilang
                # dan sudah dilompati sebelumnya). Buang, jangan disimpan --
                # kalau disimpan, dia gak akan pernah cocok dengan
                # next_expected_seq di masa depan dan cuma numpuk selamanya.
                return

        st.jitter_pending[seq_num] = payload

        if st.next_expected_seq is None:
            # Tunggu buffer awal terisi dulu sebelum menentukan seq mana
            # yang "ditunggu pertama kali" -- supaya kalau kebetulan paket
            # pertama yang nyampe BUKAN seq terkecil (network sudah
            # reorder sejak awal), kita tetap mulai dari seq yang benar,
            # bukan asal dari yang datang duluan.
            if len(st.jitter_pending) < JITTER_BUFFER_SIZE:
                return
            st.next_expected_seq = self._find_earliest_seq(st.jitter_pending.keys())

        # Fast path: lepas semua paket yang sudah berurutan dari yang
        # ditunggu. Ini PASTI berhenti karena tiap iterasi mengurangi
        # jumlah item di jitter_pending (dict terbatas).
        while st.next_expected_seq in st.jitter_pending:
            self._emit_next(st)

        # Kalau buffer masih kepenuhan setelah itu (berarti paket yang
        # ditunggu memang belum/tidak akan datang), paksa lompat ke paket
        # tertua yang tersedia supaya audio tidak macet permanen. Loop ini
        # juga PASTI berhenti karena tiap iterasi mengurangi isi buffer.
        while len(st.jitter_pending) > JITTER_BUFFER_SIZE:
            oldest = min(st.jitter_pending, key=lambda s: self._seq_gap(st.next_expected_seq, s))
            st.next_expected_seq = oldest
            self._emit_next(st)
            while st.next_expected_seq in st.jitter_pending:
                self._emit_next(st)

    def _emit_next(self, st: "_VADState"):
        """Ambil payload untuk st.next_expected_seq dari buffer, lempar ke VAD, maju satu."""
        payload = st.jitter_pending.pop(st.next_expected_seq)
        st.next_expected_seq = (st.next_expected_seq + 1) % 65536
        st.buffer.extend(payload)
        self._consume_frames()

    @staticmethod
    def _seq_gap(a: int, b: int) -> int:
        """Jarak b relatif terhadap a, aman terhadap wraparound 16-bit."""
        return (b - a) % 65536

    @classmethod
    def _find_earliest_seq(cls, candidates) -> int:
        """
        Cari sequence number "paling awal" dari sekumpulan seq, aman
        terhadap wraparound (mis. kumpulan {65534, 65535, 0, 1} -> yang
        paling awal adalah 65534, BUKAN 0 -- min() biasa akan salah pilih
        0 di sini). Caranya: seq yang benar adalah yang, kalau dijadikan
        titik acuan, punya jarak (gap) TERKECIL ke seq lain yang paling
        jauh -- karena seq lainnya seharusnya semua "di depan"-nya dalam
        rentang kecil (real jitter jauh lebih kecil dari setengah rentang
        16-bit / 32768).
        """
        candidates = list(candidates)
        return min(candidates, key=lambda c: max(cls._seq_gap(c, s) for s in candidates))

    def _consume_frames(self):
        st = self._state
        while len(st.buffer) >= self.frame_bytes:
            frame = bytes(st.buffer[: self.frame_bytes])
            del st.buffer[: self.frame_bytes]

            try:
                is_speech = st.vad.is_speech(frame, self.sample_rate)
            except Exception:
                is_speech = False

            if is_speech:
                st.in_speech = True
                st.silence_run = 0
                st.speech_frames.append(frame)
            elif st.in_speech:
                st.silence_run += 1
                st.speech_frames.append(frame)  # simpan sedikit ekor untuk konteks

            # Vosk (kalau aktif) dikasih makan SETIAP frame, apa adanya,
            # tanpa peduli VAD kita bilang ini speech atau bukan -- Vosk
            # punya endpointing sendiri yang lebih pas buat streaming.
            # Ini yang menggantikan peran "interim" versi Whisper lama
            # (yang sekarang dihapus, karena Vosk jauh lebih cocok untuk
            # peran preview instan ini).
            if self.on_live_audio is not None:
                self.on_live_audio(frame)

            segment_too_long = len(st.speech_frames) >= MAX_SEGMENT_FRAMES
            segment_ended = st.in_speech and (
                st.silence_run >= SILENCE_FRAMES_TO_END_SEGMENT or segment_too_long
            )

            if segment_ended:
                pcm_segment = b"".join(st.speech_frames)
                st.speech_frames = []
                st.in_speech = False
                st.silence_run = 0
                if pcm_segment:
                    self.on_segment(pcm_segment)

    def connection_lost(self, exc):
        # Flush sisa paket yang masih nyangkut di jitter buffer (urut
        # berdasar seq relatif terhadap yang terakhir ditunggu).
        st = self._state
        if st.next_expected_seq is not None:
            remaining = sorted(st.jitter_pending, key=lambda s: self._seq_gap(st.next_expected_seq, s))
            for seq_num in remaining:
                st.next_expected_seq = seq_num
                self._emit_next(st)
        if st.speech_frames:
            self.on_segment(b"".join(st.speech_frames))
            st.speech_frames = []


class RTPReceiver:
    """Bungkus satu UDP endpoint untuk satu leg audio (agent ATAU customer)."""

    def __init__(
        self,
        port: int,
        speaker_label: str,
        on_segment: Callable[[str, bytes], None],
        on_live_audio: Optional[Callable[[str, bytes], None]] = None,
    ):
        self.port = port
        self.speaker_label = speaker_label
        self._on_segment_outer = on_segment
        self._on_live_audio_outer = on_live_audio
        self._transport: Optional[asyncio.DatagramTransport] = None

    async def start(self):
        loop = asyncio.get_event_loop()
        self._transport, _protocol = await loop.create_datagram_endpoint(
            lambda: RTPAudioProtocol(
                settings.AUDIO_SAMPLE_RATE,
                on_segment=lambda pcm: self._on_segment_outer(self.speaker_label, pcm),
                on_live_audio=(
                    (lambda pcm: self._on_live_audio_outer(self.speaker_label, pcm))
                    if self._on_live_audio_outer
                    else None
                ),
            ),
            # PENTING: listen di 0.0.0.0 (semua interface), BUKAN di
            # settings.MEDIA_HOST langsung. MEDIA_HOST dipakai untuk kasih
            # tau Asterisk ke IP mana dia harus KIRIM RTP (lihat
            # ari_client.py, parameter external_host) -- itu peran yang
            # beda dari socket lokal yang MENDENGARKAN di sini. Bind
            # langsung ke IP virtual adapter (mis. Tailscale) sering gagal
            # di Windows (WinError 10049 / address not valid in context).
            local_addr=("0.0.0.0", self.port),
        )
        logger.info("RTP receiver listening on 0.0.0.0:%s (speaker=%s, diarahkan Asterisk ke %s:%s)",
                    self.port, self.speaker_label, settings.MEDIA_HOST, self.port)

    def stop(self):
        if self._transport:
            self._transport.close()
        port_allocator.release(self.port)
