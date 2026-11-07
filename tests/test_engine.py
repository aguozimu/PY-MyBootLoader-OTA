import queue
import time
import unittest

from ota_host.engine import OtaEngine
from ota_host.protocol import (
    ACK_CRC,
    GET_CRC,
    GET_MID,
    GET_STA,
    SET_CRC,
    SET_DAT,
    SET_END,
    SET_MID,
    SET_STA,
    ack_for,
    build_frame,
    parse_frame,
    status_record,
)


class FakeDeviceLink:
    """A synchronous protocol peer used for engine-level tests."""

    def __init__(self, device_crcs, final_crcs, program_id=0x0102, query_error_code=0, drop_first=False):
        self.frames = queue.Queue(maxsize=10000)
        self.is_open = True
        self.device_crcs = list(device_crcs)
        self.final_crcs = list(final_crcs)
        self.program_id = program_id
        self.page_buffers = []
        self.current_page = None
        self.page_count = 0
        self.host_acks = 0
        self.query_error_code = query_error_code
        self.drop_first = drop_first
        self.dropped_one = False
        self.get_crc_count = 0
        self.commands = []

    def write(self, raw):
        frame = parse_frame(raw)
        self.commands.append(frame.command_type)
        if frame.command_type == ACK_CRC:
            self.host_acks += 1
            return
        if self.drop_first and not self.dropped_one:
            self.dropped_one = True
            return
        self._put(ack_for(frame))
        if frame.command_type == GET_CRC:
            self.get_crc_count += 1
            end_error = self.query_error_code
            for index, crc in enumerate(self.device_crcs):
                self._put(build_frame(SET_CRC, index, crc.to_bytes(4, "little")))
            self._put(build_frame(SET_END, 0, status_record(0x06, 0xFF, 0xFF, end_error)))
        elif frame.command_type == GET_MID:
            self._put(build_frame(SET_MID, 0, self.program_id.to_bytes(4, "little")))
            self._put(build_frame(SET_END, 0, status_record(0x06, 0xFF, 0xFF, 0x00)))
        elif frame.command_type == GET_STA:
            self._put(build_frame(SET_STA, 0, status_record(0x06, 0x02, 0x40, 0x00)))
            self._put(build_frame(SET_END, 0, status_record(0x06, 0x02, 0x40, 0x00)))
        elif frame.command_type == SET_DAT:
            if frame.number == 0:
                self.current_page = bytearray()
                self.page_buffers.append(self.current_page)
                self.page_count += 1
            if self.current_page is not None:
                self.current_page.extend(frame.data)
        elif frame.command_type == SET_END:
            self._put(build_frame(SET_MID, 0, self.program_id.to_bytes(4, "little")))
            for index, crc in enumerate(self.final_crcs):
                self._put(build_frame(SET_CRC, index, crc.to_bytes(4, "little")))
            self._put(build_frame(SET_STA, 0, status_record(0x05, 0xFF, 0xFF, 0x00)))
            self._put(build_frame(SET_END, 0, status_record(0x05, 0xFF, 0xFF, 0x00)))

    def _put(self, raw):
        self.frames.put(parse_frame(raw), timeout=1.0)

    def close(self, reason=""):
        self.is_open = False


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = OtaEngine()

    def tearDown(self):
        self.engine.shutdown()

    def _wait_idle(self, timeout=8.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.engine.busy:
                return
            time.sleep(0.01)
        self.fail("engine did not become idle")

    def test_query_crc_updates_device_pages(self):
        crcs = [0x10000000 + index for index in range(16)]
        self.engine._link = FakeDeviceLink(crcs, crcs)
        self.engine.start_query("crc")
        self._wait_idle()
        state = self.engine.snapshot()
        self.assertEqual(state["task"]["result"], "success")
        self.assertEqual(state["device"]["crc_pages"], crcs)
        self.assertEqual(len(state["pages"]), 16)
        self.assertEqual(state["pages"][15]["device_crc"], crcs[15])

    def test_query_mid_and_status(self):
        crcs = [0x20000000 + index for index in range(16)]
        fake = FakeDeviceLink(crcs, crcs, program_id=0x0107)
        self.engine._link = fake
        self.engine.start_query("mid")
        self._wait_idle()
        self.assertEqual(self.engine.snapshot()["device"]["program_id"], 0x0107)

        self.engine.start_query("status")
        self._wait_idle()
        device = self.engine.snapshot()["device"]
        self.assertEqual(device["run_status"], 0x06)
        self.assertEqual(device["page"], 2)
        self.assertEqual(device["block"], 0x40)

    def test_upgrade_all_pages_equal_still_sends_control_page(self):
        firmware = bytes((index * 31) & 0xFF for index in range(15 * 1024))
        from ota_host.protocol import program_page_crcs

        local_crcs = program_page_crcs(firmware)
        fake = FakeDeviceLink(local_crcs + [0x12345678], local_crcs + [0x87654321], program_id=0x0108)
        self.engine._link = fake
        self.engine.start_upgrade(firmware, "equal.bin", 0x0108, pad=False)
        self._wait_idle()
        state = self.engine.snapshot()
        self.assertEqual(state["task"]["result"], "success", state["task"]["message"])
        self.assertEqual(fake.get_crc_count, 1)
        self.assertEqual(fake.page_count, 1)
        self.assertEqual(len(fake.page_buffers[0]), 1024)
        self.assertEqual(state["task"]["progress"], 100.0)

    def test_upgrade_queries_crc_before_any_setdat(self):
        firmware = bytes((index * 19 + 5) & 0xFF for index in range(15 * 1024))
        from ota_host.protocol import program_page_crcs

        local_crcs = program_page_crcs(firmware)
        fake = FakeDeviceLink(local_crcs + [0x11111111], local_crcs + [0x22222222], program_id=0x010B)
        self.engine._link = fake
        self.engine.start_upgrade(firmware, "fresh-crc.bin", 0x010B, pad=False)
        self._wait_idle()
        self.assertEqual(self.engine.snapshot()["task"]["result"], "success")
        self.assertEqual(fake.commands.count(GET_CRC), 1)
        self.assertLess(fake.commands.index(GET_CRC), fake.commands.index(SET_DAT))

    def test_upgrade_changed_pages_are_written(self):
        firmware = bytes((index * 13 + 7) & 0xFF for index in range(15 * 1024))
        from ota_host.protocol import program_page_crcs

        local_crcs = program_page_crcs(firmware)
        device_crcs = list(local_crcs)
        device_crcs[1] ^= 0xFFFFFFFF
        device_crcs[12] ^= 0xAAAAAAAA
        fake = FakeDeviceLink(device_crcs + [0x01020304], local_crcs + [0x05060708], program_id=0x0109)
        self.engine._link = fake
        self.engine.start_upgrade(firmware, "changed.bin", 0x0109, pad=False)
        self._wait_idle()
        state = self.engine.snapshot()
        self.assertEqual(state["task"]["result"], "success", state["task"]["message"])
        self.assertEqual(fake.get_crc_count, 1)
        self.assertEqual(fake.page_count, 3)
        self.assertTrue(all(len(page) == 1024 for page in fake.page_buffers))
        decisions = {page["index"]: page["decision"] for page in state["pages"]}
        self.assertEqual(decisions[1], "write")
        self.assertEqual(decisions[12], "write")
        self.assertEqual(decisions[0], "skip")

    def test_query_reports_remote_error_in_set_end(self):
        crcs = [0x30000000 + index for index in range(16)]
        self.engine._link = FakeDeviceLink(crcs, crcs, query_error_code=0x09)
        self.engine.start_query("crc")
        self._wait_idle()
        state = self.engine.snapshot()
        self.assertEqual(state["task"]["result"], "error")
        self.assertEqual(state["task"]["error_code"], 0x09)
        self.assertEqual(state["device"]["error_code"], 0x09)

    def test_missing_first_ack_is_retried(self):
        crcs = [0x40000000 + index for index in range(16)]
        fake = FakeDeviceLink(crcs, crcs, drop_first=True)
        self.engine._link = fake
        self.engine.start_query("crc")
        self._wait_idle()
        self.assertEqual(self.engine.snapshot()["task"]["result"], "success")
        self.assertTrue(fake.dropped_one)
        self.assertEqual(fake.get_crc_count, 1)

    def test_upgrade_final_crc_mismatch_is_failure(self):
        firmware = bytes((index * 7 + 3) & 0xFF for index in range(15 * 1024))
        from ota_host.protocol import program_page_crcs

        local_crcs = program_page_crcs(firmware)
        bad_final = list(local_crcs)
        bad_final[5] ^= 0xFFFFFFFF
        fake = FakeDeviceLink(local_crcs + [0x01020304], bad_final + [0x05060708], program_id=0x010A)
        self.engine._link = fake
        self.engine.start_upgrade(firmware, "bad-final.bin", 0x010A, pad=False)
        self._wait_idle()
        state = self.engine.snapshot()
        self.assertEqual(state["task"]["result"], "error")
        self.assertIn("最终页 CRC 不一致", state["task"]["message"])


if __name__ == "__main__":
    unittest.main()


