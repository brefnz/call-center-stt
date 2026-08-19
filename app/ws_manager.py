"""
Manajer koneksi WebSocket, per call_id.

Aplikasi Epic (atau, untuk sekarang, halaman demo di scripts/demo_epic_panel.html)
connect ke ws://<server>/ws/{call_id} untuk menerima event transkrip & saran KB
secara real-time saat panggilan berlangsung.
"""
import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger("ws_manager")


class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = defaultdict(set)
        # Kunci PER-KONEKSI (bukan per-call_id) -- supaya connection yang
        # beda bisa kirim bebas bersamaan, tapi pengiriman ke SATU
        # WebSocket yang sama tetap berurutan (dijamin serial). Tanpa ini,
        # beberapa broadcast yang kejadian nyaris bersamaan (mis. interim
        # dari leg agent & customer, plus final Whisper, semuanya nyoba
        # ngirim ke websocket yang sama nyaris berbarengan) bisa saling
        # tabrakan dan bikin koneksinya rusak/ke-disconnect -- ini yang
        # jadi penyebab widget reconnect terus pas lagi ramai bicara.
        self._locks: dict[WebSocket, asyncio.Lock] = {}

    async def connect(self, call_id: str, ws: WebSocket):
        await ws.accept()
        self._connections[call_id].add(ws)
        self._locks[ws] = asyncio.Lock()
        logger.info("Epic client subscribed to call_id=%s (total=%d)",
                    call_id, len(self._connections[call_id]))

    def disconnect(self, call_id: str, ws: WebSocket):
        self._connections[call_id].discard(ws)
        self._locks.pop(ws, None)
        if not self._connections[call_id]:
            self._connections.pop(call_id, None)

    async def broadcast(self, call_id: str, event: dict):
        dead = []
        payload = json.dumps(event, ensure_ascii=False)
        for ws in list(self._connections.get(call_id, [])):
            lock = self._locks.get(ws)
            if lock is None:
                continue
            try:
                async with lock:
                    await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(call_id, ws)

    # FIX: sebelumnya tidak ada cara buat BACKEND inisiatif nutup koneksi
    # client saat call beneran selesai -- disconnect() cuma dipanggil kalau
    # client sendiri yang mutusin. Akibatnya widget di sisi Epic/CRM tidak
    # pernah dapat event `onclose`, dan status di UI-nya nyangkut
    # "terhubung" terus meski panggilan sudah berakhir.
    #
    # Dipanggil dari app/ari_client.py tepat saat StasisEnd/ChannelDestroyed
    # diterima untuk call itu, SETELAH mapping agent->call_id dihapus (lihat
    # komentar FIX di ari_client.py) supaya tidak ada window balapan dengan
    # polling /active-call.
    async def close_call(self, call_id: str, code: int = 1000, reason: str = "call_ended"):
        """Tutup semua koneksi WebSocket yang lagi subscribe ke call_id ini."""
        conns = list(self._connections.get(call_id, []))
        for ws in conns:
            try:
                await ws.close(code=code, reason=reason)
            except Exception:
                logger.warning("Gagal menutup WebSocket untuk call_id=%s", call_id)
            finally:
                self.disconnect(call_id, ws)


ws_manager = ConnectionManager()
