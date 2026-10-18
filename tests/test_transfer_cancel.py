"""Tests for host-side transfer cancel (Ctrl-X) and async-worker safety."""

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rc2014bridge.link import CAN, SerialLink

from fakes import FakeSerial


def _scratch(name: str = "hardware_info.json") -> str:
    return os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), name)


def _make_link() -> tuple[SerialLink, FakeSerial]:
    fake = FakeSerial()
    with patch("serial.Serial", return_value=fake):
        link = SerialLink("/dev/fake", hw_info_file=_scratch())
    link._system_state = "cpm"
    return link, fake


class TestRequestCancel(unittest.TestCase):
    def setUp(self):
        self.link, self.fake = _make_link()
        self.addCleanup(self.link.close)

    def test_request_cancel_sets_flag_and_sends_can_can(self):
        self.assertFalse(self.link._transfer_cancel.is_set())
        self.link.request_cancel()
        self.assertTrue(self.link._transfer_cancel.is_set())
        self.assertTrue(self.fake.written.endswith(bytes([CAN, CAN])))

    def test_xq_get_returns_can_once_cancelled(self):
        self.link._transfer_cancel.set()
        self.assertEqual(self.link._xq_get(timeout=0.01), CAN)


def _cancel_after(link: SerialLink, delay: float = 1.2) -> threading.Timer:
    """Simulate the GUI: Ctrl-X lands while the transfer is mid-wait."""
    timer = threading.Timer(delay, link.request_cancel)
    timer.start()
    return timer


class TestCancelEndsTransfersPromptly(unittest.TestCase):
    def setUp(self):
        self.link, self.fake = _make_link()
        self.addCleanup(self.link.close)

    def test_xmodem_send_aborts_without_handshake_wait(self):
        # No receiver will ever poke 'C' - without the cancel check this
        # would sit in the 30s handshake wait.
        path = os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "F.COM")
        with open(path, "wb") as f:
            f.write(b"\x00" * 300)

        timer = _cancel_after(self.link)
        start = time.monotonic()
        res = self.link.xmodem_send(path, handshake_timeout=30.0)
        elapsed = time.monotonic() - start
        timer.cancel()

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "cancelled by user")
        self.assertLess(elapsed, 10.0)

    def test_xmodem_receive_aborts_without_overall_timeout_wait(self):
        # No sender will ever arrive - without the cancel check this would
        # wait out the 120s overall timeout.
        path = os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "F.COM")
        timer = _cancel_after(self.link)
        start = time.monotonic()
        res = self.link.xmodem_receive(path, overall_timeout=120.0)
        elapsed = time.monotonic() - start
        timer.cancel()

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "cancelled by user")
        self.assertLess(elapsed, 10.0)

    def test_ymodem_receive_aborts_batch(self):
        dest = tempfile.mkdtemp(prefix="rc2014-test-")
        timer = _cancel_after(self.link)
        start = time.monotonic()
        res = self.link.ymodem_receive(dest, overall_timeout=600.0)
        elapsed = time.monotonic() - start
        timer.cancel()

        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "cancelled by user")
        self.assertLess(elapsed, 10.0)


class TestAsyncCallbackAlwaysFires(unittest.TestCase):
    def setUp(self):
        self.link, self.fake = _make_link()
        self.addCleanup(self.link.close)

    def test_callback_fires_when_transfer_raises(self):
        def _boom(path):
            raise OSError("port exploded")

        self.link.xmodem_send = _boom
        done = threading.Event()
        results = []

        def _cb(res):
            results.append(res)
            done.set()

        self.link.xmodem_send_async("whatever", callback=_cb)
        self.assertTrue(done.wait(timeout=5.0))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["ok"])
        self.assertIn("port exploded", results[0]["error"])

    def test_callback_fires_on_normal_completion(self):
        path = os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "F.COM")
        with open(path, "wb") as f:
            f.write(b"\x00" * 300)
        done = threading.Event()
        results = []
        self.link.xmodem_send_async(
            path, callback=lambda res: (results.append(res), done.set()))
        # Cancel mid-handshake-wait, as the GUI would on Ctrl-X.
        _cancel_after(self.link)
        self.assertTrue(done.wait(timeout=10.0))
        self.assertEqual(results[0]["error"], "cancelled by user")


if __name__ == "__main__":
    unittest.main()
