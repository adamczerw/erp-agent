"""Tests for RfbConnection against a fake in-process RFB server (TCP and WebSocket)."""

from __future__ import annotations

import socket
import struct
import threading
import time

import numpy as np
import pytest
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from websockets.sync.server import serve

from erp_agent.desktop.vnc_connection import (
    BUTTON_LEFT,
    RfbConnection,
    RfbError,
    keysym,
)

WIDTH, HEIGHT = 8, 6
PASSWORD = "secret"
CHALLENGE = bytes(range(16))


def _expected_auth_response(password: str) -> bytes:
    key = password.encode()[:8].ljust(8, b"\0")
    key = bytes(sum(((b >> i) & 1) << (7 - i) for i in range(8)) for b in key)
    enc = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return enc.update(CHALLENGE) + enc.finalize()


def _recv_exact(conn: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            raise ConnectionError
        data += chunk
    return data


def _raw_rect(x: int, y: int, bgrx: np.ndarray) -> bytes:
    h, w, _ = bgrx.shape
    return struct.pack(">HHHHi", x, y, w, h, 0) + bgrx.astype(np.uint8).tobytes()


def _update(*rects: bytes) -> bytes:
    return struct.pack(">BxH", 0, len(rects)) + b"".join(rects)


class FakeServer:
    """RFB 3.8 server with VNC auth. Answers non-incremental update requests with the
    requested area of ``self.screen``; records every client message so the test can push
    other updates itself."""

    def __init__(self, password: str = PASSWORD):
        self.password = password
        self.screen = np.random.default_rng(0).integers(0, 256, (HEIGHT, WIDTH, 4), np.uint8)
        self.screen[..., 3] = 0
        self.messages: list[tuple[int, bytes]] = []
        self.auth_ok: bool | None = None
        self.conn: socket.socket | None = None
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def push(self, data: bytes) -> None:
        self.conn.sendall(data)

    def close(self) -> None:
        self._listener.close()
        if self.conn:
            self.conn.close()

    def wait_for(self, message_type: int, count: int = 1, timeout: float = 2.0) -> list[bytes]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [p for t, p in self.messages if t == message_type]
            if len(found) >= count:
                return found
            time.sleep(0.01)
        raise AssertionError(f"server didn't receive {count} message(s) of type {message_type}")

    def _serve(self) -> None:
        conn, _ = self._listener.accept()
        self.conn = conn
        try:
            conn.sendall(b"RFB 003.008\n")
            assert _recv_exact(conn, 12) == b"RFB 003.008\n"
            conn.sendall(b"\x01\x02")  # one security type: VNC auth
            assert _recv_exact(conn, 1) == b"\x02"
            conn.sendall(CHALLENGE)
            self.auth_ok = _recv_exact(conn, 16) == _expected_auth_response(self.password)
            if not self.auth_ok:
                reason = b"bad password"
                conn.sendall(struct.pack(">II", 1, len(reason)) + reason)
                return
            conn.sendall(struct.pack(">I", 0))
            assert _recv_exact(conn, 1) == b"\x01"  # shared
            name = b"fake"
            conn.sendall(struct.pack(">HH16xI", WIDTH, HEIGHT, len(name)) + name)
            while True:
                message_type = _recv_exact(conn, 1)[0]
                if message_type == 0:
                    payload = _recv_exact(conn, 19)
                elif message_type == 2:
                    head = _recv_exact(conn, 3)
                    payload = head + _recv_exact(conn, 4 * struct.unpack(">xH", head)[0])
                elif message_type == 3:
                    payload = _recv_exact(conn, 9)
                    if payload[0] == 0:
                        x, y, w, h = struct.unpack(">HHHH", payload[1:])
                        self.push(_update(_raw_rect(x, y, self.screen[y : y + h, x : x + w])))
                elif message_type in (4, 5):
                    payload = _recv_exact(conn, 7 if message_type == 4 else 5)
                elif message_type == 6:
                    head = _recv_exact(conn, 7)
                    payload = head + _recv_exact(conn, struct.unpack(">3xI", head)[0])
                else:
                    raise AssertionError(f"unexpected client message {message_type}")
                self.messages.append((message_type, payload))
        except (ConnectionError, OSError):
            pass


@pytest.fixture
def server():
    s = FakeServer()
    yield s
    s.close()


@pytest.fixture
def conn(server):
    with RfbConnection(f"127.0.0.1:{server.port}", password=PASSWORD, timeout=2.0) as c:
        yield c


def test_handshake_reads_full_screen(server, conn):
    assert server.auth_ok
    assert (conn.width, conn.height, conn.name) == (WIDTH, HEIGHT, "fake")
    np.testing.assert_array_equal(conn.frame(), server.screen[..., 2::-1])
    pixel_format = server.wait_for(0)[0]
    assert pixel_format[3:7] == bytes([32, 24, 0, 1])  # 32 bpp, depth 24, little-endian, true colour


def test_wrong_password_raises():
    server = FakeServer(password="other")
    with pytest.raises(RfbError, match="bad password"):
        RfbConnection(f"127.0.0.1:{server.port}", password=PASSWORD, timeout=2.0)
    server.close()


def test_missing_password_raises(server):
    with pytest.raises(RfbError, match="requires a VNC password"):
        RfbConnection(f"127.0.0.1:{server.port}", timeout=2.0)


def test_unreachable_server_raises_rfb_error():
    with socket.create_server(("127.0.0.1", 0)) as s:
        port = s.getsockname()[1]  # closed again before connecting, so the port refuses
    with pytest.raises(RfbError, match="can't connect"):
        RfbConnection(f"127.0.0.1:{port}", password=PASSWORD, timeout=2.0)
    with pytest.raises(RfbError, match="can't connect"):
        RfbConnection(f"ws://127.0.0.1:{port}", password=PASSWORD, timeout=2.0)


def test_idle_screen_times_out_with_one_outstanding_request(server, conn):
    assert conn.wait_for_update(timeout=0.1) is None
    assert conn.wait_for_update(timeout=0.0) is None
    requests = server.wait_for(3)
    incremental = [r for r in requests if r[0] == 1]
    assert len(incremental) == 1


def test_raw_and_copy_rect_updates(server, conn):
    conn.wait_for_update(timeout=0.0)
    patch = np.full((2, 3, 4), 7, np.uint8)
    copy_rect = struct.pack(">HHHHiHH", 4, 3, 3, 2, 1, 0, 0)  # copy (0,0) 3x2 to (4,3)
    server.push(_update(_raw_rect(1, 1, patch), copy_rect))
    assert conn.wait_for_update(timeout=1.0) == [(1, 1, 3, 2), (4, 3, 3, 2)]

    expected = server.screen.copy()
    expected[1:3, 1:4] = patch
    expected[3:5, 4:7] = expected[0:2, 0:3]
    np.testing.assert_array_equal(conn.framebuffer, expected)


def test_bell_and_cut_text_before_update_are_consumed(server, conn):
    conn.wait_for_update(timeout=0.0)
    text = "schowek".encode("latin-1")
    server.push(b"\x02" + struct.pack(">B3xI", 3, len(text)) + text + _update())
    assert conn.wait_for_update(timeout=1.0) == []
    assert conn.server_cut_text == "schowek"


def test_input_events(server, conn):
    conn.pointer(100, -5, BUTTON_LEFT)  # clamped to (WIDTH - 1, 0)
    conn.key(keysym("Return"), down=True)
    conn.cut_text("abc")
    assert server.wait_for(5)[0] == struct.pack(">BHH", BUTTON_LEFT, WIDTH - 1, 0)
    assert server.wait_for(4)[0] == struct.pack(">BxxI", 1, 0xFF0D)
    assert server.wait_for(6)[0] == struct.pack(">3xI", 3) + b"abc"


def test_sync_requests_one_pixel_after_input(server, conn):
    conn.pointer(1, 1, BUTTON_LEFT)
    conn.sync()
    pointer_index = next(i for i, (t, _) in enumerate(server.messages) if t == 5)
    t, request = server.messages[-1]
    assert t == 3 and pointer_index < len(server.messages) - 1
    assert request == struct.pack(">BHHHH", 0, WIDTH - 1, HEIGHT - 1, 1, 1)


def test_sync_skips_older_update_that_misses_its_pixel(server, conn):
    conn.wait_for_update(timeout=0.0)  # leaves an incremental request outstanding
    server.wait_for(3, count=2)
    patch = np.full((1, 1, 4), 9, np.uint8)
    server.push(_update(_raw_rect(0, 0, patch)))  # answers the old request, not the sync
    conn.sync()
    assert conn.framebuffer[0, 0, 0] == 9  # the older update was applied, not dropped
    assert server.wait_for(3, count=3)[-1] == struct.pack(">BHHHH", 0, WIDTH - 1, HEIGHT - 1, 1, 1)


def test_websocket_transport(server):
    # Relay like websockify: WebSocket on one side, the fake TCP server on the other.
    def relay(ws):
        backend = socket.create_connection(("127.0.0.1", server.port))

        def to_ws():
            try:
                while data := backend.recv(65536):
                    ws.send(data)
            except Exception:
                pass

        threading.Thread(target=to_ws, daemon=True).start()
        with backend:
            for message in ws:
                backend.sendall(message)

    with serve(relay, "127.0.0.1", 0) as ws_server:
        threading.Thread(target=ws_server.serve_forever, daemon=True).start()
        port = ws_server.socket.getsockname()[1]
        # No scheme but a path, like an OpenSandbox endpoint: treated as ws://.
        with RfbConnection(f"127.0.0.1:{port}/proxy/6080", password=PASSWORD, timeout=2.0) as c:
            np.testing.assert_array_equal(c.frame(), server.screen[..., 2::-1])
            assert c.wait_for_update(timeout=0.1) is None
        ws_server.shutdown()


@pytest.mark.parametrize(
    "key, expected",
    [
        ("a", 0x61),
        ("A", 0x41),
        ("ó", 0xF3),  # Latin-1
        ("ż", 0x0100017C),  # Unicode keysym range
        ("\n", 0xFF0D),
        ("Enter", 0xFF0D),
        ("CTRL", 0xFFE3),
        ("F5", 0xFFC2),
        ("space", 0x20),
    ],
)
def test_keysym(key, expected):
    assert keysym(key) == expected


def test_keysym_rejects_unknown():
    with pytest.raises(ValueError):
        keysym("nope")
    with pytest.raises(ValueError):
        keysym("\x01")
