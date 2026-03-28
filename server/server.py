import asyncio
import json
import websockets
from collections import defaultdict

rooms = defaultdict(list)  # room_code -> list of websockets

async def handler(websocket):
    room = None
    try:
        async for message in websocket:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "join":
                room = data.get("room")
                if not room:
                    await websocket.send(json.dumps({"type": "error", "message": "Room code is required"}))
                    continue

                if len(rooms[room]) >= 2:
                    await websocket.send(json.dumps({"type": "error", "message": "Room is full (max 2 people)"}))
                    continue

                rooms[room].append(websocket)

                await websocket.send(json.dumps({"type": "joined"}))

                print(f"✅ Room '{room}' — User joined (Total: {len(rooms[room])})")

                if len(rooms[room]) == 2:
                    partner_msg = json.dumps({"type": "partner_joined"})
                    for ws in rooms[room]:
                        try:
                            await ws.send(partner_msg)
                        except:
                            pass

                continue

            # Heartbeat handling - just acknowledge quickly
            if msg_type == "heartbeat":
                try:
                    await websocket.send(json.dumps({"type": "heartbeat_ack"}))
                except:
                    pass
                continue

            # Relay sync and chat
            if room and msg_type in ("sync", "chat"):
                relay_data = json.dumps(data)
                for other in rooms[room]:
                    if other is not websocket:
                        try:
                            await other.send(relay_data)
                        except:
                            pass

    except websockets.exceptions.ConnectionClosed:
        print(f"Client disconnected from room '{room or 'unknown'}'")
    except Exception as e:
        print(f"Error in room '{room or 'unknown'}': {e}")
    finally:
        if room and websocket in rooms.get(room, []):
            rooms[room].remove(websocket)
            print(f"Client removed from room '{room}'. Remaining: {len(rooms[room])}")
            if not rooms[room]:
                del rooms[room]
                print(f"Room '{room}' deleted (empty)")

async def main():
    async with websockets.serve(
        handler,
        "0.0.0.0",
        8765,
        ping_interval=10,      # Server-side ping every 10s (complements client heartbeat)
        ping_timeout=30
    ):
        print("🚀 SyncPlay Ultra Python Server running on ws://0.0.0.0:8765")
        print("   → Client heartbeat every 1 second enabled")
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())
