"""OTA-PROTO-1.0 frame, CRC and control-page primitives."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

HEADER = 0xA5
FRAME_SIZE = 16
TYPE_SIZE = 6
DATA_SIZE = 4
PAGE_SIZE = 1024
PROGRAM_PAGE_COUNT = 15
TOTAL_PAGE_COUNT = 16
CONTROL_PAGE_INDEX = 15
PROGRAM_SIZE = PROGRAM_PAGE_COUNT * PAGE_SIZE
BLOCKS_PER_PAGE = PAGE_SIZE // DATA_SIZE
CONTROL_PAGE_ADDRESS = 0x3C00

CRC_POLY = 0x04C11DB7
CRC_INIT = 0xFFFFFFFF
UINT32_MASK = 0xFFFFFFFF

GET_CRC = "GetCRC"
GET_MID = "GetMID"
SET_DAT = "SetDat"
GET_STA = "GetSta"
SET_ERO = "SetEro"
SET_END = "SetEnd"
ACK_CRC = "AckCRC"
SET_CRC = "SetCRC"
SET_MID = "SetMID"
SET_STA = "SetSta"

COMMAND_TYPES = (
    GET_CRC,
    GET_MID,
    SET_DAT,
    GET_STA,
    SET_ERO,
    SET_END,
    ACK_CRC,
    SET_CRC,
    SET_MID,
    SET_STA,
)

RUN_STATUS = {
    0x00: "EMPTY",
    0x01: "FLASHABLE",
    0x02: "RECEIVING",
    0x03: "PAUSED",
    0x04: "VERIFYING",
    0x05: "VALID_BACKUP",
    0x06: "RUNNING",
    0x07: "ERROR",
}

RUN_STATUS_ZH = {
    0x00: "空 / 未初始化",
    0x01: "可刷入",
    0x02: "接收中",
    0x03: "已暂停",
    0x04: "校验中",
    0x05: "有效备用",
    0x06: "运行中",
    0x07: "错误",
}

ERROR_CODES = {
    0x00: ("NONE", "无错误"),
    0x01: ("INVALID_FRAME", "帧头、长度或字段格式错误"),
    0x02: ("UNKNOWN_COMMAND", "未定义命令类型"),
    0x03: ("CRC_ERROR", "帧 CRC 错误"),
    0x04: ("INVALID_STATE", "当前状态不允许该命令"),
    0x05: ("SEQUENCE_ERROR", "编号、页序或块序错误"),
    0x06: ("NO_WRITABLE_SLOT", "无可用目标槽"),
    0x07: ("FLASH_ERASE_FAIL", "Flash 擦除失败"),
    0x08: ("FLASH_PROGRAM_FAIL", "Flash 写入失败"),
    0x09: ("PAGE_CRC_MISMATCH", "页 CRC 不一致"),
    0x0A: ("CONTROL_PAGE_INVALID", "控制页无效"),
    0x0B: ("FIRMWARE_VERIFY_FAIL", "固件最终校验失败"),
    0x0C: ("INVALID_PROGRAM_ID", "程序 ID 无效"),
    0x0D: ("PEER_ABORT", "对端中止"),
    0x0E: ("TIMEOUT", "确认超时"),
    0x0F: ("BUSY", "设备忙"),
}


class ProtocolError(ValueError):
    """Raised when a frame or protocol value is invalid."""


@dataclass(frozen=True)
class Frame:
    command_type: str
    number: int
    data: bytes
    crc: int
    raw: bytes

    @property
    def data_uint32(self) -> int:
        return int.from_bytes(self.data, "little")

    def to_dict(self) -> dict:
        return {
            "type": self.command_type,
            "number": self.number,
            "data": self.data.hex(" ").upper(),
            "crc": f"{self.crc:08X}",
            "raw": self.raw.hex(" ").upper(),
        }


def _rol32(value: int, bits: int) -> int:
    return ((value << bits) | (value >> (32 - bits))) & UINT32_MASK


def stm32_crc_words(words: Iterable[int]) -> int:
    """STM32F1 hardware CRC: poly 0x04C11DB7, init 0xFFFFFFFF, no xorout."""
    crc = CRC_INIT
    for word in words:
        crc ^= word & UINT32_MASK
        for _ in range(32):
            if crc & 0x80000000:
                crc = ((crc << 1) ^ CRC_POLY) & UINT32_MASK
            else:
                crc = (crc << 1) & UINT32_MASK
    return crc


def stm32_crc_bytes(data: bytes) -> int:
    if len(data) % 4:
        raise ProtocolError(f"CRC input length must be divisible by 4, got {len(data)}")
    words = (int.from_bytes(data[i : i + 4], "little") for i in range(0, len(data), 4))
    return stm32_crc_words(words)


def frame_crc(frame_prefix: bytes) -> int:
    if len(frame_prefix) != 12:
        raise ProtocolError("frame prefix must be exactly 12 bytes")
    return stm32_crc_bytes(frame_prefix)


def build_frame(command_type: str, number: int, data: bytes) -> bytes:
    if not isinstance(command_type, str):
        raise ProtocolError("command type must be a string")
    try:
        type_bytes = command_type.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ProtocolError("command type must be ASCII") from exc
    if len(type_bytes) != TYPE_SIZE:
        raise ProtocolError(f"command type must be exactly {TYPE_SIZE} ASCII bytes")
    if not 0 <= number <= 0xFF:
        raise ProtocolError("number must be in 0x00..0xFF")
    if len(data) != DATA_SIZE:
        raise ProtocolError("data must be exactly 4 bytes")

    prefix = bytes([HEADER]) + type_bytes + bytes([number]) + data
    crc = frame_crc(prefix)
    return prefix + crc.to_bytes(4, "little")


def parse_frame(raw: bytes) -> Frame:
    if len(raw) != FRAME_SIZE:
        raise ProtocolError("frame must be exactly 16 bytes")
    if raw[0] != HEADER:
        raise ProtocolError("invalid frame header")
    if not all(0x20 <= byte <= 0x7E for byte in raw[1:7]):
        raise ProtocolError("command type is not printable ASCII")
    expected = int.from_bytes(raw[12:16], "little")
    actual = frame_crc(raw[:12])
    if actual != expected:
        raise ProtocolError(f"CRC mismatch: expected {expected:08X}, calculated {actual:08X}")
    return Frame(
        command_type=raw[1:7].decode("ascii"),
        number=raw[7],
        data=raw[8:12],
        crc=actual,
        raw=raw,
    )


def ack_for(frame: Frame) -> bytes:
    if frame.command_type == ACK_CRC:
        raise ProtocolError("AckCRC must not acknowledge another AckCRC")
    return build_frame(ACK_CRC, frame.number, frame.crc.to_bytes(4, "little"))


def status_record(run_status: int, page: int, block: int, error_code: int) -> bytes:
    for name, value in (("run_status", run_status), ("page", page), ("block", block), ("error_code", error_code)):
        if not 0 <= value <= 0xFF:
            raise ProtocolError(f"{name} must fit in one byte")
    return bytes([run_status, page, block, error_code])


def parse_status(data: bytes) -> dict:
    if len(data) != DATA_SIZE:
        raise ProtocolError("status record must be 4 bytes")
    run_status, page, block, error_code = data
    return {
        "run_status": run_status,
        "run_status_name": RUN_STATUS.get(run_status, "RESERVED"),
        "run_status_zh": RUN_STATUS_ZH.get(run_status, "保留状态"),
        "page": page,
        "block": block,
        "error_code": error_code,
        "error_name": ERROR_CODES.get(error_code, ("RESERVED", "保留错误"))[0],
        "error_zh": ERROR_CODES.get(error_code, ("RESERVED", "保留错误"))[1],
    }


class FrameStreamParser:
    """A byte stream parser that searches for 0xA5 and validates fixed frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.invalid_frames = 0
        self.discarded_bytes = 0

    def feed(self, data: bytes) -> list[Frame]:
        if data:
            self._buffer.extend(data)
        frames: list[Frame] = []
        while True:
            header_index = self._buffer.find(HEADER)
            if header_index < 0:
                self.discarded_bytes += len(self._buffer)
                self._buffer.clear()
                break
            if header_index:
                self.discarded_bytes += header_index
                del self._buffer[:header_index]
            if len(self._buffer) < FRAME_SIZE:
                break
            candidate = bytes(self._buffer[:FRAME_SIZE])
            try:
                frame = parse_frame(candidate)
            except ProtocolError:
                self.invalid_frames += 1
                self.discarded_bytes += 1
                del self._buffer[0]
                continue
            frames.append(frame)
            del self._buffer[:FRAME_SIZE]
        return frames


def program_page_crcs(firmware: bytes) -> list[int]:
    if len(firmware) != PROGRAM_SIZE:
        raise ProtocolError(f"firmware must be exactly {PROGRAM_SIZE} bytes")
    return [stm32_crc_bytes(firmware[i : i + PAGE_SIZE]) for i in range(0, PROGRAM_SIZE, PAGE_SIZE)]


def build_control_page(
    program_id: int,
    data_area: int,
    page_crcs: list[int],
    run_status: int = 0x01,
) -> bytes:
    """Build the 1 KiB control page and fill Page16.CRC last."""
    if not 0x0001 <= program_id <= 0xFFFF:
        raise ProtocolError("program ID must be in 0x0001..0xFFFF")
    if not 0 <= run_status <= 0xFF:
        raise ProtocolError("run status must fit in one byte")
    if not 0 <= data_area <= 0xFF:
        raise ProtocolError("data area must fit in one byte")
    if len(page_crcs) != PROGRAM_PAGE_COUNT:
        raise ProtocolError("exactly 15 program-page CRCs are required")

    page = bytearray(b"\xFF" * PAGE_SIZE)
    page[0:2] = program_id.to_bytes(2, "little")
    page[2] = run_status
    page[3] = data_area

    for index, crc in enumerate(page_crcs):
        entry_offset = 4 + 8 * index
        page[entry_offset : entry_offset + 4] = (index * PAGE_SIZE).to_bytes(4, "little")
        page[entry_offset + 4 : entry_offset + 8] = (crc & UINT32_MASK).to_bytes(4, "little")

    page16_offset = 4 + 8 * CONTROL_PAGE_INDEX
    page[page16_offset : page16_offset + 4] = CONTROL_PAGE_ADDRESS.to_bytes(4, "little")
    page[page16_offset + 4 : page16_offset + 8] = b"\x00\x00\x00\x00"

    control_crc = stm32_crc_bytes(bytes(page))
    page[page16_offset + 4 : page16_offset + 8] = control_crc.to_bytes(4, "little")
    return bytes(page)


def control_page_crc(page: bytes) -> int:
    if len(page) != PAGE_SIZE:
        raise ProtocolError("control page must be exactly 1024 bytes")
    copy = bytearray(page)
    copy[128:132] = b"\x00\x00\x00\x00"
    return stm32_crc_bytes(bytes(copy))


def build_update_plan(local_crcs: list[int], device_crcs: list[int]) -> dict:
    if len(local_crcs) != PROGRAM_PAGE_COUNT or len(device_crcs) < PROGRAM_PAGE_COUNT:
        raise ProtocolError("page CRC lists must contain at least 15 entries")
    write_pages = [index for index in range(PROGRAM_PAGE_COUNT) if local_crcs[index] != device_crcs[index]]
    write_page_set = set(write_pages)
    # Protocol: equal pages retain the running-area CRC; changed pages start at 0
    # until the device successfully writes and verifies each page.
    control_crcs = [0 if index in write_page_set else device_crcs[index] for index in range(PROGRAM_PAGE_COUNT)]
    return {
        "write_pages": write_pages,
        "control_crcs": control_crcs,
        "data_area": min(write_pages) if write_pages else 0xFF,
    }


def format_frame(frame: Frame) -> str:
    return (
        f"{frame.command_type:<6} #{frame.number:02X} "
        f"DATA={frame.data.hex(' ').upper()} CRC={frame.crc:08X}"
    )
