"""
Koneksi ke Asterisk ARI (Asterisk REST Interface) - FreePBX v17 / Asterisk v20.20.1.

Pola yang dipakai (lihat PRD Bab 7.2 & 7.4):
  1. Dialplan FreePBX mengarahkan channel ke Stasis(stt-kb) (lihat
     asterisk_config/extensions_ari_sample.conf).
  2. Saat event StasisStart masuk, kita:
       a. Buat Snoop channel pada channel asli untuk masing-masing arah
          (spy=in untuk suara customer, spy=out untuk suara agent) --
          ini yang memberi kita pemisahan speaker "gratis" tanpa model ML
          diarization terpisah.
       b. Buat externalMedia channel per snoop, diarahkan ke
          MEDIA_HOST:port kita (format audio slin16 / 16kHz PCM).
       c. Bridge snoop channel dengan externalMedia channel di sebuah
          mixing bridge, supaya audio benar-benar mengalir keluar sebagai RTP.
  3. RTPReceiver (audio_capture.py) menerima audio itu, VAD memotong per
     ucapan, lalu pipeline.py menjalankan STT -> KB search -> broadcast.

Catatan: ini adalah implementasi referensi. Nama endpoint/parameter ARI di
bawah mengikuti dokumentasi resmi Asterisk ARI (res_ari, res_ari_channels,
res_ari_bridges) per versi 20.x -- sesuaikan bila ada perbedaan minor di
environment Anda.
"""
import asyncio
import logging

import requests
import websockets
import json as _json

from app.audio_capture import RTPReceiver, port_allocator
from app.config import settings
from app.pipeline import process_audio_segment, broadcast_live_transcript
from app.sherpa_engine import LiveRecognizer

logger = logging.getLogger("ari_client")

AUTH = (settings.ARI_USERNAME, settings.ARI_PASSWORD)


class CallSession:
    """Menyimpan state satu panggilan yang sedang di-capture."""

    def __init__(self, call_id: str, channel_id: str):
        self.call_id = call_id
        self.channel_id = channel_id
        self.receivers: dict[str, RTPReceiver] = {}  # speaker_label -> RTPReceiver
        self.external_media_channel_ids: list[str] = []
        self.snoop_channel_ids: list[str] = []
        self.bridge_ids: list[str] = []  # satu bridge PER arah (customer, agent) -- lihat catatan di _setup_capture_for_channel
        self.live_recognizers: dict[str, LiveRecognizer] = {}  # speaker_label -> sherpa-onnx recognizer (preview instan)
        self.live_recognizer_locks: dict[str, asyncio.Lock] = {}  # kunci per speaker, jaga urutan frame tetap benar

    async def stop(self):
        for recv in self.receivers.values():
            recv.stop()
        for ch_id in self.external_media_channel_ids + self.snoop_channel_ids:
            _ari_delete(f"/channels/{ch_id}")
        for bridge_id in self.bridge_ids:
            _ari_delete(f"/bridges/{bridge_id}")
        for recognizer in self.live_recognizers.values():
            recognizer.close()


_sessions: dict[str, CallSession] = {}  # channel_id -> CallSession
_own_channel_ids: set[str] = set()  # semua snoop + externalMedia channel_id yang KITA buat
_agent_active_call: dict[str, str] = {}  # extension agent -> call_id (channel_id) yang lagi aktif


async def _process_live_audio(sess: "CallSession", speaker: str, recognizer: LiveRecognizer, pcm: bytes, call_id: str):
    """
    Jalankan LiveRecognizer.feed() di THREAD TERPISAH (run_in_executor) --
    meskipun sherpa-onnx model int8 relatif ringan, inference ONNX tetap
    makan waktu CPU nyata, dan ini dipanggil sangat sering (tiap frame
    20ms). Kalau dijalankan sinkron langsung di event loop, event loop
    bisa ke-block cukup lama pas audio banyak masuk -- ini yang bikin
    WebSocket ke-disconnect & reconnect terus-terusan (ping/pong ke client
    telat karena event loop sibuk), persis pola bug yang sama seperti yang
    pernah terjadi di faster-whisper sebelumnya.

    Dikunci per speaker (bukan global) supaya frame-frame untuk SATU leg
    audio yang sama tetap diproses berurutan (recognizer itu stateful,
    kalau diproses tidak berurutan/bertumpukan hasilnya bisa kacau) --
    tapi leg agent & customer tetap bisa jalan paralel karena locknya beda.
    """
    lock = sess.live_recognizer_locks.setdefault(speaker, asyncio.Lock())
    async with lock:
        loop = asyncio.get_event_loop()
        try:
            partial_text, final_text = await loop.run_in_executor(None, recognizer.feed, pcm)
        except Exception:
            logger.exception("Gagal proses live audio (sherpa-onnx) speaker=%s", speaker)
            return

    if partial_text:
        await broadcast_live_transcript(call_id, speaker, partial_text, is_final=False)
    if final_text:
        await broadcast_live_transcript(call_id, speaker, final_text, is_final=True)


def get_active_call_for_agent(extension: str) -> str | None:
    """Dipakai oleh endpoint REST (routers/calls.py) supaya Epic bisa nanya
    'agent nomor X lagi di panggilan yang mana' -> dapat call_id untuk
    dipakai connect ke WebSocket /ws/{call_id}."""
    return _agent_active_call.get(extension)


def _ari_post(path: str, **params):
    r = requests.post(f"{settings.ARI_BASE_URL}/ari{path}", params=params, auth=AUTH, timeout=5)
    r.raise_for_status()
    return r.json() if r.content else None


def _ari_delete(path: str):
    try:
        requests.delete(f"{settings.ARI_BASE_URL}/ari{path}", auth=AUTH, timeout=5)
    except requests.RequestException as exc:
        logger.warning("Gagal hapus resource ARI %s: %s", path, exc)


def _extract_extension(channel_name: str) -> str | None:
    """'PJSIP/201-0000001a' -> '201'"""
    tech_prefix = f"{settings.AGENT_CHANNEL_TECH}/"
    if not channel_name.startswith(tech_prefix):
        return None
    return channel_name[len(tech_prefix):].split("-")[0]


async def _setup_capture_for_channel(
    channel_id: str, return_to_dialplan: bool = True, agent_extension: str | None = None
):
    """
    Bikin 1 pasang snoop+externalMedia untuk arah 'in' dan 1 pasang lagi
    untuk arah 'out' -> hasilnya dua stream terpisah.

    return_to_dialplan=True dipakai kalau channel ini masuk lewat
    Stasis() di dialplan (harus di-continue balik). Untuk channel agent
    yang kita pantau lewat endpoint subscription (bukan lewat Stasis di
    dialplan), channel itu TIDAK pernah "nyangkut" di Stasis, jadi
    /continue tidak perlu (dan akan error kalau dipanggil).
    """
    session = CallSession(call_id=channel_id, channel_id=channel_id)
    _sessions[channel_id] = session

    # PENTING soal arah "in"/"out": sejak capture dipindah ke skema
    # subscribe-endpoint-agent (channel_id di sini = channel milik AGENT,
    # bukan lagi channel customer/trunk seperti skema Stasis dialplan yang
    # lama), artinya arah dibalik dari sebelumnya:
    #   spy="in"  -> suara yang MASUK dari channel ini = suara AGENT sendiri
    #   spy="out" -> suara yang DIKIRIM ke channel ini (yang didengar agent,
    #                setelah ke-bridge) = suara CUSTOMER
    for spy_direction, speaker_label in (("in", "agent"), ("out", "customer")):
        # PENTING: satu bridge KHUSUS per arah, JANGAN satu bridge dipakai
        # bersama untuk kedua arah. Bridge tipe "mixing" men-JUMLAHKAN
        # audio semua channel di dalamnya -- kalau snoop customer, snoop
        # agent, dan kedua externalMedia digabung dalam satu bridge yang
        # sama, suara customer dan agent akan tercampur/dijumlahkan
        # (menyebabkan clipping parah karena dua sinyal full-scale
        # dijumlahkan) DAN kedua speaker jadi tidak benar-benar terpisah,
        # meniadakan tujuan awal pemisahan speaker itu sendiri.
        bridge = _ari_post("/bridges", type="mixing")
        session.bridge_ids.append(bridge["id"])

        snoop = _ari_post(
            f"/channels/{channel_id}/snoop",
            spy=spy_direction,
            whisper="none",
            app=settings.ARI_APP_NAME,
        )
        session.snoop_channel_ids.append(snoop["id"])
        _own_channel_ids.add(snoop["id"])

        port = port_allocator.acquire()
        ext_media = _ari_post(
            "/channels/externalMedia",
            app=settings.ARI_APP_NAME,
            external_host=f"{settings.MEDIA_HOST}:{port}",
            format="slin16",
        )
        session.external_media_channel_ids.append(ext_media["id"])
        _own_channel_ids.add(ext_media["id"])

        _ari_post(f"/bridges/{bridge['id']}/addChannel", channel=snoop["id"])
        _ari_post(f"/bridges/{bridge['id']}/addChannel", channel=ext_media["id"])

        if settings.SHERPA_ENABLED:
            session.live_recognizers[speaker_label] = LiveRecognizer(sample_rate=16000)

        async def on_segment(speaker: str, pcm: bytes, call_id=channel_id):
            await process_audio_segment(call_id, speaker, pcm)

        def on_live_audio(speaker: str, pcm: bytes, call_id=channel_id, sess=session):
            recognizer = sess.live_recognizers.get(speaker)
            if recognizer is None:
                return
            asyncio.create_task(_process_live_audio(sess, speaker, recognizer, pcm, call_id))

        receiver = RTPReceiver(
            port=port,
            speaker_label=speaker_label,
            on_segment=lambda spk, pcm, cb=on_segment: asyncio.create_task(cb(spk, pcm)),
            on_live_audio=on_live_audio,
        )
        await receiver.start()
        session.receivers[speaker_label] = receiver

    logger.info("Capture aktif untuk call_id=%s (agent+customer terpisah)", channel_id)

    if agent_extension:
        _agent_active_call[agent_extension] = channel_id

    if return_to_dialplan:
        # PENTING: channel asli hanya "mampir" sebentar ke Stasis untuk kita
        # pasangi snoop, lalu HARUS dikembalikan ke dialplan supaya panggilan
        # tetap diproses normal oleh FreePBX. Tanpa ini, channel akan
        # tersangkut di aplikasi Stasis kita.
        _ari_post(f"/channels/{channel_id}/continue")


def _subscribe_agent_endpoints():
    """
    Subscribe ARI app ke endpoint tiap agent (mis. PJSIP/201) supaya kita
    kebagian event channel-nya TANPA channel itu perlu masuk Stasis lewat
    dialplan. Ini yang bikin capture-nya independen dari Queue/IVR mana pun
    yang dipakai untuk merutekan panggilan ke agent.
    """
    for ext in settings.AGENT_EXTENSIONS:
        endpoint = f"{settings.AGENT_CHANNEL_TECH}/{ext}"
        try:
            _ari_post(
                f"/applications/{settings.ARI_APP_NAME}/subscription",
                eventSource=f"endpoint:{endpoint}",
            )
            logger.info("Subscribe ke endpoint agent: %s", endpoint)
        except Exception:
            logger.exception("Gagal subscribe endpoint agent %s", endpoint)


def _is_agent_channel(channel: dict) -> bool:
    """Cek apakah channel ini punya channel_id di salah satu endpoint agent."""
    tech_prefix = f"{settings.AGENT_CHANNEL_TECH}/"
    name = channel.get("name", "")  # contoh: "PJSIP/201-0000001a"
    if not name.startswith(tech_prefix):
        return False
    ext_part = name[len(tech_prefix):].split("-")[0]
    return ext_part in settings.AGENT_EXTENSIONS


async def _handle_event(event: dict):
    ev_type = event.get("type")

    if ev_type == "StasisStart":
        channel = event["channel"]
        channel_id = channel["id"]
        # Hindari re-trigger untuk snoop/externalMedia channel kita sendiri
        # (channel-channel ini juga masuk app Stasis yang sama, jadi ikut
        # memicu StasisStart -- kalau tidak di-skip, akan bikin loop
        # rekursif: snoop dari snoop, tanpa henti).
        if channel_id in _sessions or channel_id in _own_channel_ids:
            return
        logger.info("StasisStart channel_id=%s caller=%s", channel_id,
                    channel.get("caller", {}).get("number"))
        try:
            await _setup_capture_for_channel(channel_id)
        except Exception:
            logger.exception("Gagal setup capture untuk channel %s", channel_id)

    elif ev_type == "ChannelStateChange":
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        state = channel.get("state")
        # Cuma trigger sekali, pas channel agent baru saja terjawab (Up),
        # dan belum ada sesi capture buat channel ini, dan ini BUKAN salah
        # satu channel snoop/externalMedia buatan kita sendiri.
        if (
            state == "Up"
            and channel_id not in _sessions
            and channel_id not in _own_channel_ids
            and _is_agent_channel(channel)
        ):
            logger.info("Agent channel Up: id=%s name=%s -> mulai capture",
                        channel_id, channel.get("name"))
            try:
                ext = _extract_extension(channel.get("name", ""))
                await _setup_capture_for_channel(
                    channel_id, return_to_dialplan=False, agent_extension=ext
                )
            except Exception:
                logger.exception("Gagal setup capture untuk agent channel %s", channel_id)

    elif ev_type in ("StasisEnd", "ChannelDestroyed"):
        channel_id = event.get("channel", {}).get("id")
        _own_channel_ids.discard(channel_id)
        session = _sessions.pop(channel_id, None)
        if session:
            logger.info("Panggilan selesai, membersihkan resource call_id=%s", channel_id)
            await session.stop()
            # Bersihkan juga mapping agent -> call_id kalau ini call yang sedang aktif
            for ext, active_call_id in list(_agent_active_call.items()):
                if active_call_id == channel_id:
                    del _agent_active_call[ext]


async def run_ari_listener():
    """Background task: subscribe ke event ARI via WebSocket, jalan selama app hidup."""
    url = (
        f"{settings.ARI_WS_URL}/ari/events"
        f"?app={settings.ARI_APP_NAME}&api_key={settings.ARI_USERNAME}:{settings.ARI_PASSWORD}"
    )
    backoff = 2
    while True:
        try:
            logger.info("Menghubungkan ke ARI WebSocket ...")
            async with websockets.connect(url) as ws:
                logger.info("Terhubung ke ARI, app=%s", settings.ARI_APP_NAME)
                backoff = 2
                if settings.AGENT_EXTENSIONS:
                    _subscribe_agent_endpoints()
                async for raw in ws:
                    event = _json.loads(raw)
                    await _handle_event(event)
        except Exception as exc:
            logger.warning("Koneksi ARI terputus (%s), reconnect dalam %ss ...", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
