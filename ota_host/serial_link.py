"""Serial transport with a fixed-frame streaming receiver."""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable

from .protocol import Frame, FrameStreamParser, format_frame

try:  # Keep protocol tests usable before installing pyserial.
    import serial
    from serial.tools import list_ports as serial_list_ports
except ImportError:  # pragma: no cover - depends on environment
    serial = None
    serial_list_ports = None


class SerialUnavailableError(RuntimeError):
    pass


class SerialLink:
    def __init__(
        self,
        on_frame: Callable[[Frame], None] | None = None,
        on_log: Callable[[str, str], None] | None = None,
        on_disconnect: Callable[[str], None] | None = None,
    ) -> None:
        self._on_frame = on_frame
        self._on_log = on_log
        self._on_disconnect = on_disconnect
        self._serial = None
        self._reader: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._write_lock = threading.Lock()
        self._parser = FrameStreamParser()
        self._port = ""
        self._baudrate = 115200
        self._opened_at = 0.0
        self.frames: queue.Queue[Frame] = queue.Queue(maxsize=4096)

    @staticmethod
    def available() -> bool:
        return serial is not None

    @staticmethod
    def list_ports() -> list[dict]:
        if serial_list_ports is None:
            return []
        ports = []
        for item in serial_list_ports.comports():
            ports.append(
                {
                    "device": item.device,
                    "description": item.description or "",
                    "hwid": item.hwid or "",
                }
            )
        return sorted(ports, key=lambda item: item["device"])

    @property
    def is_open(self) -> bool:
        return bool(self._serial is not None and self._serial.is_open)

    @property
    def port(self) -> str:
        return self._port

    @property
    def baudrate(self) -> int:
        return self._baudrate

    @property
    def opened_at(self) -> float:
        return self._opened_at

    @property
    def parser_stats(self) -> dict:
        return {
            "invalid_frames": self._parser.invalid_frames,
            "discarded_bytes": self._parser.discarded_bytes,
        }

    def open(self, port: str, baudrate: int = 115200) -> None:
        if serial is None:
            raise SerialUnavailableError("未安装 pyserial，请运行 start.bat 或 pip install -r requirements.txt")
        if self.is_open:
            self.close()
        self._port = port.strip()
        self._baudrate = int(baudrate)
        if not self._port:
            raise ValueError("串口名称不能为空")
        self._serial = serial.Serial(
            port=self._port,
            baudrate=self._baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.05,
            write_timeout=0.5,
        )
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()
        self._parser = FrameStreamParser()
        while not self.frames.empty():
            try:
                self.frames.get_nowait()
            except queue.Empty:
                break
        self._stop_event.clear()
        self._opened_at = time.time()
        self._reader = threading.Thread(target=self._read_loop, name="ota-serial-reader", daemon=True)
        self._reader.start()
        self._log("info", f"串口已打开: {self._port} @ {self._baudrate} 8N1")

    def close(self, reason: str = "用户主动断开") -> None:
        was_open = self.is_open
        self._stop_event.set()
        ser = self._serial
        self._serial = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        reader = self._reader
        if reader is not None and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        self._reader = None
        self._port = ""
        self._opened_at = 0.0
        if was_open and self._on_disconnect:
            self._on_disconnect(reason)

    def write(self, raw: bytes) -> None:
        ser = self._serial
        if ser is None or not ser.is_open:
            raise RuntimeError("串口未连接")
        with self._write_lock:
            ser.write(raw)
            ser.flush()

    def _read_loop(self) -> None:
        reason = "串口读取结束"
        while not self._stop_event.is_set():
            ser = self._serial
            if ser is None or not ser.is_open:
                break
            try:
                waiting = ser.in_waiting
                chunk = ser.read(waiting if waiting > 0 else 1)
            except Exception as exc:
                reason = f"串口读取失败: {exc}"
                break
            if not chunk:
                continue
            for frame in self._parser.feed(chunk):
                self._log("rx", "RX " + format_frame(frame))
                if self._on_frame:
                    self._on_frame(frame)
                try:
                    self.frames.put_nowait(frame)
                except queue.Full:
                    self._log("error", "接收队列已满，丢弃一帧")
        if not self._stop_event.is_set() and self._on_disconnect:
            self._on_disconnect(reason)
        self._log("warn", reason)

    def _log(self, level: str, message: str) -> None:
        if self._on_log:
            self._on_log(level, message)

    def __enter__(self) -> "SerialLink":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()