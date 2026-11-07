"""Minimal RFB (VNC) client: handshake, VNC auth, framebuffer updates, pointer and key events.

Protocol only. PNG screenshots, clicks, typing and settle detection belong in
``VncDesktop`` (``vnc.py``), which builds on :class:`RfbConnection`.

Spec: RFC 6143. Speaks RFB 3.8 with Raw and CopyRect encodings, over plain TCP
(x11vnc on 5900) or WebSocket (websockify on 6080, e.g. the OpenSandbox
``.../proxy/6080`` endpoint, which only forwards HTTP and WebSocket).

Not thread-safe: use one connection from one thread.
"""

from __future__ import annotations

import logging
import socket
import struct
import time
from contextlib import ExitStack

import numpy as np
from cryptography.hazmat.decrepit.ciphers.algorithms import TripleDES
from cryptography.hazmat.primitives.ciphers import Cipher, modes
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect as ws_connect

logger = logging.getLogger(__name__)

# Pointer button bits for RfbConnection.pointer(). Scrolling is a press + release of 8/16.
BUTTON_LEFT = 1
BUTTON_MIDDLE = 2
BUTTON_RIGHT = 4
BUTTON_SCROLL_UP = 8
BUTTON_SCROLL_DOWN = 16
BUTTON_SCROLL_LEFT = 32
BUTTON_SCROLL_RIGHT = 64

Rect = tuple[int, int, int, int]  # x, y, width, height

_SEC_NONE = 1
_SEC_VNC_AUTH = 2

_ENC_RAW = 0
_ENC_COPY_RECT = 1
_ENC_DESKTOP_SIZE = -223  # pseudo-encoding: the server announces a new screen size

# Client -> server message types
_SET_PIXEL_FORMAT = 0
_SET_ENCODINGS = 2
_FRAMEBUFFER_UPDATE_REQUEST = 3
_KEY_EVENT = 4
_POINTER_EVENT = 5
_CLIENT_CUT_TEXT = 6

# Server -> client message types
_FRAMEBUFFER_UPDATE = 0
_SET_COLOUR_MAP_ENTRIES = 1
_BELL = 2
_SERVER_CUT_TEXT = 3

# 32 bpp, depth 24, little-endian, true colour, 8 bits per channel at shifts 16/8/0.
# In memory each pixel is then B, G, R, X, which numpy reads directly.
_PIXEL_FORMAT = struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)

_NAMED_KEYSYMS = {
    "return": 0xFF0D, "enter": 0xFF0D, "\n": 0xFF0D,
    "tab": 0xFF09, "\t": 0xFF09, "iso_left_tab": 0xFE20,
    "backspace": 0xFF08,
    "escape": 0xFF1B, "esc": 0xFF1B,
    "delete": 0xFFFF, "del": 0xFFFF,
    "insert": 0xFF63,
    "home": 0xFF50, "end": 0xFF57,
    "left": 0xFF51, "up": 0xFF52, "right": 0xFF53, "down": 0xFF54,
    "pageup": 0xFF55, "pgup": 0xFF55, "pagedown": 0xFF56, "pgdn": 0xFF56,
    "space": 0x0020,
    "shift": 0xFFE1, "shift_l": 0xFFE1, "shift_r": 0xFFE2,
    "ctrl": 0xFFE3, "control": 0xFFE3, "ctrl_l": 0xFFE3, "ctrl_r": 0xFFE4,
    "alt": 0xFFE9, "alt_l": 0xFFE9, "alt_r": 0xFFEA, "altgr": 0xFE03,
    "super": 0xFFEB, "win": 0xFFEB, "meta": 0xFFE7,
    "menu": 0xFF67,
    **{f"f{i}": 0xFFBE + i - 1 for i in range(1, 13)},
}


def keysym(key: str) -> int:
    """X11 keysym for a key name (``"Return"``, ``"ctrl"``, ``"F5"``) or one character (``"a"``, ``"ż"``).

    Names are case-insensitive. Characters are case-sensitive: ``"A"`` is the keysym
    for a capital A, which the server types with Shift by itself.
    """
    named = _NAMED_KEYSYMS.get(key.lower()) if len(key) > 1 or key in "\n\t" else None
    if named is not None:
        return named
    if len(key) != 1:
        raise ValueError(f"unknown key name: {key!r}")
    code = ord(key)
    if code < 0x20 or 0x7F <= code < 0xA0:
        raise ValueError(f"no keysym for control character {key!r}")
    # Latin-1 keysyms equal the codepoint; everything else uses the Unicode keysym range.
    return code if code <= 0xFF else 0x01000000 | code


class RfbError(Exception):
    """Connection, authentication or protocol failure."""


class _TcpTransport:
    def __init__(self, host: str, port: int, timeout: float):
        self._timeout = timeout
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def send(self, data: bytes) -> None:
        self._sock.settimeout(self._timeout)
        try:
            self._sock.sendall(data)
        except OSError as e:
            raise RfbError(f"send failed: {e}") from e

    def recv(self, timeout: float) -> bytes | None:
        """Whatever data is available, or None if nothing arrived within ``timeout`` seconds."""
        self._sock.settimeout(timeout)  # 0 makes the socket non-blocking
        try:
            data = self._sock.recv(65536)
        except (TimeoutError, BlockingIOError):
            return None
        if not data:
            raise RfbError("server closed the connection")
        return data

    def close(self) -> None:
        self._sock.close()


class _WebSocketTransport:
    """RFB bytes in binary WebSocket messages. Message boundaries don't match RFB messages."""

    def __init__(self, url: str, headers: dict[str, str] | None, timeout: float):
        # websockets >= 17.1 wants connect() used as a context manager; the stack keeps it open.
        self._stack = ExitStack()
        self._ws = self._stack.enter_context(ws_connect(
            url,
            subprotocols=["binary"],  # what websockify expects (noVNC sends the same)
            additional_headers=headers,
            compression=None,
            max_size=None,
            open_timeout=timeout,
            ping_interval=None,
            proxy=None,  # don't route localhost through HTTP_PROXY from the environment
        ))

    def send(self, data: bytes) -> None:
        try:
            self._ws.send(data)
        except ConnectionClosed as e:
            raise RfbError(f"WebSocket closed: {e}") from e

    def recv(self, timeout: float) -> bytes | None:
        try:
            message = self._ws.recv(timeout=timeout)
        except TimeoutError:
            return None
        except ConnectionClosed as e:
            raise RfbError(f"WebSocket closed: {e}") from e
        return message if isinstance(message, bytes) else message.encode("latin-1")

    def close(self) -> None:
        self._stack.close()


def _open_transport(address: str, headers: dict[str, str] | None, timeout: float):
    if address.startswith(("ws://", "wss://")):
        return _WebSocketTransport(address, headers, timeout)
    if address.startswith("tcp://"):
        address = address.removeprefix("tcp://")
    elif "/" in address:
        # OpenSandbox endpoint like "127.0.0.1:46796/proxy/6080": routed by path, so WebSocket.
        return _WebSocketTransport(f"ws://{address}", headers, timeout)
    host, sep, port = address.rpartition(":")
    if not sep:
        host, port = address, "5900"
    return _TcpTransport(host, int(port), timeout)


def _vnc_auth_response(challenge: bytes, password: str) -> bytes:
    # VNC auth is DES-ECB over the 16-byte challenge. The key is the password cut or
    # zero-padded to 8 bytes, with the bit order of every key byte reversed.
    key = password.encode("latin-1")[:8].ljust(8, b"\0")
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in key)
    # TripleDES with the same key three times is single DES (encrypt-decrypt-encrypt).
    encryptor = Cipher(TripleDES(key * 3), modes.ECB()).encryptor()
    return encryptor.update(challenge) + encryptor.finalize()


class RfbConnection:
    """One RFB session: keeps a local copy of the remote screen and sends input events.

    Connecting does the handshake and reads the whole screen once. After that the
    framebuffer only changes inside :meth:`wait_for_update`. The server answers an
    incremental update request only once something on screen has changed, so
    ``wait_for_update`` returning None means "nothing changed within the timeout".

    Args:
        address: Where the VNC server is.
            - ``"host:port"`` or ``"tcp://host:port"``: plain TCP (port defaults to 5900).
            - ``"ws://host:port/path"`` or ``"wss://..."``: RFB over WebSocket (websockify).
            - An address with a path but no scheme (the OpenSandbox
              ``endpoint.endpoint``, e.g. ``"127.0.0.1:46796/proxy/6080"``) is treated as ``ws://``.
        password: VNC password; only the first 8 characters count. None if the server
            needs no authentication.
        headers: Extra HTTP headers for the WebSocket handshake (OpenSandbox
            ``endpoint.headers``).
        timeout: Seconds allowed to connect, and to receive a message once it has started.

    Example:
        with RfbConnection("127.0.0.1:46796/proxy/6080", password="secret") as conn:
            rgb = conn.frame()                         # (800, 1280, 3) uint8
            conn.pointer(640, 400, BUTTON_LEFT)        # press...
            conn.pointer(640, 400)                     # ...and release
            rects = conn.wait_for_update(timeout=1.5)  # None: nothing changed
    """

    def __init__(
        self,
        address: str,
        password: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ):
        self._timeout = timeout
        self._buf = bytearray()
        self._pending = False  # an update request the server hasn't answered yet
        self._needs_full = False  # the screen was resized, so the next request must be full
        self.name = ""
        self.server_cut_text: str | None = None  # last clipboard text the server sent
        try:
            self._transport = _open_transport(address, headers, timeout)
        except (OSError, WebSocketException) as e:
            raise RfbError(f"can't connect to {address}: {e}") from e
        try:
            self._handshake(password)
            self.refresh()
        except BaseException:
            self._transport.close()
            raise
        logger.info("Connected to %s: %r, %dx%d", address, self.name, self.width, self.height)

    @property
    def width(self) -> int:
        return self._fb.shape[1]

    @property
    def height(self) -> int:
        return self._fb.shape[0]

    @property
    def framebuffer(self) -> np.ndarray:
        """The live (height, width, 4) uint8 BGRX screen. Updated in place; don't modify it."""
        return self._fb

    def frame(self) -> np.ndarray:
        """A (height, width, 3) uint8 RGB copy of the screen."""
        return self._fb[:, :, 2::-1].copy()

    def refresh(self) -> None:
        """Re-read the whole screen. Blocks until a full update has arrived."""
        self._request_update(incremental=False)
        deadline = time.monotonic() + self._timeout
        while True:
            if not self._wait_readable(deadline - time.monotonic()):
                raise RfbError("timed out waiting for a full framebuffer update")
            rects = self._read_message()
            if rects is None:
                continue
            if self._needs_full:  # resized mid-refresh: ask again for the new size
                self._request_update(incremental=False)
            elif sum(w * h for _, _, w, h in rects) >= self.width * self.height:
                return
            # Otherwise it was the answer to an older incremental request; keep reading.

    def sync(self) -> None:
        """Block until the server has read every message sent before this call.

        RFB doesn't acknowledge input events. But the server handles client messages in
        order and must answer a non-incremental update request, so asking for one pixel
        and waiting for the update that contains it works as a barrier. It costs a few
        bytes instead of the full frame :meth:`refresh` reads.

        The pixel is the bottom-right corner, which is rarely redrawn: an answer to an
        older incremental request that happens to cover it would end the wait early.
        """
        if self._needs_full:  # after a resize only a full update is safe
            self.refresh()
            return
        x, y = self.width - 1, self.height - 1
        self._send(struct.pack(">BBHHHH", _FRAMEBUFFER_UPDATE_REQUEST, 0, x, y, 1, 1))
        self._pending = True
        deadline = time.monotonic() + self._timeout
        while True:
            if not self._wait_readable(deadline - time.monotonic()):
                raise RfbError("timed out waiting for the sync update")
            rects = self._read_message()
            if rects is None:
                continue
            if self._needs_full:
                self.refresh()
                return
            if any(rx <= x < rx + rw and ry <= y < ry + rh for rx, ry, rw, rh in rects):
                return
            # Otherwise it was the answer to an older incremental request; keep reading.

    def wait_for_update(self, timeout: float) -> list[Rect] | None:
        """Wait up to ``timeout`` seconds for the screen to change, and apply the change.

        Sends an incremental update request if none is outstanding. Returns the
        rectangles (x, y, width, height) that the update covered, or None if nothing
        changed in time. ``timeout=0`` only polls. A request that timed out stays
        outstanding, so the next call gets the change whenever it happens.

        A returned rectangle means the server re-sent that area, not that every pixel
        in it changed. Compare against an earlier ``frame()`` if you need the real diff.
        """
        if not self._pending:
            self._request_update(incremental=not self._needs_full)
        deadline = time.monotonic() + timeout
        while True:
            if not self._wait_readable(deadline - time.monotonic()):
                return None
            rects = self._read_message()
            if rects is not None:
                return rects

    def pointer(self, x: int, y: int, buttons: int = 0) -> None:
        """Move the pointer to pixel (x, y) with ``buttons`` (``BUTTON_*`` bits) held down.

        The state is absolute: ``buttons=0`` releases everything. Coordinates are
        clamped to the screen.
        """
        x = min(max(int(x), 0), self.width - 1)
        y = min(max(int(y), 0), self.height - 1)
        self._send(struct.pack(">BBHH", _POINTER_EVENT, buttons, x, y))

    def key(self, sym: int, down: bool) -> None:
        """Press (``down=True``) or release one key by keysym; see :func:`keysym`."""
        self._send(struct.pack(">BBxxI", _KEY_EVENT, int(down), sym))

    def cut_text(self, text: str, encoding: str = "latin-1") -> None:
        """Set the server's clipboard. RFB defines this as Latin-1 only.

        Some servers accept other encodings (pass ``encoding="utf-8"`` to try); with
        Latin-1, text outside it raises UnicodeEncodeError.
        """
        data = text.encode(encoding)
        self._send(struct.pack(">B3xI", _CLIENT_CUT_TEXT, len(data)) + data)

    def close(self) -> None:
        self._transport.close()

    def __enter__(self) -> RfbConnection:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- handshake ---

    def _handshake(self, password: str | None) -> None:
        version = self._read(12)
        if not version.startswith(b"RFB ") or version[7:8] != b".":
            raise RfbError(f"not an RFB server (got {version!r})")
        if (int(version[4:7]), int(version[8:11])) < (3, 8):
            raise RfbError(f"server speaks {version.strip().decode()}, only RFB 3.8 is supported")
        self._send(b"RFB 003.008\n")

        (count,) = self._unpack(">B")
        if count == 0:
            raise RfbError(f"server refused the connection: {self._read_reason()}")
        types = self._read(count)
        if password is not None and _SEC_VNC_AUTH in types:
            security = _SEC_VNC_AUTH
        elif _SEC_NONE in types:
            security = _SEC_NONE
        elif _SEC_VNC_AUTH in types:
            raise RfbError("server requires a VNC password")
        else:
            raise RfbError(f"no supported security type in {list(types)}")
        self._send(bytes([security]))
        if security == _SEC_VNC_AUTH:
            self._send(_vnc_auth_response(self._read(16), password))
        (result,) = self._unpack(">I")
        if result != 0:
            raise RfbError(f"authentication failed: {self._read_reason()}")

        self._send(b"\x01")  # ClientInit, shared: other viewers (noVNC) stay connected
        width, height = self._unpack(">HH")
        self._read(16)  # the server's pixel format; replaced by ours below
        (name_length,) = self._unpack(">I")
        self.name = self._read(name_length).decode("utf-8", errors="replace")
        self._fb = np.zeros((height, width, 4), np.uint8)

        self._send(struct.pack(">B3x", _SET_PIXEL_FORMAT) + _PIXEL_FORMAT)
        encodings = (_ENC_COPY_RECT, _ENC_RAW, _ENC_DESKTOP_SIZE)
        self._send(struct.pack(f">BxH{len(encodings)}i", _SET_ENCODINGS, len(encodings), *encodings))

    def _read_reason(self) -> str:
        (length,) = self._unpack(">I")
        return self._read(length).decode("utf-8", errors="replace")

    # --- server messages ---

    def _request_update(self, incremental: bool) -> None:
        self._send(struct.pack(
            ">BBHHHH", _FRAMEBUFFER_UPDATE_REQUEST, int(incremental), 0, 0, self.width, self.height
        ))
        self._pending = True
        if not incremental:
            self._needs_full = False

    def _read_message(self) -> list[Rect] | None:
        """Read one server message. Returns the updated rectangles for a framebuffer update."""
        (message_type,) = self._unpack(">B")
        if message_type == _FRAMEBUFFER_UPDATE:
            return self._read_framebuffer_update()
        if message_type == _SET_COLOUR_MAP_ENTRIES:  # not used in true colour; skip it
            _, count = self._unpack(">xHH")
            self._read(6 * count)
        elif message_type == _BELL:
            pass
        elif message_type == _SERVER_CUT_TEXT:
            (length,) = self._unpack(">3xI")
            self.server_cut_text = self._read(length).decode("latin-1")
        else:
            raise RfbError(f"unknown server message type {message_type}")
        return None

    def _read_framebuffer_update(self) -> list[Rect]:
        (count,) = self._unpack(">xH")
        rects = []
        for _ in range(count):
            x, y, w, h, encoding = self._unpack(">HHHHi")
            if encoding == _ENC_RAW:
                pixels = np.frombuffer(self._read(w * h * 4), np.uint8).reshape(h, w, 4)
                self._fb[y : y + h, x : x + w] = pixels
            elif encoding == _ENC_COPY_RECT:
                src_x, src_y = self._unpack(">HH")
                self._fb[y : y + h, x : x + w] = self._fb[src_y : src_y + h, src_x : src_x + w].copy()
            elif encoding == _ENC_DESKTOP_SIZE:
                logger.info("Screen resized to %dx%d", w, h)
                self._fb = np.zeros((h, w, 4), np.uint8)
                self._needs_full = True
                x, y = 0, 0
            else:
                raise RfbError(f"server sent unrequested encoding {encoding}")
            rects.append((x, y, w, h))
        self._pending = False
        return rects

    # --- byte stream ---

    def _send(self, data: bytes) -> None:
        self._transport.send(data)

    def _fill(self, timeout: float) -> bool:
        data = self._transport.recv(max(timeout, 0.0))
        if data is None:
            return False
        self._buf += data
        return True

    def _wait_readable(self, timeout: float) -> bool:
        """True once at least one byte is buffered, False if none arrived within ``timeout``."""
        return bool(self._buf) or self._fill(timeout)

    def _read(self, n: int) -> bytes:
        deadline = time.monotonic() + self._timeout
        while len(self._buf) < n:
            if not self._fill(deadline - time.monotonic()):
                raise RfbError(f"timed out waiting for {n - len(self._buf)} more bytes")
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _unpack(self, fmt: str) -> tuple:
        return struct.unpack(fmt, self._read(struct.calcsize(fmt)))
