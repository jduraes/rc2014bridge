"""YMODEM (batch) protocol tests.

The board side of these transfers is RomWBW's ZMD 1.50: `zmd r` auto-detects
a YMODEM batch from the block-0 headers, `zmd sb <names>` sends one (the `B`
forces batch mode - plain `zmd s` with a single file silently drops to
XMODEM-1K and sends no block-0 header). The tests
here pin the details XMODEM never had: exact file length in the block-0
header (so downloads trim to their true size, even when a file ends in
0x1A), per-file block numbering restarting at 1, the EOT NAK->ACK dance,
and the all-NUL block 0 that terminates a batch.
"""

import hashlib
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from rc2014bridge.link import (ACK, BLOCK_SIZE, CAN, EOT, LONG_BLOCK_SIZE, NAK,
                               SOH, STX, SUB, SerialLink, _crc16)

from fakes import FakeSerial


def _scratch_hw_info() -> str:
    """A hardware-info path that doesn't exist, so tests never read or clobber
    the real hardware_info.json."""
    return os.path.join(tempfile.mkdtemp(prefix="rc2014-test-"), "hardware_info.json")


def _unpaced(link):
    """Pacing is a UART workaround, irrelevant to protocol correctness in a
    loopback - skipping it keeps these tests fast."""
    return patch.object(type(link), "_write_paced",
                        lambda self, data, chunk=8, delay=0.010: self._write_raw(data))


class ScriptedYmodemReceiver(threading.Thread):
    """Stands in for ZMD R receiving a YMODEM batch: pokes 'C' for each block
    0, validates the header (name + exact size), ACKs, pokes 'C' again for
    the data, ACKs each 1K block, NAKs the first EOT and ACKs the second, and
    finally expects the terminating all-NUL block 0. Records everything it
    saw for the test to assert on."""

    def __init__(self, port: FakeSerial):
        super().__init__(daemon=True)
        self.port = port
        self.headers: list[tuple[str, int]] = []   # (name, size) per file
        self.block_seqs: list[list[int]] = []      # data block numbers per file
        self.payloads: list[bytearray] = []        # raw payloads per file
        self.eot_nak_count = 0
        self.saw_null_header = False
        self.errors: list[str] = []
        self._stop = threading.Event()

    def run(self):
        cursor = 0
        state = "header"   # "header" = waiting for a block 0, "data" = file body
        next_poke = 0.0
        eot_naked = False
        deadline = time.time() + 20.0
        while not self._stop.is_set() and time.time() < deadline:
            if state == "header" and time.time() >= next_poke:
                # One poke per header request, re-poked only after a long
                # silence - like ZMD, not a 'C' flood the sender could
                # misread as a header response.
                self.port.feed(b"C")
                next_poke = time.time() + 3.0
            buf = self.port.written_since(cursor)
            if not buf:
                time.sleep(0.005)
                continue
            b0 = buf[0]
            if state == "data" and b0 == EOT:
                cursor += 1
                if not eot_naked:
                    eot_naked = True
                    self.eot_nak_count += 1
                    self.port.feed(bytes([NAK]))
                else:
                    eot_naked = False
                    self.port.feed(bytes([ACK]))
                    state = "header"
                    next_poke = 0.0
                continue
            if b0 not in (SOH, STX):
                cursor += 1  # stray byte
                continue
            size = BLOCK_SIZE if b0 == SOH else LONG_BLOCK_SIZE
            packet_len = 3 + size + 2
            if len(buf) < packet_len:
                time.sleep(0.005)
                continue
            blk, nblk = buf[1], buf[2]
            payload = bytes(buf[3:3 + size])
            crc = _crc16(payload)
            if (blk ^ nblk) != 0xFF or buf[3 + size:packet_len] != bytes([crc >> 8, crc & 0xFF]):
                self.errors.append(f"bad block framing/CRC at offset {cursor}")
            cursor += packet_len
            if state == "header":
                if b0 != SOH or blk != 0:
                    self.errors.append(f"header block must be SOH #0, got {b0:#x} #{blk}")
                if not any(payload):
                    self.saw_null_header = True
                    self.port.feed(bytes([ACK]))
                    return
                parts = payload.split(b"\0")
                self.headers.append((parts[0].decode("latin-1"), int(parts[1])))
                self.block_seqs.append([])
                self.payloads.append(bytearray())
                self.port.feed(bytes([ACK]))
                self.port.feed(b"C")   # ask for the data
                state = "data"
            else:
                self.block_seqs[-1].append(blk)
                self.payloads[-1].extend(payload)
                self.port.feed(bytes([ACK]))
        self.errors.append("receiver thread ended without seeing the null header")

    def stop(self):
        self._stop.set()


def _write_temp(data: bytes) -> str:
    with tempfile.NamedTemporaryFile(delete=False) as tf:
        tf.write(data)
        return tf.name


class TestYmodemSender(unittest.TestCase):
    def _link(self) -> tuple[SerialLink, FakeSerial]:
        fake = FakeSerial()
        with patch("serial.Serial", return_value=fake):
            link = SerialLink("/dev/fake", hw_info_file=_scratch_hw_info())
        self.addCleanup(link.close)
        return link, fake

    def test_batch_of_two_files(self):
        link, fake = self._link()
        receiver = ScriptedYmodemReceiver(fake)

        data1 = bytes((i * 7) & 0xFF for i in range(2500))  # 3 blocks (1K+1K+452)
        data2 = bytes((i * 3 + 1) & 0xFF for i in range(300))  # 1 short block
        paths = [_write_temp(data1), _write_temp(data2)]
        for p in paths:
            self.addCleanup(os.unlink, p)

        receiver.start()
        with _unpaced(link):
            res = link.ymodem_send([(paths[0], "FILE1.BIN"), (paths[1], "FILE2.BIN")],
                                   handshake_timeout=10.0)
        receiver.stop()

        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["file_count"], 2)
        self.assertEqual(res["bytes"], len(data1) + len(data2))
        self.assertFalse(receiver.errors, receiver.errors)

        # Block-0 headers carry the bare 8.3 name and the EXACT size.
        self.assertEqual(receiver.headers, [("FILE1.BIN", len(data1)),
                                            ("FILE2.BIN", len(data2))])
        # Data blocks are numbered from 1 per file.
        self.assertEqual(receiver.block_seqs[0], [1, 2, 3])
        self.assertEqual(receiver.block_seqs[1], [1])
        # Payloads arrive byte-exact; the short tail is SUB-padded to 1K.
        self.assertEqual(bytes(receiver.payloads[0])[:len(data1)], data1)
        self.assertEqual(bytes(receiver.payloads[0])[len(data1):],
                         bytes([SUB]) * (3 * LONG_BLOCK_SIZE - len(data1)))
        self.assertEqual(bytes(receiver.payloads[1])[:len(data2)], data2)
        # Every file got the NAK->ACK EOT dance, and the batch ended with
        # the terminating all-NUL block 0.
        self.assertEqual(receiver.eot_nak_count, 2)
        self.assertTrue(receiver.saw_null_header)

    def test_empty_file_sends_no_data_blocks(self):
        """A 0-byte file is header block 0 then EOT immediately."""
        link, fake = self._link()
        receiver = ScriptedYmodemReceiver(fake)

        path = _write_temp(b"")
        self.addCleanup(os.unlink, path)

        receiver.start()
        with _unpaced(link):
            res = link.ymodem_send([(path, "EMPTY.BIN")], handshake_timeout=10.0)
        receiver.stop()

        self.assertTrue(res.get("ok"), res)
        self.assertFalse(receiver.errors, receiver.errors)
        self.assertEqual(receiver.headers, [("EMPTY.BIN", 0)])
        self.assertEqual(receiver.block_seqs, [[]])
        self.assertTrue(receiver.saw_null_header)

    def test_handshake_timeout_cancels(self):
        link, fake = self._link()
        path = _write_temp(b"x" * 64)
        self.addCleanup(os.unlink, path)

        # Nobody ever pokes, so the handshake must give up - and must not
        # leave the receiver hanging: a CAN CAN pair goes out on the way back.
        with _unpaced(link):
            res = link.ymodem_send([(path, "FILE.BIN")], handshake_timeout=1.0)
        self.assertFalse(res["ok"])
        self.assertIn("handshake timeout", res["error"])
        self.assertIn(b"\x18\x18", bytes(fake.written))


class TestYmodemRoundTrip(unittest.TestCase):
    def _pair(self):
        port_a, port_b = FakeSerial.pair()
        with patch("serial.Serial", side_effect=[port_a, port_b]):
            sender = SerialLink("/dev/fake-a", hw_info_file=_scratch_hw_info())
            receiver = SerialLink("/dev/fake-b", hw_info_file=_scratch_hw_info())
        self.addCleanup(sender.close)
        self.addCleanup(receiver.close)
        return sender, receiver

    def test_batch_round_trip_exact_lengths(self):
        """Two files between cross-wired links must survive byte-exact at
        their EXACT lengths - the block-0 size trims the padding, which is
        the thing XMODEM cannot do. Sizes deliberately miss every block
        boundary, and one payload ends in 0x1A - hence strip_padding=False,
        the same convention as the XMODEM binary round trip."""
        sender, receiver = self._pair()

        data1 = bytes((i * 7 + (0x1A if i % 41 == 0 else 0)) & 0xFF
                      for i in range(2500))  # not a multiple of 1024 or 128
        data2 = bytes(range(200)) + b"\x1a"  # 201 bytes, ENDS in 0x1A
        paths = [_write_temp(data1), _write_temp(data2)]
        for p in paths:
            self.addCleanup(os.unlink, p)
        dest_dir = tempfile.mkdtemp(prefix="rc2014-ymodem-")

        recv_result = {}

        def _receive():
            recv_result.update(receiver.ymodem_receive(
                dest_dir, handshake_timeout=15.0, overall_timeout=40.0,
                strip_padding=False))

        with _unpaced(sender):
            thread = threading.Thread(target=_receive, daemon=True)
            thread.start()
            time.sleep(0.4)  # let the receiver start poking
            send_result = sender.ymodem_send(
                [(paths[0], "FILE1.BIN"), (paths[1], "FILE2.BIN")],
                handshake_timeout=15.0)
            thread.join(timeout=40.0)

        self.assertTrue(send_result.get("ok"), send_result)
        self.assertTrue(recv_result.get("ok"), recv_result)
        self.assertEqual(recv_result["file_count"], 2)
        for name, expected in (("FILE1.BIN", data1), ("FILE2.BIN", data2)):
            with open(os.path.join(dest_dir, name), "rb") as f:
                got = f.read()
            self.assertEqual(len(got), len(expected),
                             f"{name}: header size must trim block padding exactly")
            self.assertEqual(hashlib.sha256(got).hexdigest(),
                             hashlib.sha256(expected).hexdigest(), name)
        by_name = {f["name"]: f for f in recv_result["files"]}
        self.assertEqual(by_name["FILE2.BIN"]["bytes"], len(data2))


class ScriptedYmodemSender(threading.Thread):
    """Stands in for ZMD S sending a batch: answers a 'C' with each file's
    block-0 header, waits for the ACK, waits for 'C' again, sends 1K data
    blocks, and does the EOT NAK->ACK dance. ZMD's observed quirks are
    opt-in:

      drop_first_data_c: swallow the first 'C' after the header ACK without
        answering it (ZMD 1.50 opens the file after the ACK and misses a
        back-to-back 'C' - the receiver must re-poke to get data flowing).
      round_size: advertise 1K-rounded sizes like ZMD does (CP/M knows only
        128-byte record counts, so a 561-byte file is advertised as 1024).
      null_header=False: end without the terminating all-NUL block 0 (ZMD
        single-file send; the receiver's grace period must end the batch).
      abort_on_poke: ZMD 1.50's send-batch end-of-batch contract - once its
        file list is exhausted it stays armed, and the receiver's ONE final
        'C' is what releases it (answered with CAN CAN). Counts every poke
        in a ~6s window: exactly one is correct, zero means ZMD would hang,
        more than one means the receiver is nagging.
      end_with_can_can: end the batch by spontaneously sending CAN CAN right
        after the final EOT-ACK (ZMD's cosmetic "-- File transfer aborted").
    """

    def __init__(self, port: FakeSerial, files: list, drop_first_data_c: bool = False,
                 round_size: bool = False, null_header: bool = True,
                 abort_on_poke: bool = False, end_with_can_can: bool = False):
        super().__init__(daemon=True)
        self.port = port
        self.files = files            # [(wire_name, data_bytes)]
        self.drop_first_data_c = drop_first_data_c
        self.round_size = round_size
        self.null_header = null_header
        self.abort_on_poke = abort_on_poke
        self.end_with_can_can = end_with_can_can
        self.post_batch_pokes = 0
        self.dropped_c = False
        self.eot_nak_seen = False
        self.finished = False
        self.errors: list[str] = []
        self._stop = threading.Event()

    @staticmethod
    def _block(marker: int, num: int, payload: bytes) -> bytes:
        pkt = bytes([marker, num & 0xFF, (~num) & 0xFF]) + payload
        crc = _crc16(payload)
        return pkt + bytes([crc >> 8, crc & 0xFF])

    def _next_byte(self, cursor: int, deadline: float):
        while time.time() < deadline and not self._stop.is_set():
            buf = self.port.written_since(cursor)
            if buf:
                return buf[0], cursor + 1
            time.sleep(0.005)
        return None, cursor

    def _wait_for(self, wanted: set, cursor: int, deadline: float, what: str):
        """Consume bytes (receiver re-pokes and strays) until a wanted one."""
        while time.time() < deadline and not self._stop.is_set():
            b, cursor = self._next_byte(cursor, deadline)
            if b is None:
                self.errors.append(f"timeout waiting for {what}")
                return None, cursor
            if b in wanted:
                return b, cursor
        return None, cursor

    def run(self):
        cursor = 0
        deadline = time.time() + 40.0
        for name, data in self.files:
            advertised = ((len(data) + LONG_BLOCK_SIZE - 1) // LONG_BLOCK_SIZE
                          * LONG_BLOCK_SIZE) if self.round_size else len(data)
            header = self._block(SOH, 0, (name.encode("latin-1") + b"\0"
                                          + str(advertised).encode("ascii") + b"\0")
                                 .ljust(BLOCK_SIZE, b"\0"))
            blocks = [data[i:i + LONG_BLOCK_SIZE]
                      for i in range(0, len(data), LONG_BLOCK_SIZE)]
            if blocks and len(blocks[-1]) < LONG_BLOCK_SIZE:
                blocks[-1] += bytes([SUB]) * (LONG_BLOCK_SIZE - len(blocks[-1]))

            b, cursor = self._wait_for({ord("C")}, cursor, deadline, "header request")
            if b is None:
                return
            self.port.feed(header)
            b, cursor = self._wait_for({ACK}, cursor, deadline, "header ACK")
            if b is None:
                return
            if self.drop_first_data_c:
                b, cursor = self._wait_for({ord("C")}, cursor, deadline,
                                           "first data request (to drop)")
                if b is None:
                    return
                self.dropped_c = True   # ...and pointedly do NOT answer it
            b, cursor = self._wait_for({ord("C")}, cursor, deadline, "data request")
            if b is None:
                return
            for num, payload in enumerate(blocks, start=1):
                self.port.feed(self._block(STX, num, payload))
                b, cursor = self._wait_for({ACK}, cursor, deadline, f"block {num} ACK")
                if b is None:
                    return
            self.port.feed(bytes([EOT]))
            b, cursor = self._wait_for({ACK, NAK}, cursor, deadline, "EOT response")
            if b is None:
                return
            if b == NAK:
                self.eot_nak_seen = True
                self.port.feed(bytes([EOT]))
                b, cursor = self._wait_for({ACK}, cursor, deadline, "EOT ACK")
                if b is None:
                    return
        if self.end_with_can_can:
            self.port.feed(bytes([CAN, CAN]))
        elif self.abort_on_poke:
            # ZMD 1.50 send-batch contract: the file list is exhausted, so
            # the receiver's ONE final 'C' releases us - answer it with
            # CAN CAN. Keep counting pokes for the whole window so the test
            # can assert exactly one arrived.
            window = time.time() + 6.0
            while time.time() < window and not self._stop.is_set():
                b, cursor = self._wait_for({ord("C")}, cursor, window,
                                           "post-batch poke")
                if b != ord("C"):
                    break
                self.post_batch_pokes += 1
                if self.post_batch_pokes == 1:
                    self.port.feed(bytes([CAN, CAN]))
        elif self.null_header:
            b, cursor = self._wait_for({ord("C")}, cursor, deadline,
                                       "terminator request")
            if b is None:
                return
            self.port.feed(self._block(SOH, 0, bytes(BLOCK_SIZE)))
            b, cursor = self._wait_for({ACK}, cursor, deadline, "terminator ACK")
            if b is None:
                return
        self.finished = True

    def stop(self):
        self._stop.set()


class TestYmodemReceiverAgainstZmdQuirks(unittest.TestCase):
    def _link(self) -> tuple[SerialLink, FakeSerial]:
        fake = FakeSerial()
        with patch("serial.Serial", return_value=fake):
            link = SerialLink("/dev/fake", hw_info_file=_scratch_hw_info())
        self.addCleanup(link.close)
        return link, fake

    @staticmethod
    def _wait_finished(sender: "ScriptedYmodemSender", timeout: float = 10.0):
        """The receiver returns the moment it ACKs the last block; the sender
        thread still has to consume that ACK. Give it a moment rather than
        racing it."""
        deadline = time.time() + timeout
        while not sender.finished and time.time() < deadline:
            time.sleep(0.05)
        sender.stop()

    def test_missed_first_c_is_repoked(self):
        """Regression: ZMD opens the file after the header ACK and misses the
        back-to-back 'C', so data never started and every download died with
        CAN CAN ~45s later. The receiver must re-poke 'C' until data flows."""
        link, fake = self._link()
        payload = bytes((i * 5 + 1) & 0xFF for i in range(561))
        sender = ScriptedYmodemSender(fake, [("QUIRK.BIN", payload)],
                                      drop_first_data_c=True)
        dest_dir = tempfile.mkdtemp(prefix="rc2014-ymodem-")

        sender.start()
        res = link.ymodem_receive(dest_dir, handshake_timeout=10.0,
                                  overall_timeout=30.0, strip_padding=False)
        self._wait_finished(sender)

        self.assertTrue(sender.dropped_c, "the sender must have swallowed a first 'C'")
        self.assertFalse(sender.errors, sender.errors)
        self.assertTrue(sender.finished)
        self.assertTrue(res.get("ok"), res)
        self.assertTrue(sender.eot_nak_seen, "the EOT NAK->ACK dance must happen")
        with open(os.path.join(dest_dir, "QUIRK.BIN"), "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_zmd_rounded_size_and_strip_padding(self):
        """ZMD advertises 1K-rounded sizes (561-byte file -> '1024'), so the
        header trim alone leaves SUB padding: strip_padding=True must remove
        it, strip_padding=False must keep the record-granular bytes."""
        for strip, expected_len in ((True, 561), (False, 1024)):
            with self.subTest(strip_padding=strip):
                link, fake = self._link()
                payload = bytes((i * 5 + 1) & 0xFF for i in range(561))
                sender = ScriptedYmodemSender(fake, [("ROUND.BIN", payload)],
                                              round_size=True)
                dest_dir = tempfile.mkdtemp(prefix="rc2014-ymodem-")

                sender.start()
                res = link.ymodem_receive(dest_dir, handshake_timeout=10.0,
                                          overall_timeout=30.0, strip_padding=strip)
                self._wait_finished(sender)

                self.assertFalse(sender.errors, sender.errors)
                self.assertTrue(sender.finished)
                self.assertTrue(res.get("ok"), res)
                with open(os.path.join(dest_dir, "ROUND.BIN"), "rb") as f:
                    got = f.read()
                self.assertEqual(len(got), expected_len)
                self.assertEqual(got[:561], payload)
                if not strip:
                    self.assertEqual(got[561:], bytes([SUB]) * (1024 - 561))

    def test_single_file_without_null_header_ends_on_grace(self):
        """ZMD's single-file send terminates without a null header - the
        batch must end cleanly on the post-file silence."""
        link, fake = self._link()
        payload = b"short file\r\n"
        sender = ScriptedYmodemSender(fake, [("ONE.TXT", payload)], null_header=False)
        dest_dir = tempfile.mkdtemp(prefix="rc2014-ymodem-")

        sender.start()
        started = time.time()
        res = link.ymodem_receive(dest_dir, handshake_timeout=10.0,
                                  overall_timeout=30.0)
        elapsed = time.time() - started
        self._wait_finished(sender)

        self.assertTrue(sender.finished)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["file_count"], 1)
        self.assertGreaterEqual(elapsed, 4.0, "the ~5s grace should be what ends it")
        self.assertLess(elapsed, 20.0)
        with open(os.path.join(dest_dir, "ONE.TXT"), "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_one_final_c_lets_zmd_end_batch(self):
        """Regression: ZMD 1.50 S stays armed after its last file (until its
        own ~45-60s no-input timeout), so the receiver must send ONE final
        'C' after the final EOT-ACK - ZMD answers it with CAN CAN, which is
        the clean batch end. Exactly one poke: none hangs ZMD, periodic
        poking is wrong."""
        link, fake = self._link()
        payload = bytes((i * 5 + 1) & 0xFF for i in range(561))
        sender = ScriptedYmodemSender(fake, [("YMTEST1.TXT", payload)],
                                      abort_on_poke=True)
        dest_dir = tempfile.mkdtemp(prefix="rc2014-ymodem-")

        sender.start()
        started = time.time()
        res = link.ymodem_receive(dest_dir, handshake_timeout=10.0,
                                  overall_timeout=30.0, expected_files=1)
        elapsed = time.time() - started
        self._wait_finished(sender)

        self.assertEqual(sender.post_batch_pokes, 1,
                         "exactly one final 'C' must be poked after the last EOT-ACK")
        self.assertTrue(sender.finished)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["file_count"], 1)
        self.assertLess(elapsed, 15.0,
                        "ZMD's CAN CAN answer should end the batch well before the grace")
        with open(os.path.join(dest_dir, "YMTEST1.TXT"), "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_can_can_after_expected_count_is_clean_end(self):
        """ZMD 1.50 ends a send-batch with CAN CAN + "-- File transfer
        aborted" instead of a null header; with every requested file already
        received that is a clean batch end, not an error."""
        link, fake = self._link()
        payload = b"short file\r\n"
        sender = ScriptedYmodemSender(fake, [("YMTEST2.TXT", payload)],
                                      end_with_can_can=True)
        dest_dir = tempfile.mkdtemp(prefix="rc2014-ymodem-")

        sender.start()
        res = link.ymodem_receive(dest_dir, handshake_timeout=10.0,
                                  overall_timeout=30.0, expected_files=1)
        self._wait_finished(sender)

        self.assertTrue(sender.finished)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["file_count"], 1)
        with open(os.path.join(dest_dir, "YMTEST2.TXT"), "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_can_can_before_expected_count_is_still_an_error(self):
        """CAN CAN with fewer files than requested stays a cancellation."""
        link, fake = self._link()
        payload = b"short file\r\n"
        sender = ScriptedYmodemSender(fake, [("YMTEST2.TXT", payload)],
                                      end_with_can_can=True)
        dest_dir = tempfile.mkdtemp(prefix="rc2014-ymodem-")

        sender.start()
        res = link.ymodem_receive(dest_dir, handshake_timeout=10.0,
                                  overall_timeout=30.0, expected_files=2)
        self._wait_finished(sender)

        self.assertTrue(sender.finished)
        self.assertFalse(res.get("ok"), res)
        self.assertIn("cancelled", res["error"])
        self.assertEqual(len(res["files"]), 1)


class TestSettleAfterZmd(unittest.TestCase):
    """ZMD's exit can pause at a "-- Hit a key" prompt before returning to
    the OS (seen on the Zed Mini's ZMD 1.50). _settle_after_zmd must press a
    key to release it - but only when that text actually appears, so a ZMD
    without the pause is never poked."""

    def _link(self) -> tuple[SerialLink, FakeSerial]:
        fake = FakeSerial()
        with patch("serial.Serial", return_value=fake):
            link = SerialLink("/dev/fake", hw_info_file=_scratch_hw_info())
        self.addCleanup(link.close)
        time.sleep(0.4)  # let the constructor's initial CR probe pass
        return link, fake

    def test_hit_a_key_gets_pressed(self):
        link, fake = self._link()
        cursor = len(fake.written)

        def board():
            time.sleep(0.5)
            fake.feed(b"\r\n-- Hit a key")   # no newline: it sits waiting
            # ZMD consumes the CR the bridge sends, then ZPM3 prompts.
            deadline = time.time() + 8.0
            while time.time() < deadline:
                if b"\r" in bytes(fake.written)[cursor:]:
                    time.sleep(0.2)
                    fake.feed(b"\r\nA>")
                    return
                time.sleep(0.02)

        threading.Thread(target=board, daemon=True).start()
        ok = link._settle_after_zmd(grace=0.1, timeout=15.0)

        self.assertTrue(ok)
        self.assertIn(b"\r", bytes(fake.written)[cursor:],
                      "a CR must be sent once '-- Hit a key' appears")

    def test_no_hit_a_key_means_no_keypress(self):
        """A ZMD that returns straight to the prompt is never poked."""
        link, fake = self._link()
        cursor = len(fake.written)

        def board():
            time.sleep(0.8)
            fake.feed(b"\r\nA>")

        threading.Thread(target=board, daemon=True).start()
        ok = link._settle_after_zmd(grace=0.1, timeout=15.0)

        self.assertTrue(ok)
        self.assertNotIn(b"\r", bytes(fake.written)[cursor:])

    def test_silent_board_times_out_without_keypresses(self):
        """Nothing at all on the wire: no prompt, no 'Hit a key', so no CR
        either - just the timeout."""
        link, fake = self._link()
        cursor = len(fake.written)

        started = time.time()
        ok = link._settle_after_zmd(grace=0.1, timeout=3.0)
        elapsed = time.time() - started

        self.assertFalse(ok)
        self.assertLess(elapsed, 8.0)
        self.assertNotIn(b"\r", bytes(fake.written)[cursor:])


if __name__ == "__main__":
    unittest.main()
