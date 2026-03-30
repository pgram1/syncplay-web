import asyncio
import json
import threading
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler
from collections import defaultdict
import websockets

# room_code -> {"clients": [ws, ...], "last_sync": {...}}
rooms = defaultdict(lambda: {"clients": [], "last_sync": None})


# ─── WebSocket handler ────────────────────────────────────────────────────────

async def handler(websocket):
    room_code = None
    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except Exception:
                continue

            msg_type = data.get("type")

            # ── Join ──────────────────────────────────────────────────────────
            if msg_type == "join":
                room_code = data.get("room", "").strip().lower().replace(" ", "-")
                if not room_code:
                    await _send(websocket, {"type": "error", "message": "Room code is required"})
                    room_code = None
                    continue

                room = rooms[room_code]
                clients = room["clients"]

                if len(clients) >= 2:
                    await _send(websocket, {"type": "error", "message": "Room is full (max 2 people)"})
                    room_code = None
                    continue

                clients.append(websocket)
                await _send(websocket, {"type": "joined"})
                print(f"[+] Room '{room_code}' — joined ({len(clients)}/2)")

                if len(clients) == 2:
                    # Notify both that the partner has arrived.
                    await _broadcast(room_code, {"type": "partner_joined"}, sender=None)
                    # Catch the new joiner up to the last known playback state.
                    if room["last_sync"]:
                        await _send(websocket, room["last_sync"])

                continue

            # ── Heartbeat ─────────────────────────────────────────────────────
            if msg_type == "heartbeat":
                await _send(websocket, {"type": "heartbeat_ack"})
                continue

            # ── Sync (relay + cache) ──────────────────────────────────────────
            if msg_type == "sync" and room_code:
                rooms[room_code]["last_sync"] = data
                await _relay(room_code, websocket, data)
                continue

            # ── Chat (relay only) ─────────────────────────────────────────────
            if msg_type == "chat" and room_code:
                await _relay(room_code, websocket, data)
                continue

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as exc:
        print(f"[!] Error in room '{room_code}': {exc}")
    finally:
        _remove_client(room_code, websocket)


# ─── Async helpers ────────────────────────────────────────────────────────────

async def _send(ws, obj):
    try:
        await ws.send(json.dumps(obj))
    except Exception:
        pass


async def _relay(room_code, sender, obj):
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


async def _broadcast(room_code, obj, sender=None):
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


def _remove_client(room_code, websocket):
    if not room_code or room_code not in rooms:
        return
    room = rooms[room_code]
    clients = room["clients"]
    if websocket not in clients:
        return

    clients.remove(websocket)
    print(f"[-] Room '{room_code}' — client left ({len(clients)}/2)")

    if clients:
        asyncio.get_event_loop().create_task(
            _send(clients[0], {"type": "partner_left"})
        )
    else:
        del rooms[room_code]
        print(f"[x] Room '{room_code}' deleted (empty)")


# ─── HTTP file server (serves index.html on port 5000) ───────────────────────

class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass  # suppress per-request stdout noise

    def end_headers(self):
        # Disable caching so the browser always picks up the latest index.html
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def _run_http_server():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    httpd = HTTPServer(("0.0.0.0", 5000), QuietHandler)
    print("🌐  HTTP server  →  http://0.0.0.0:5000  (serves index.html)")
    httpd.serve_forever()


# ─── Entry point ──────────────────────────────────────────────────────────────

async def main():
    print("🚀  SyncPlay Ultra — signaling server starting…")

    # HTTP server in a daemon thread (serves index.html)
    t = threading.Thread(target=_run_http_server, daemon=True)
    t.start()

    # WebSocket server in the asyncio loop
    async with websockets.serve(
        handler,
        "0.0.0.0",
        8765,
        ping_interval=20,
        ping_timeout=40,
    ):
        print("🔌  WebSocket server  →  ws://0.0.0.0:8765  (sync protocol)")
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
