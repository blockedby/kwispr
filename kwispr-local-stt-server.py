#!/usr/bin/env python3
"""Tiny OpenAI-compatible local STT server skeleton for Kwispr.

This server intentionally returns a stub transcript. It exists to lock the
HTTP contract and wiring before a later slice adds real local inference.
"""
from __future__ import annotations

import argparse
import json
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

STUB_TEXT = "[stub transcript]"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


class LocalSTTHandler(BaseHTTPRequestHandler):
    server_version = "kwispr-local-stt/0.1"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path != "/v1/audio/transcriptions":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._handle_transcription()

    def _handle_transcription(self) -> None:
        content_type = self.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "expected multipart/form-data"})
            return

        content_length = self.headers.get("content-length")
        try:
            length = int(content_length or "0")
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid content-length"})
            return
        if length <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "empty request body"})
            return
        if length > MAX_UPLOAD_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "upload too large"})
            return

        body = self.rfile.read(length)
        try:
            fields, files = parse_multipart(content_type, body)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        model = fields.get("model", "")
        response_format = fields.get("response_format", "json")
        # Accepted for OpenAI compatibility and future inference routing.
        _language = fields.get("language")

        if "file" not in files:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing audio file field: file"})
            return
        if not model:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing model field"})
            return
        if response_format != "json":
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "only response_format=json is supported"})
            return

        self._send_json(HTTPStatus.OK, {"text": STUB_TEXT})

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the skeleton quiet by default; reverse proxies/supervisors can log.
        return


def parse_multipart(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, bytes]]:
    """Parse multipart/form-data using Python's stdlib email parser."""
    if "boundary=" not in content_type:
        raise ValueError("multipart boundary is missing")
    message_bytes = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n"
        "\r\n"
    ).encode("utf-8") + body
    message = BytesParser(policy=default).parsebytes(message_bytes)
    if not message.is_multipart():
        raise ValueError("invalid multipart body")

    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        filename = part.get_filename()
        if filename is not None:
            files[name] = payload
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset, errors="replace")
    return fields, files


def main() -> None:
    parser = argparse.ArgumentParser(description="Kwispr local STT server skeleton")
    parser.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=19650, help="bind port (default: 19650)")
    args = parser.parse_args()

    httpd = ThreadingHTTPServer((args.host, args.port), LocalSTTHandler)
    print(f"kwispr local STT stub listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
