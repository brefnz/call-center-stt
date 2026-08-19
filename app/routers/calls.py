from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app import ari_client, db, pipeline
from app.schemas import TranscriptEvent
from app.ws_manager import ws_manager

router = APIRouter(tags=["calls"])


@router.get("/agents/{extension}/active-call")
def get_active_call_for_agent(extension: str):
    """
    Dipanggil Epic (atau panel apapun di layar agent) untuk tau call_id
    yang sedang aktif untuk extension agent tertentu. Kalau ada, Epic
    langsung connect ke WebSocket /ws/{call_id} untuk mulai nerima
    transkrip + saran KB real-time.

    Kontrak yang disarankan untuk sisi Epic: poll endpoint ini tiap 1-2
    detik selagi agent belum dalam panggilan. Begitu call_id muncul,
    stop polling dan buka WebSocket -- lanjut poll lagi setelah
    WebSocket itu disconnect (menandakan panggilan selesai).
    """
    call_id = ari_client.get_active_call_for_agent(extension)
    return {"extension": extension, "call_id": call_id, "in_call": call_id is not None}


@router.websocket("/ws/{call_id}")
async def epic_panel_socket(websocket: WebSocket, call_id: str):
    """
    Endpoint yang di-consume oleh aplikasi Epic (atau demo panel) untuk
    menerima event transkrip & saran KB secara real-time selama panggilan
    dengan call_id ini berlangsung.
    """
    await ws_manager.connect(call_id, websocket)
    try:
        while True:
            # Kita tidak mengharapkan pesan dari client, tapi tetap perlu
            # menunggu supaya koneksi tidak langsung ditutup.
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(call_id, websocket)


@router.get("/calls/{call_id}/transcripts")
def get_call_transcripts(call_id: str):
    return db.transcript_list_for_call(call_id)


@router.post("/calls/transcript-event")
async def push_transcript_event(payload: TranscriptEvent):
    """
    Endpoint bantu untuk testing manual (curl/Postman) tanpa perlu audio asli:
    kirim teks langsung, pipeline akan jalan seperti biasa (KB search,
    simpan log, broadcast ke WebSocket).
    """
    result = await pipeline.process_transcript_text(
        payload.call_id, payload.speaker, payload.text
    )
    return result
