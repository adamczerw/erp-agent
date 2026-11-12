import re
import subprocess
import time
from collections.abc import Callable
from urllib.parse import urlparse

CHROME_FLAGS = [
    "--window-size=1280,800",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-notifications",
    "--disable-infobars",
    "--disable-session-crashed-bubble",
    "--no-sandbox",
    "--test-type",
    "--disable-dev-shm-usage",
]


class BrowserError(Exception):
    pass


class Browser:
    def __init__(
        self,
        container: str | None = None,
        url: str = "http://host.docker.internal/",
        user: str = "desktop",
        display: str = ":0",
        timeout: float = 30.0,
    ):
        self.url = url
        self.user = user
        self.display = display
        self.timeout = timeout
        self.container = container or self._find_sandbox()

    def restart(self, url: str | None = None) -> None:
        url = url or self.url
        self._exec("pkill", "-x", "chrome", check=False)
        self._wait_for(lambda: self._exec("pgrep", "-x", "-r", "D,R,S,T", "chrome", check=False).returncode != 0, "Chrome to exit")
        self._exec("google-chrome", f"--app={url}", *CHROME_FLAGS, detach=True)
        host = urlparse(url).hostname
        self._wait_for(lambda: self._loaded_window(host) == self._active_window(), "the page to load and get focus")

    def _loaded_window(self, host: str) -> int | None:
        tree = self._exec("xwininfo", "-root", "-tree").stdout
        for window_id, title in re.findall(rf'(0x[0-9a-f]+) "(.*)": \("{re.escape(host)}', tree):
            if not title.startswith(host):
                return int(window_id, 16)
        return None

    def _active_window(self) -> int | None:
        match = re.search(r"0x[0-9a-f]+", self._exec("xprop", "-root", "_NET_ACTIVE_WINDOW").stdout)
        return int(match.group(), 16) if match else None

    def _wait_for(self, condition: Callable[[], bool], what: str) -> None:
        deadline = time.monotonic() + self.timeout
        while not condition():
            if time.monotonic() > deadline:
                raise BrowserError(f"Timed out after {self.timeout} s waiting for {what}")
            time.sleep(0.2)

    def _exec(self, *args: str, check: bool = True, detach: bool = False) -> subprocess.CompletedProcess:
        options = ["-u", self.user, "-e", f"DISPLAY={self.display}"] + (["-d"] if detach else [])
        return _run(["docker", "exec", *options, self.container, *args], check, self.timeout)

    def _find_sandbox(self) -> str:
        names = _run(["docker", "ps", "--filter", "name=sandbox-", "--format", "{{.Names}}"], True, self.timeout)
        found = names.stdout.split()
        if len(found) != 1:
            raise BrowserError(f"Expected one sandbox container, found {found}")
        return found[0]


def _run(cmd: list[str], check: bool, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, encoding="utf-8", check=check, timeout=timeout)
    except subprocess.CalledProcessError as e:
        raise BrowserError(f"{' '.join(cmd[:4])} exited with {e.returncode}: {e.stderr.strip()}") from e
    except subprocess.TimeoutExpired as e:
        raise BrowserError(f"{' '.join(cmd[:4])} timed out after {timeout} s") from e
