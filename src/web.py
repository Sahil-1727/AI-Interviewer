"""Local web UI and token endpoint for the AI interviewer."""

import json
import os
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from livekit import api

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
load_dotenv(BASE_DIR / ".env.local")


class InterviewUIHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/connection-details":
            try:
                self.send_json(self.connection_details())
            except (ValueError, KeyError) as error:
                self.send_json({"detail": str(error)}, status=500)
        elif path == "/api/interview-config":
            config = json.loads(
                (BASE_DIR / "interview_config.json").read_text(encoding="utf-8")
            )
            self.send_json(
                {
                    "role": config.get("role"),
                    "experience_level": config.get("experience_level"),
                }
            )
        elif path == "/" or path in {"/app.js", "/styles.css"}:
            file_path = WEB_DIR / ("index.html" if path == "/" else path[1:])
            content_type = {
                ".html": "text/html; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
            }[file_path.suffix]
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            self.wfile.write(file_path.read_bytes())
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_error(404)

    def connection_details(self) -> dict[str, str]:
        livekit_url = os.getenv("LIVEKIT_URL")
        api_key = os.getenv("LIVEKIT_API_KEY")
        api_secret = os.getenv("LIVEKIT_API_SECRET")
        if not all((livekit_url, api_key, api_secret)):
            raise ValueError("LiveKit credentials are not configured in .env.local")

        room_name = f"interview-{secrets.token_urlsafe(8)}"
        identity = f"candidate-{secrets.token_urlsafe(6)}"
        dispatch = api.RoomAgentDispatch(agent_name="livekit-ai-interviewer")
        deployment = os.getenv("LIVEKIT_AGENT_DEPLOYMENT")
        if deployment:
            dispatch.deployment = deployment

        token = (
            api.AccessToken(api_key, api_secret)
            .with_identity(identity)
            .with_name("Candidate")
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                )
            )
            .with_room_config(api.RoomConfiguration(agents=[dispatch]))
        )
        return {
            "serverUrl": livekit_url,
            "participantToken": token.to_jwt(),
            "roomName": room_name,
            "participantName": "Candidate",
        }

    def send_json(self, data: dict[str, str], status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format_string: str, *args: object) -> None:
        if self.path.startswith("/api/connection-details"):
            return
        super().log_message(format_string, *args)


if __name__ == "__main__":
    port = int(os.getenv("UI_PORT", "8000"))
    server = ThreadingHTTPServer(("127.0.0.1", port), InterviewUIHandler)
    print(f"Interview UI running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down interview UI")
    finally:
        server.server_close()
