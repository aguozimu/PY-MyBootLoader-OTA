import unittest

from ota_host.protocol import (
    ACK_CRC,
    CONTROL_PAGE_ADDRESS,
    PAGE_SIZE,
    PROGRAM_SIZE,
    FrameStreamParser,
    ProtocolError,
    ack_for,
    build_control_page,
    build_frame,
    build_update_plan,
    control_page_crc,
    parse_frame,
    program_page_crcs,
    stm32_crc_bytes,
)


FRAME_EXAMPLES = [
    ("GetCRC", 0x00, "00000000", 0x81E24AE9),
    ("GetMID", 0x00, "00000000", 0xD8F9B2C9),
    ("SetDat", 0x2A, "DEADBEEF", 0x7DD0ED91),
    ("GetSta", 0x00, "00000000", 0x6608C021),
    ("SetEro", 0x00, "07002A09", 0xF0AC15C7),
    ("SetEnd", 0x00, "05FFFF00", 0xB23AEBB4),
    ("AckCRC", 0x2A, "91EDD07D", 0x35A629D2),
    ("SetCRC", 0x00, "78563412", 0xBDAE4C83),
    ("SetMID", 0x00, "02010000", 0x27A01941),
    ("SetSta", 0x00, "02000000", 0x4B48AA75),
]


class ProtocolTests(unittest.TestCase):
    def test_documented_frame_crc_examples(self):
        for command_type, number, data_hex, expected_crc in FRAME_EXAMPLES:
            with self.subTest(command_type=command_type):
                raw = build_frame(command_type, number, bytes.fromhex(data_hex))
                self.assertEqual(len(raw), 16)
                self.assertEqual(raw[0], 0xA5)
                self.assertEqual(int.from_bytes(raw[12:16], "little"), expected_crc)
                parsed = parse_frame(raw)
                self.assertEqual(parsed.command_type, command_type)
                self.assertEqual(parsed.number, number)
                self.assertEqual(parsed.data.hex().upper(), data_hex)

    def test_ack_echoes_number_and_original_crc(self):
        original = parse_frame(build_frame("SetDat", 0x2A, bytes.fromhex("DEADBEEF")))
        ack = parse_frame(ack_for(original))
        self.assertEqual(ack.command_type, ACK_CRC)
        self.assertEqual(ack.number, 0x2A)
        self.assertEqual(ack.data_uint32, original.crc)

    def test_stream_parser_handles_fragmentation_and_coalescing(self):
        first = build_frame("GetCRC", 0, b"\x00" * 4)
        second = build_frame("GetMID", 0, b"\x00" * 4)
        stream = first + second
        parser = FrameStreamParser()
        received = []
        for start in range(0, len(stream), 3):
            received.extend(parser.feed(stream[start : start + 3]))
        self.assertEqual([frame.command_type for frame in received], ["GetCRC", "GetMID"])

    def test_stream_parser_resynchronizes_after_bad_frame(self):
        bad = b"\xA5" + b"Broken" + b"\x00" + b"\x00" * 4 + b"\x00" * 4
        good = build_frame("GetSta", 0, b"\x00" * 4)
        parser = FrameStreamParser()
        frames = parser.feed(b"\x00\x11" + bad + good)
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].command_type, "GetSta")
        self.assertGreaterEqual(parser.invalid_frames, 1)

    def test_crc_rejects_unaligned_input(self):
        with self.assertRaises(ProtocolError):
            stm32_crc_bytes(b"\x00\x01")

    def test_control_page_layout_and_self_crc(self):
        firmware = bytes((index * 17) & 0xFF for index in range(PROGRAM_SIZE))
        crcs = program_page_crcs(firmware)
        page = build_control_page(0x0102, 3, crcs)
        self.assertEqual(len(page), PAGE_SIZE)
        self.assertEqual(page[0:4], b"\x02\x01\x01\x03")
        self.assertEqual(page[4:8], (0).to_bytes(4, "little"))
        self.assertEqual(page[8:12], crcs[0].to_bytes(4, "little"))
        self.assertEqual(page[124:128], CONTROL_PAGE_ADDRESS.to_bytes(4, "little"))
        self.assertEqual(control_page_crc(page), int.from_bytes(page[128:132], "little"))
        self.assertEqual(page[132:], b"\xFF" * (PAGE_SIZE - 132))

    def test_update_plan_keeps_equal_crc_and_zeroes_changed_crc(self):
        local = [0x11111111 * (index + 1) & 0xFFFFFFFF for index in range(15)]
        device = local[:]
        device[2] = 0xAAAAAAAA
        device[7] = 0xBBBBBBBB
        plan = build_update_plan(local, device)
        self.assertEqual(plan["write_pages"], [2, 7])
        self.assertEqual(plan["data_area"], 2)
        self.assertEqual(plan["control_crcs"][2], 0)
        self.assertEqual(plan["control_crcs"][0], device[0])

    def test_update_plan_all_equal(self):
        local = list(range(1, 16))
        plan = build_update_plan(local, local)
        self.assertEqual(plan["write_pages"], [])
        self.assertEqual(plan["data_area"], 0xFF)


if __name__ == "__main__":
    unittest.main()
