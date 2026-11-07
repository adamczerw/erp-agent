from erp_agent.desktop.vnc_connection import (
    RfbConnection,
    BUTTON_LEFT,
    BUTTON_MIDDLE,
    BUTTON_RIGHT,
    BUTTON_SCROLL_UP,
    BUTTON_SCROLL_DOWN,
    BUTTON_SCROLL_LEFT,
    BUTTON_SCROLL_RIGHT,
    keysym,
)
from PIL import Image
import io
import time
from dotenv import load_dotenv
import os
from typing import Literal
import logging

logger = logging.getLogger(__name__)

BUTTON_MAP = {
    "left": BUTTON_LEFT,
    "middle": BUTTON_MIDDLE,
    "right": BUTTON_RIGHT
}

SCROLL_MAP = {
    "up": BUTTON_SCROLL_UP,
    "down": BUTTON_SCROLL_DOWN,
    "left": BUTTON_SCROLL_LEFT,
    "right": BUTTON_SCROLL_RIGHT
}

# BUTTON_LEFT = 1
# BUTTON_MIDDLE = 2
# BUTTON_RIGHT = 4
# BUTTON_SCROLL_UP = 8
# BUTTON_SCROLL_DOWN = 16
# BUTTON_SCROLL_LEFT = 32
# BUTTON_SCROLL_RIGHT = 64

def _with_shift(key: str) -> str:
    """x11vnc lifts Shift to produce an unshifted keysym, so send the shifted one instead."""
    if key.lower() == "tab":
        return "iso_left_tab"
    return key.upper() if len(key) == 1 else key


class VncDesktop:

    def __init__(self, address: str, password: str, headers: dict[str, str] | None = None):
        self._address = address
        self._password = password
        self._headers = headers
        self.conn = RfbConnection(address, password, headers)

    def reconnect(self) -> None:
        """Replace the connection with a new one to the same server.

        Meant for recovery, so the old connection is closed without the final sync
        that ``close()`` does: it may already be dead. Raises ``RfbError`` if the
        server can't be reached; ``self.conn`` then still holds the closed old one.
        """
        try:
            self.conn.close()
        except Exception as e:
            logger.warning("Closing the old connection failed: %s", e)
        self.conn = RfbConnection(self._address, self._password, self._headers)

    def close(self) -> None:
        try:
            self.conn.sync()
        except Exception as e:
            logger.warning("Final refresh before close failed: %s", e)
        finally:
            self.conn.close()

    def __enter__(self):
        return self
    
    def __exit__(self, *exc):
        self.close()

    def screenshot(self) -> bytes:
        pil_img = Image.fromarray(self.conn.frame())
        buf = io.BytesIO()
        pil_img.save(buf, format="PNG")
        png_bytes = buf.getvalue()
        return png_bytes

    def wait_until_stable(
        self, first_change: float = 1.5, quiet: float = 0.5, cap: float = 8.0, min_area: int = 400
    ) -> None:
        start = time.monotonic()
        last_change = None
        while True:
            limit = start + first_change if last_change is None else last_change + quiet
            remaining = min(limit, start + cap) - time.monotonic()
            if remaining <= 0:
                if time.monotonic() >= start + cap:
                    logger.warning("Screen still changing after %.1f s", cap)
                return
            rects = self.conn.wait_for_update(remaining)
            if rects is None:
                return
            if sum(w * h for _, _, w, h in rects) >= min_area:
                last_change = time.monotonic()

    @property
    def width(self):
        return self.conn.width

    @property
    def height(self):
        return self.conn.height   
    
    
    def _press_release(self, x: int, y: int, button: Literal["left", "middle", "right"]) -> None:
        self.conn.pointer(x, y, 0)
        self.conn.pointer(x, y, BUTTON_MAP[button])
        self.conn.pointer(x, y, 0)


    def click(self, x: int, y: int, button: Literal["left", "middle", "right"]) -> None:
        self._press_release(x, y, button)
        self.conn.sync()


    def double_click(self, x: int, y: int) -> None:
        self._press_release(x, y, "left")
        self._press_release(x, y, "left")
        self.conn.sync()


    def type(self, text: str) -> None:
        keysyms = [keysym(character) for character in text]
        for ks in keysyms:
            self.conn.key(ks, down=True)
            self.conn.key(ks, down=False)
        self.conn.sync()


    def press(self, key: str, modifiers: list[str] | None = None) -> None:
        modifiers = modifiers or []
        if any(modifier.lower().startswith("shift") for modifier in modifiers):
            key = _with_shift(key)
        key_keysym = keysym(key)
        modifier_keysyms = [keysym(modifier) for modifier in modifiers]
        for ks in modifier_keysyms:
            self.conn.key(ks, down=True)
        self.conn.key(key_keysym, down=True)
        self.conn.key(key_keysym, down=False)
        for ks in reversed(modifier_keysyms):
            self.conn.key(ks, down=False)
        self.conn.sync()


    def scroll(self, x: int, y: int, direction: Literal["up", "down", "left", "right"], amount: int = 3) -> None:
        self.conn.pointer(x, y, 0)
        for _ in range(amount):
            self.conn.pointer(x, y, SCROLL_MAP[direction])
            self.conn.pointer(x, y, 0)
        self.conn.sync()


    def drag(self, x: int, y: int, end_x: int, end_y: int) -> None:
        self.conn.pointer(x, y, 0)
        self.conn.pointer(x, y, BUTTON_LEFT)
        self.conn.pointer(end_x, end_y, BUTTON_LEFT)
        self.conn.pointer(end_x, end_y, 0)
        self.conn.sync()


if __name__ == "__main__":
    load_dotenv()
    VNC_PASSWORD = os.environ["VNC_PASSWORD"]
    with VncDesktop("127.0.0.1:59462/proxy/6080", VNC_PASSWORD) as vncd:
        png_bytes = vncd.screenshot()
        print(vncd.monitor_width)
        print(vncd.monitor_height)
        with open("screenshot.png", "wb") as f:
            f.write(png_bytes)