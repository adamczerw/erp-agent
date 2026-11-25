"""Tests for VncDesktop with RfbConnection replaced by a recorder."""

from __future__ import annotations

import itertools
import time

import pytest

import erp_agent.desktop.vnc_desktop as vnc_desktop
from erp_agent.desktop.vnc_connection import RfbError
from erp_agent.desktop.vnc_desktop import VncDesktop


class RecordingConn:
    """Stands in for RfbConnection: remembers its arguments and whether it was closed."""

    created: list[RecordingConn] = []
    fail_next = False

    def __init__(self, address, password, headers):
        if RecordingConn.fail_next:
            raise RfbError("can't connect")
        self.args = (address, password, headers)
        self.closed = False
        self.close_raises = False
        RecordingConn.created.append(self)

    def close(self):
        self.closed = True
        if self.close_raises:
            raise OSError("socket already broken")


@pytest.fixture
def desktop(monkeypatch):
    RecordingConn.created = []
    RecordingConn.fail_next = False
    monkeypatch.setattr(vnc_desktop, "RfbConnection", RecordingConn)
    return VncDesktop("127.0.0.1:1234/proxy/6080", "pw", {"X-Token": "t"})


def test_reconnect_opens_a_new_connection_with_the_same_arguments(desktop):
    old = desktop.conn
    desktop.reconnect()
    assert old.closed
    assert desktop.conn is not old
    assert desktop.conn.args == old.args == ("127.0.0.1:1234/proxy/6080", "pw", {"X-Token": "t"})


def test_reconnect_survives_a_broken_old_connection(desktop):
    desktop.conn.close_raises = True
    desktop.reconnect()
    assert len(RecordingConn.created) == 2


def test_reconnect_failure_raises_rfb_error(desktop):
    old = desktop.conn
    RecordingConn.fail_next = True
    with pytest.raises(RfbError):
        desktop.reconnect()
    assert old.closed


class UpdatesConn:
    def __init__(self, areas):
        self.areas = iter(areas)

    def wait_for_update(self, timeout):
        area = next(self.areas, None)
        if area is None:
            time.sleep(timeout)
            return None
        time.sleep(0.01)
        return [(0, 0, area, 1)]


class TimedConn:
    """Delivers updates of the given areas at fixed times after creation."""

    def __init__(self, events):
        self.start = time.monotonic()
        self.events = list(events)  # (seconds after start, area)

    def wait_for_update(self, timeout):
        if self.events:
            at, area = self.events[0]
            delay = self.start + at - time.monotonic()
            if delay <= timeout:
                time.sleep(max(delay, 0))
                self.events.pop(0)
                return [(0, 0, area, 1)]
        time.sleep(timeout)
        return None


def _settle(areas, conn=None, **kwargs):
    desktop = VncDesktop.__new__(VncDesktop)
    desktop.conn = conn or UpdatesConn(areas)
    start = time.monotonic()
    desktop.wait_until_stable(**{"first_change": 0.1, "quiet": 0.05, "cap": 0.5, "min_wait": 0, **kwargs})
    return time.monotonic() - start


def test_settle_gives_up_when_nothing_changes():
    assert 0.1 <= _settle([]) < 0.2


def test_settle_waits_for_quiet_after_changes():
    assert 0.1 <= _settle([1000] * 10) < 0.25


def test_settle_ignores_tiny_changes():
    assert _settle(itertools.repeat(10)) < 0.2


def test_settle_stops_at_cap():
    assert 0.5 <= _settle(itertools.repeat(1000)) < 0.6


def test_settle_lasts_at_least_min_wait():
    assert 0.3 <= _settle([], min_wait=0.3) < 0.4


def test_settle_min_wait_catches_the_late_page_after_the_echo():
    # Typed text at once, the new page after the quiet period would have ended.
    conn = TimedConn([(0.01, 1000), (0.2, 100_000), (0.22, 50_000)])
    elapsed = _settle(None, conn=conn, min_wait=0.3)
    assert conn.events == []
    assert 0.3 <= elapsed < 0.4


def test_settle_without_min_wait_misses_the_late_page():
    conn = TimedConn([(0.01, 1000), (0.2, 100_000)])
    assert _settle(None, conn=conn) < 0.15
    assert len(conn.events) == 1
