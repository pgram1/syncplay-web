import asyncio
import json
import threading
import os
import signal
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from collections import defaultdict
import websockets

# room_code -> {"clients": [ws, ...], "state": {"playing": bool, "pos": float, "time": float, "rate": float}}
rooms = defaultdict(lambda: {"clients": [], "state": None})

async def handler(websocket):
    room_code = None
    try:
        async for raw in websocket:
            try:
                data = json.loads(raw)
            except Exception:
                continue

            msg_type = data.get("type")

            # --- Join Logic ---
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

                # Catch up the new joiner to the current state
                if room["state"]:
                    await _send(websocket, {"type": "sync", **room["state"]})

                if len(room["clients"]) == 2:
                    await _broadcast(room_code, {"type": "partner_joined"})
                continue

            # --- Advanced Sync (Interrupt/Seek/Rate) ---
            if msg_type == "sync" and room_code:
                # Update the room's source of truth
                # pos: playback position, time: server timestamp, rate: playback speed
                rooms[room_code]["state"] = {
                    "playing": data.get("playing"),
                    "pos": data.get("pos"),
                    "rate": data.get("rate", 1.0),
                    "server_time": time.time()
                }
                # Relay the change to the other client immediately
                await _broadcast(room_code, data, sender=websocket)

            # --- Chat ---
            elif msg_type == "chat" and room_code:
                await _broadcast(room_code, data, sender=websocket)

            # --- Heartbeat ---
            elif msg_type == "heartbeat":
                await _send(websocket, {"type": "heartbeat_ack"})

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        _remove_client(room_code, websocket)

# --- Helpers ---
async def _send(ws, obj):
    try:
        await ws.send(json.dumps(obj))
    except:
        pass

async def _broadcast(room_code, obj, sender=None):
    room = rooms.get(room_code)
    if not room: return
    payload = json.dumps(obj)
    # Use a list copy to prevent "size changed during iteration" errors
    for ws in list(room["clients"]):
        if ws is not sender:
            await _send(ws, obj)

def _remove_client(room_code, websocket):
    if room_code in rooms:
        room = rooms[room_code]
        if websocket in room["clients"]:
            room["clients"].remove(websocket)
            if room["clients"]:
                asyncio.create_task(_broadcast(room_code, {"type": "partner_left"}))
            else:
                del rooms[room_code]

# --- HTTP Server ---
class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_): pass
    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

def _run_http_server():
    # Recommended: Move index.html to a 'public' folder to protect your .py source
    path = os.path.join(os.path.dirname(__file__), "public")
    if os.path.exists(path): os.chdir(path)

    httpd = HTTPServer(("0.0.0.0", 5000), QuietHandler)
    print("🌐 Web UI: http://localhost:5000")
    httpd.serve_forever()

# --- Main Entry & Interrupt Handling ---
async def main():
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    # Handle Ctrl+C (SIGINT) and Termination (SIGTERM)
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop_event.set)

    print("🚀 SyncPlay Ultra — Starting...")

    threading.Thread(target=_run_http_server, daemon=True).start()

    async with websockets.serve(handler, "0.0.0.0", 8765, ping_interval=20):
        print("🔌 WebSocket: ws://localhost:8765")
        await stop_event.wait()

    print("\n🛑 Server shutting down gracefully...")

if __name__ == "__main__":
    asyncio.run(main())
