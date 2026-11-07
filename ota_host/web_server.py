"""Local HTTP server for the OTA web UI."""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import queue
import signal
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .engine import OtaEngine, OtaError
from .protocol import ProtocolError
from .serial_link import SerialLink

ROOT_DIR = Path(__file__).resolve().parents[1]
WEB_DIR = ROOT_DIR / "web"
MAX_BODY_BYTES = 2 * 1024 * 1024


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _parse_program_id(value: Any) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip().lower()
    return int(text, 16) if text.startswith("0x") else int(text, 10)


class OtaHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, engine: OtaEngine):
        super().__init__(server_address, handler_class)
        self.engine = engine
        self.ui_client_seen = False


class OtaRequestHandler(BaseHTTPRequestHandler):
    server: OtaHttpServer
    server_version = "OtaConsole/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # Runtime logs are managed by OtaEngine; keep the console quiet.
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/state":
            if not self.server.ui_client_seen:
                self.server.ui_client_seen = True
                self.server.engine._log("success", "Web 界面已连接到本地服务")
            self._send_json(HTTPStatus.OK, self.server.engine.snapshot(include_logs=True))
        elif path == "/api/ports":
            self._send_json(
                HTTPStatus.OK,
                {
                    "serial_available": SerialLink.available(),
                    "ports": SerialLink.list_ports(),
                },
            )
        elif path == "/api/events":
            self._serve_events()
        else:
            self._serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
            if path == "/api/connect":
                self.server.engine.connect(str(payload.get("port", "")), int(payload.get("baudrate", 115200)))
                self._send_json(HTTPStatus.OK, {"ok": True})
            elif path == "/api/disconnect":
                self.server.engine.disconnect()
                self._send_json(HTTPStatus.OK, {"ok": True})
            elif path == "/api/query":
                self.server.engine.start_query(str(payload.get("which", "")))
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True})
            elif path == "/api/firmware/inspect":
                firmware = self._decode_firmware(payload)
                result = self.server.engine.inspect_firmware(
                    firmware,
                    str(payload.get("filename", "firmware.bin")),
                    bool(payload.get("pad", True)),
                )
                self._send_json(HTTPStatus.OK, result)
            elif path == "/api/upgrade":
                firmware = self._decode_firmware(payload)
                program_id = _parse_program_id(payload.get("program_id", 0))
                self.server.engine.start_upgrade(
                    firmware=firmware,
                    filename=str(payload.get("filename", "firmware.bin")),
                    program_id=program_id,
                    pad=bool(payload.get("pad", True)),
                )
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True})
            elif path == "/api/stop":
                self.server.engine.stop()
                self._send_json(HTTPStatus.OK, {"ok": True})
            elif path == "/api/clear-logs":
                self.server.engine.clear_ui_logs()
                self._send_json(HTTPStatus.OK, {"ok": True})
            else:
                self._send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
        except (OtaError, ValueError, ProtocolError) as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception as exc:
            self.server.engine._log("error", f"HTTP 请求异常: {exc}", category="system")
            self.server.engine._logger.error(traceback.format_exc())
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务内部错误: {exc}")

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求 JSON 无效") from exc
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 必须为对象")
        return value

    @staticmethod
    def _decode_firmware(payload: dict) -> bytes:
        encoded = payload.get("firmware_base64")
        if not isinstance(encoded, str) or not encoded:
            raise ValueError("缺少 firmware_base64")
        try:
            return base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise ValueError("固件 base64 数据无效") from exc

    def _serve_events(self) -> None:
        subscriber = self.server.engine.bus.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            initial_state = {
                "kind": "state",
                "state": self.server.engine.snapshot(include_logs=False),
            }
            self._write_sse(initial_state)
            initial_logs = {
                "kind": "initial_logs",
                "logs": self.server.engine.snapshot(include_logs=True).get("logs", []),
            }
            self._write_sse(initial_logs)
            while True:
                try:
                    event = subscriber.get(timeout=1.0)
                    self._write_sse(event)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.engine.bus.unsubscribe(subscriber)
            self.close_connection = True

    def _write_sse(self, event: dict) -> None:
        self.wfile.write(b"data: " + _json_bytes(event) + b"\n\n")
        self.wfile.flush()

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in ("", "/") else unquote(request_path.lstrip("/"))
        target = (WEB_DIR / relative).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._send_error_json(HTTPStatus.FORBIDDEN, "非法路径")
            return
        if not target.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "文件不存在")
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or target.suffix in (".js", ".css"):
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: HTTPStatus, value: Any) -> None:
        body = _json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"ok": False, "error": message})


def create_server(host: str, port: int, engine: OtaEngine) -> OtaHttpServer:
    last_error = None
    for candidate in range(port, port + 20):
        try:
            return OtaHttpServer((host, candidate), OtaRequestHandler, engine)
        except OSError as exc:
            last_error = exc
    raise OtaError(f"无法启动本地服务: {last_error}")


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> int:
    engine = OtaEngine()
    server = create_server(host, port, engine)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    engine._log("info", f"Web 服务已启动: {url}")
    print(f"OTA Console: {url}")
    print("按 Ctrl+C 停止服务。")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    stop_event = threading.Event()

    def request_shutdown(signum=None, frame=None):
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, request_shutdown)
        signal.signal(signal.SIGTERM, request_shutdown)
    except (ValueError, AttributeError):
        pass

    server_thread = threading.Thread(target=server.serve_forever, name="ota-http", daemon=True)
    server_thread.start()
    try:
        while not stop_event.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        engine.shutdown()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OTA Console local web server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run(args.host, args.port, not args.no_browser))



