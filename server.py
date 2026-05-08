import asyncio
import json
import threading
import os
import signal
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from collections import defaultdict
import websockets

# Authority sync interval in seconds
AUTHORITY_INTERVAL = 2

# room_code -> {"clients": [ws, ...], "state": {...}, "sync_task": asyncio.Task|None}
rooms = defaultdict(lambda: {"clients": [], "state": None, "sync_task": None})


async def handler(websocket):
    room_code = None
    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except Exception:
                continue

            msg_type = data.get("type")

            # --- Join ---
            if msg_type == "join":
                room_code = data.get("room", "").strip().lower().replace(" ", "-")
                if not room_code:
                    await _send(websocket, {"type": "error", "message": "Room code required"})
                    continue

                room = rooms[room_code]
                if len(room["clients"]) >= 2:
                    await _send(websocket, {"type": "error", "message": "Room is full"})
                    continue

                room["clients"].append(websocket)
                await _send(websocket, {"type": "joined", "room": room_code})

                # Catch new joiner up to current state, advancing pos if playing
                if room["state"]:
                    state = room["state"].copy()
                    if state.get("playing"):
                        elapsed = time.time() - state.get("server_time", time.time())
                        state["pos"] = (state.get("pos") or 0) + elapsed * (state.get("rate") or 1.0)
                    state["server_time"] = time.time()
                    await _send(websocket, {"type": "sync", **state})

                if len(room["clients"]) == 2:
                    await _broadcast(room_code, {"type": "partner_joined"})
                    # Start the authority sync loop now that both peers are present
                    _start_authority_loop(room_code)
                continue

            # --- Sync (from a client) ---
            if msg_type == "sync" and room_code:
                now = time.time()
                rooms[room_code]["state"] = {
                    "playing":     data.get("playing"),
                    "pos":         data.get("pos"),
                    "rate":        data.get("rate", 1.0),
                    "server_time": now,
                }
                # Immediately push the server's authoritative state to ALL clients
                # (including the sender).  The sender suppresses their own echo
                # via senderId comparison — the partner gets a hard-correct with
                # the right force flag preserved.
                await _broadcast_all(room_code, {
                    "type":        "sync",
                    "pos":         data.get("pos"),
                    "playing":     data.get("playing"),
                    "rate":        data.get("rate", 1.0),
                    "force":       data.get("force", False),
                    "authority":   True,
                    "senderId":    data.get("senderId"),  # echoed back; sender uses this to suppress their own echo
                    "leaderTs":    int(now * 1000),
                    "server_time": now,
                })

            # --- Chat ---
            elif msg_type == "chat" and room_code:
                await _broadcast(room_code, data, sender=websocket)

            # --- Heartbeat — echo sentAt for RTT measurement ---
            elif msg_type == "heartbeat":
                await _send(websocket, {
                    "type":        "heartbeat_ack",
                    "sentAt":      data.get("sentAt"),
                    "server_time": time.time(),
                })

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _remove_client(room_code, websocket)


# ─── Authority sync loop ──────────────────────────────────────────────────────

def _start_authority_loop(room_code):
    """Start (or restart) the authority sync loop for a room."""
    room = rooms.get(room_code)
    if not room:
        return
    _cancel_authority_loop(room_code)
    task = asyncio.create_task(_authority_sync_loop(room_code))
    room["sync_task"] = task


def _cancel_authority_loop(room_code):
    room = rooms.get(room_code)
    if not room:
        return
    task = room.get("sync_task")
    if task and not task.done():
        task.cancel()
    room["sync_task"] = None


async def _authority_sync_loop(room_code):
    """
    Every AUTHORITY_INTERVAL seconds, push the server's ground-truth position
    to ALL clients in the room.  This corrects accumulated drift regardless of
    who the current 'leader' is.
    """
    try:
        while True:
            await asyncio.sleep(AUTHORITY_INTERVAL)

            room = rooms.get(room_code)
            if not room or len(room["clients"]) < 2:
                break  # room gone or partner left — loop ends naturally

            state = room.get("state")
            if not state or state.get("pos") is None:
                continue  # no state yet (no one has played) — wait

            # Compute the real-time position
            pos = state["pos"]
            if state.get("playing"):
                elapsed = time.time() - state.get("server_time", time.time())
                pos = max(0.0, pos + elapsed * (state.get("rate") or 1.0))

            msg = {
                "type":      "sync",
                "pos":       pos,
                "playing":   state.get("playing"),
                "rate":      state.get("rate", 1.0),
                "authority": True,           # clients handle this specially
                # Use server wall-clock ms as leaderTs so clients can gauge freshness
                "leaderTs":  int(time.time() * 1000),
            }
            await _broadcast_all(room_code, msg)

    except asyncio.CancelledError:
        pass  # normal — room emptied or server is shutting down


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _send(ws, obj):
    try:
        await ws.send(json.dumps(obj))
    except Exception:
        pass


async def _broadcast(room_code, obj, sender=None):
    """Send to every client in the room except sender."""
    room = rooms.get(room_code)
    if not room:
        return
    payload = json.dumps(obj)
    for ws in list(room["clients"]):
        if ws is not sender:
            try:
                await ws.send(payload)
            except Exception:
                pass


async def _broadcast_all(room_code, obj):
    """Send to every client in the room, including the current leader."""
    room = rooms.get(room_code)
    if not room:
        return
    payload = json.dumps(obj)
    for ws in list(room["clients"]):
        try:
            await ws.send(payload)
        except Exception:
            pass


def _remove_client(room_code, websocket):
    if room_code not in rooms:
        return
    room = rooms[room_code]
    if websocket not in room["clients"]:
        return
    room["clients"].remove(websocket)
    if room["clients"]:
        # Still one client left — stop the authority loop and notify them
        _cancel_authority_loop(room_code)
        asyncio.create_task(_broadcast(room_code, {"type": "partner_left"}))
    else:
        _cancel_authority_loop(room_code)
        del rooms[room_code]


# ─── HTTP server ──────────────────────────────────────────────────────────────

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_): pass
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()


def _run_http_server():
    path = os.path.join(os.path.dirname(__file__), "public")
    if os.path.exists(path):
        os.chdir(path)
    httpd = HTTPServer(("0.0.0.0", 5000), QuietHandler)
    print("🌐 Web UI: http://localhost:5000")
    httpd.serve_forever()


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    print("🚀 SyncPlay Ultra — Starting...")
    threading.Thread(target=_run_http_server, daemon=True).start()

    async with websockets.serve(handler, "0.0.0.0", 8765,
                                ping_interval=20, ping_timeout=40):
        print("🔌 WebSocket: ws://localhost:8765")
        await stop_event.wait()

    print("\n🛑 Server shutting down gracefully...")


if __name__ == "__main__":
    asyncio.run(main())
