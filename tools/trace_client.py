import asyncio
import websockets
import argparse
import json

async def trace_client(session_id: str):
    uri = f"ws://localhost:8000/api/v1/sessions/{session_id}/trace"
    try:
        async with websockets.connect(uri) as websocket:
            print(f"Successfully connected to {uri}")
            while True:
                try:
                    message = await websocket.recv()
                    data = json.loads(message)
                    print(f"Received: {json.dumps(data, indent=2)}")
                except websockets.ConnectionClosed:
                    print("Connection closed by the server.")
                    break
    except Exception as e:
        print(f"Failed to connect or an error occurred: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Connect to the trace WebSocket.")
    parser.add_argument("session_id", type=str, help="The session ID to connect to.")
    args = parser.parse_args()

    asyncio.run(trace_client(args.session_id)) 