"""OTA host state machine for queries, page comparison and upgrades."""

from __future__ import annotations

import copy
import logging
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .event_bus import EventBus
from .protocol import (
    ACK_CRC,
    BLOCKS_PER_PAGE,
    CONTROL_PAGE_INDEX,
    DATA_SIZE,
    ERROR_CODES,
    GET_CRC,
    GET_MID,
    GET_STA,
    PAGE_SIZE,
    PROGRAM_PAGE_COUNT,
    PROGRAM_SIZE,
    SET_CRC,
    SET_DAT,
    SET_END,
    SET_ERO,
    SET_MID,
    SET_STA,
    Frame,
    ProtocolError,
    ack_for,
    build_control_page,
    build_frame,
    build_update_plan,
    control_page_crc,
    format_frame,
    parse_frame,
    parse_status,
    program_page_crcs,
    status_record,
    stm32_crc_bytes,
)
from .serial_link import SerialLink

ROOT_DIR = Path(__file__).resolve().parents[1]
LOG_DIR = ROOT_DIR / "logs" / "runtime"
ACK_TIMEOUT_SECONDS = 0.5
MAX_RETRIES = 5
QUERY_TIMEOUT_SECONDS = 3.0
FINAL_TIMEOUT_SECONDS = 10.0

PHASE_ZH = {
    "idle": "待机",
    "query_crc": "查询运行区 CRC",
    "query_mid": "查询程序 ID",
    "query_status": "查询设备状态",
    "compare": "比较固件页",
    "control_page": "发送控制页",
    "program_pages": "发送程序页",
    "finalize": "发送结束请求",
    "verifying": "等待设备最终校验",
    "success": "升级成功",
    "failed": "失败",
    "aborted": "已中止",
}


class OtaError(RuntimeError):
    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


class RemoteOtaError(OtaError):
    pass


class UpgradeCancelled(OtaError):
    pass


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class OtaEngine:
    def __init__(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.bus = EventBus()
        self._state_lock = threading.RLock()
        self._busy_lock = threading.Lock()
        self._busy = False
        self._cancel_event = threading.Event()
        self._pending: deque[Frame] = deque()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ota-worker")
        self._logs: deque[dict] = deque(maxlen=2000)
        self._log_seq = 0
        self._logger = self._make_logger()

        self._state = {
            "connection": {
                "connected": False,
                "port": "",
                "baudrate": 115200,
                "opened_at": None,
                "serial_available": SerialLink.available(),
            },
            "device": {
                "program_id": None,
                "run_status": None,
                "run_status_name": "UNKNOWN",
                "run_status_zh": "未查询",
                "page": None,
                "block": None,
                "error_code": 0,
                "error_name": "NONE",
                "error_zh": "无错误",
                "crc_pages": [],
                "last_query_ms": None,
                "last_query_at": None,
            },
            "firmware": {
                "filename": "",
                "size": 0,
                "padded_bytes": 0,
                "valid": False,
                "page_crcs": [],
                "overall_crc": None,
                "message": "尚未选择固件",
            },
            "task": {
                "active": False,
                "kind": "",
                "phase": "idle",
                "phase_zh": PHASE_ZH["idle"],
                "message": "等待操作",
                "progress": 0.0,
                "confirmed_frames": 0,
                "total_frames": 0,
                "current_page": None,
                "current_block": None,
                "retry": 0,
                "started_at": None,
                "finished_at": None,
                "result": "pending",
                "error_code": None,
            },
            "pages": self._initial_pages(),
        }

        self._link = SerialLink(
            on_log=self._handle_link_log,
            on_disconnect=self._handle_disconnect,
        )
        self._log("info", "OTA 上位机已启动，等待连接串口")

    @staticmethod
    def _make_logger() -> logging.Logger:
        logger = logging.getLogger("ota_host.runtime")
        logger.setLevel(logging.INFO)
        logger.propagate = False
        if not logger.handlers:
            handler = RotatingFileHandler(
                LOG_DIR / f"ota-{datetime.now().strftime('%Y-%m-%d')}.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
            logger.addHandler(handler)
        return logger

    @staticmethod
    def _initial_pages() -> list[dict]:
        pages = []
        for index in range(PROGRAM_PAGE_COUNT):
            pages.append(
                {
                    "index": index,
                    "label": f"Page {index:02d}",
                    "address": f"0x{index * PAGE_SIZE:04X}",
                    "kind": "program",
                    "local_crc": None,
                    "device_crc": None,
                    "decision": "pending",
                    "status": "idle",
                }
            )
        pages.append(
            {
                "index": CONTROL_PAGE_INDEX,
                "label": "Control",
                "address": "0x3C00",
                "kind": "control",
                "local_crc": None,
                "device_crc": None,
                "decision": "pending",
                "status": "idle",
            }
        )
        return pages

    def snapshot(self, include_logs: bool = False) -> dict:
        with self._state_lock:
            state = copy.deepcopy(self._state)
            if include_logs:
                state["logs"] = list(self._logs)
        return state

    def _publish_state(self) -> None:
        self.bus.publish({"kind": "state", "state": self.snapshot(include_logs=False)})

    def _update_task(self, publish: bool = True, **changes) -> None:
        with self._state_lock:
            self._state["task"].update(changes)
        if publish:
            self._publish_state()

    def _update_page(self, index: int, publish: bool = True, **changes) -> None:
        with self._state_lock:
            for page in self._state["pages"]:
                if page["index"] == index:
                    page.update(changes)
                    break
        if publish:
            self._publish_state()

    def _phase(self, phase: str, message: str | None = None, **extra) -> None:
        changes = {"phase": phase, "phase_zh": PHASE_ZH.get(phase, phase)}
        if message is not None:
            changes["message"] = message
        changes.update(extra)
        self._update_task(**changes)

    def _log(self, level: str, message: str, category: str = "system") -> None:
        now = datetime.now().astimezone()
        with self._state_lock:
            self._log_seq += 1
            entry = {
                "seq": self._log_seq,
                "time": now.strftime("%H:%M:%S.%f")[:-3],
                "iso": now.isoformat(timespec="seconds"),
                "level": level,
                "category": category,
                "message": message,
            }
            self._logs.append(entry)
        log_method = {
            "error": self._logger.error,
            "warn": self._logger.warning,
            "debug": self._logger.debug,
        }.get(level, self._logger.info)
        log_method(message)
        self.bus.publish({"kind": "log", "log": entry})
    # ----------------------------------------------------------- connection
    @staticmethod
    def list_ports() -> list[dict]:
        return SerialLink.list_ports()

    @property
    def busy(self) -> bool:
        return self._busy

    def connect(self, port: str, baudrate: int = 115200) -> None:
        if self._busy:
            raise OtaError("正在执行任务，不能切换串口")
        self._log("info", f"正在连接 {port.strip()} @ {int(baudrate)} 8N1")
        try:
            self._link.open(port.strip(), int(baudrate))
        except Exception as exc:
            self._log("error", f"串口连接失败: {exc}")
            raise OtaError(f"串口连接失败: {exc}") from exc
        with self._state_lock:
            self._state["connection"].update(
                {
                    "connected": self._link.is_open,
                    "port": self._link.port,
                    "baudrate": self._link.baudrate,
                    "opened_at": _iso_now(),
                    "serial_available": SerialLink.available(),
                }
            )
        self._publish_state()
        self._log("success", "串口连接成功")

    def disconnect(self) -> None:
        if self._busy:
            raise OtaError("任务进行中，请先中止任务再断开串口")
        self._link.close("用户主动断开")
        with self._state_lock:
            self._state["connection"]["connected"] = False
        self._publish_state()

    def _handle_link_log(self, level: str, message: str) -> None:
        category = "rx" if message.startswith("RX ") else "system"
        self._log(level, message, category=category)

    def _handle_disconnect(self, reason: str) -> None:
        with self._state_lock:
            was_connected = self._state["connection"]["connected"]
            self._state["connection"]["connected"] = False
        if was_connected or self._busy:
            self._log("warn", f"串口已断开: {reason}")
        if self._busy:
            self._cancel_event.set()
        self._publish_state()

    # -------------------------------------------------------------- requests
    def start_query(self, which: str) -> None:
        which = which.strip().lower()
        handlers = {
            "crc": ("query_crc", self._perform_query_crc),
            "mid": ("query_mid", self._perform_query_mid),
            "status": ("query_status", self._perform_query_status),
        }
        if which not in handlers:
            raise OtaError("未知查询类型")
        if not self._link.is_open:
            raise OtaError("串口未连接")
        self._claim_operation("query")
        phase, handler = handlers[which]
        self._begin_task("query", PHASE_ZH[phase])
        self._phase(phase, "查询请求已发送")
        self._executor.submit(self._run_query_job, handler)

    def _run_query_job(self, handler) -> None:
        try:
            handler()
            self._update_task(
                active=False,
                phase="success",
                phase_zh="查询完成",
                message="查询事务完成",
                progress=100.0,
                result="success",
                finished_at=_iso_now(),
            )
            self._log("success", "查询事务完成", category="business")
        except Exception as exc:
            self._fail_task(exc)
        finally:
            self._release_operation()

    def inspect_firmware(self, firmware: bytes, filename: str, pad: bool = True) -> dict:
        prepared, padded_bytes = self._prepare_firmware(firmware, pad)
        crcs = program_page_crcs(prepared)
        overall_crc = stm32_crc_bytes(prepared)
        with self._state_lock:
            self._state["firmware"] = {
                "filename": filename or "firmware.bin",
                "size": len(prepared),
                "padded_bytes": padded_bytes,
                "valid": True,
                "page_crcs": crcs,
                "overall_crc": overall_crc,
                "message": "固件校验完成" if not padded_bytes else f"已以 0xFF 补齐 {padded_bytes} 字节",
            }
            for page in self._state["pages"]:
                index = page["index"]
                if index < PROGRAM_PAGE_COUNT:
                    page["local_crc"] = crcs[index]
                    page["decision"] = "pending"
                    page["status"] = "idle"
        self._publish_state()
        return {
            "filename": filename or "firmware.bin",
            "size": len(prepared),
            "original_size": len(firmware),
            "padded_bytes": padded_bytes,
            "overall_crc": f"{overall_crc:08X}",
            "page_crcs": [f"{crc:08X}" for crc in crcs],
        }

    def start_upgrade(self, firmware: bytes, filename: str, program_id: int, pad: bool = True) -> None:
        if not self._link.is_open:
            raise OtaError("串口未连接")
        if not 0x0001 <= int(program_id) <= 0xFFFF:
            raise OtaError("目标程序 ID 必须在 0x0001..0xFFFF")
        prepared, padded_bytes = self._prepare_firmware(firmware, pad)
        self.inspect_firmware(prepared, filename, pad=False)
        with self._state_lock:
            self._state["firmware"]["padded_bytes"] = padded_bytes
        self._claim_operation("upgrade")
        self._cancel_event.clear()
        self._begin_task("upgrade", "准备升级")
        self._update_task(total_frames=0, progress=0.0, current_page=None, current_block=None, retry=0)
        self._executor.submit(self._run_upgrade_job, prepared, filename, int(program_id), padded_bytes)

    def stop(self) -> None:
        if not self._busy:
            raise OtaError("当前没有正在执行的任务")
        self._cancel_event.set()
        self._log("warn", "收到中止请求，将在当前 ACK 窗口后停止", category="business")
        self._update_task(message="正在中止…")

    def clear_ui_logs(self) -> None:
        with self._state_lock:
            self._logs.clear()
        self.bus.publish({"kind": "clear_logs"})

    def _prepare_firmware(self, firmware: bytes, pad: bool) -> tuple[bytes, int]:
        if not firmware:
            raise OtaError("固件文件为空")
        if len(firmware) > PROGRAM_SIZE:
            raise OtaError(f"固件超过 15 KB 程序区：{len(firmware)} 字节")
        if len(firmware) < PROGRAM_SIZE:
            if not pad:
                raise OtaError(f"固件不足 15 KB：{len(firmware)} 字节")
            padded_bytes = PROGRAM_SIZE - len(firmware)
            firmware = firmware + b"\xFF" * padded_bytes
        else:
            padded_bytes = 0
        return firmware, padded_bytes

    def _claim_operation(self, kind: str) -> None:
        with self._busy_lock:
            if self._busy:
                raise OtaError("已有任务正在执行")
            self._busy = True
        self._cancel_event.clear()

    def _release_operation(self) -> None:
        with self._busy_lock:
            self._busy = False

    def _begin_task(self, kind: str, message: str) -> None:
        with self._state_lock:
            self._state["task"].update(
                {
                    "active": True,
                    "kind": kind,
                    "phase": "idle",
                    "phase_zh": PHASE_ZH["idle"],
                    "message": message,
                    "progress": 0.0,
                    "confirmed_frames": 0,
                    "total_frames": 0,
                    "current_page": None,
                    "current_block": None,
                    "retry": 0,
                    "started_at": _iso_now(),
                    "finished_at": None,
                    "result": "pending",
                    "error_code": None,
                }
            )
        self._publish_state()

    def _fail_task(self, exc: Exception, aborted: bool = False) -> None:
        error_code = getattr(exc, "error_code", None)
        if aborted or isinstance(exc, UpgradeCancelled):
            phase, result, message = "aborted", "aborted", "任务已中止"
            level = "warn"
        else:
            phase, result, message = "failed", "error", str(exc)
            level = "error"
        self._update_task(
            active=False,
            phase=phase,
            phase_zh=PHASE_ZH[phase],
            message=message,
            result=result,
            error_code=error_code,
            finished_at=_iso_now(),
        )
        self._log(level, message, category="business")
    # --------------------------------------------------------------- queries
    def _perform_query_crc(self) -> list[int]:
        started = time.monotonic()
        self._phase("query_crc", "发送 GetCRC")
        self._send_with_ack(build_frame(GET_CRC, 0, b"\x00" * 4))
        crcs: dict[int, int] = {}
        final_status: dict | None = None
        while final_status is None:
            frame = self._receive_any(QUERY_TIMEOUT_SECONDS)
            if frame is None:
                raise OtaError("等待 GetCRC 回报超时")
            if frame.command_type == SET_CRC:
                self._ack_frame(frame)
                if frame.number > 0x0F:
                    self._log("warn", f"忽略非法 SetCRC 页号 {frame.number:02X}", category="business")
                else:
                    crcs[frame.number] = frame.data_uint32
            elif frame.command_type == SET_END:
                self._ack_frame(frame)
                final_status = parse_status(frame.data)
            elif frame.command_type == SET_ERO:
                self._ack_frame(frame)
                self._raise_remote_error(frame, "GetCRC")
            elif frame.command_type == ACK_CRC:
                continue
            else:
                self._ack_frame(frame)
                self._log("warn", f"GetCRC 收到未预期帧: {format_frame(frame)}", category="business")
        self._ensure_query_end(final_status, "GetCRC")
        if len(crcs) != 16:
            raise OtaError(f"GetCRC 返回页数不足: {len(crcs)}/16")
        ordered = [crcs[index] for index in range(16)]
        elapsed_ms = int((time.monotonic() - started) * 1000)
        with self._state_lock:
            device = self._state["device"]
            device["crc_pages"] = ordered
            device["last_query_ms"] = elapsed_ms
            device["last_query_at"] = _iso_now()
            for page in self._state["pages"]:
                page["device_crc"] = ordered[page["index"]]
            for page in self._state["pages"]:
                if page["index"] < PROGRAM_PAGE_COUNT and page["local_crc"] is not None:
                    page["decision"] = "skip" if page["local_crc"] == page["device_crc"] else "write"
        self._publish_state()
        self._log("success", f"GetCRC 完成，共 16 页，耗时 {elapsed_ms} ms", category="business")
        return ordered

    def _perform_query_mid(self) -> int:
        self._phase("query_mid", "发送 GetMID")
        self._send_with_ack(build_frame(GET_MID, 0, b"\x00" * 4))
        program_id = None
        final_status = None
        while final_status is None:
            frame = self._receive_any(QUERY_TIMEOUT_SECONDS)
            if frame is None:
                raise OtaError("等待 GetMID 回报超时")
            if frame.command_type == SET_MID:
                self._ack_frame(frame)
                program_id = frame.data_uint32 & 0xFFFF
            elif frame.command_type == SET_END:
                self._ack_frame(frame)
                final_status = parse_status(frame.data)
            elif frame.command_type == SET_ERO:
                self._ack_frame(frame)
                self._raise_remote_error(frame, "GetMID")
            elif frame.command_type == ACK_CRC:
                continue
            else:
                self._ack_frame(frame)
                self._log("warn", f"GetMID 收到未预期帧: {format_frame(frame)}", category="business")
        self._ensure_query_end(final_status, "GetMID")
        if program_id is None:
            raise OtaError("GetMID 未返回程序 ID")
        with self._state_lock:
            self._state["device"]["program_id"] = program_id
            self._state["device"]["last_query_at"] = _iso_now()
        self._publish_state()
        self._log("success", f"当前运行程序 ID: 0x{program_id:04X}", category="business")
        return program_id

    def _perform_query_status(self) -> dict:
        self._phase("query_status", "发送 GetSta")
        self._send_with_ack(build_frame(GET_STA, 0, b"\x00" * 4))
        status = None
        final_status = None
        while final_status is None:
            frame = self._receive_any(QUERY_TIMEOUT_SECONDS)
            if frame is None:
                raise OtaError("等待 GetSta 回报超时")
            if frame.command_type == SET_STA:
                self._ack_frame(frame)
                status = parse_status(frame.data)
            elif frame.command_type == SET_END:
                self._ack_frame(frame)
                final_status = parse_status(frame.data)
            elif frame.command_type == SET_ERO:
                self._ack_frame(frame)
                self._raise_remote_error(frame, "GetSta")
            elif frame.command_type == ACK_CRC:
                continue
            else:
                self._ack_frame(frame)
                self._log("warn", f"GetSta 收到未预期帧: {format_frame(frame)}", category="business")
        self._ensure_query_end(final_status, "GetSta")
        if status is None:
            status = final_status
        self._update_device_status(status)
        self._log(
            "success",
            f"设备状态: {status['run_status_name']} / Page={status['page']} / Block={status['block']}",
            category="business",
        )
        return status

    def _update_device_status(self, status: dict) -> None:
        with self._state_lock:
            device = self._state["device"]
            device.update(
                {
                    "run_status": status["run_status"],
                    "run_status_name": status["run_status_name"],
                    "run_status_zh": status["run_status_zh"],
                    "page": status["page"],
                    "block": status["block"],
                    "error_code": status["error_code"],
                    "error_name": status["error_name"],
                    "error_zh": status["error_zh"],
                    "last_query_at": _iso_now(),
                }
            )
        self._publish_state()

    def _ensure_query_end(self, status: dict, operation: str) -> None:
        if status["error_code"] == 0:
            return
        self._update_device_status(status)
        name, description = ERROR_CODES.get(status["error_code"], ("RESERVED", "保留错误"))
        raise RemoteOtaError(
            f"{operation} 失败: 0x{status['error_code']:02X} {name} ({description})",
            error_code=status["error_code"],
        )

    def _raise_remote_error(self, frame: Frame, operation: str) -> None:
        status = parse_status(frame.data)
        self._update_device_status(status)
        name, description = ERROR_CODES.get(status["error_code"], ("RESERVED", "保留错误"))
        raise RemoteOtaError(
            f"{operation} 失败: 0x{status['error_code']:02X} {name} ({description})",
            error_code=status["error_code"],
        )

    # --------------------------------------------------------------- upgrade
    def _run_upgrade_job(self, firmware: bytes, filename: str, program_id: int, padded_bytes: int) -> None:
        current_page = None
        try:
            local_crcs = program_page_crcs(firmware)
            self._log(
                "info",
                f"开始升级: {filename or 'firmware.bin'}, ID=0x{program_id:04X}, 补齐={padded_bytes} 字节",
                category="business",
            )
            self._log("info", "升级前置：强制重新查询运行区 CRC，不复用缓存", category="business")
            device_crcs = self._perform_query_crc()
            self._perform_query_mid()

            self._phase("compare", "比较本地固件与运行区页 CRC")
            plan = build_update_plan(local_crcs, device_crcs)
            write_pages: list[int] = plan["write_pages"]
            control_page = build_control_page(
                program_id=program_id,
                data_area=plan["data_area"],
                page_crcs=plan["control_crcs"],
                run_status=0x01,
            )
            control_crc = control_page_crc(control_page)
            self._apply_plan(control_page, control_crc, local_crcs, device_crcs, write_pages)
            total_frames = (1 + len(write_pages)) * BLOCKS_PER_PAGE
            self._update_task(
                total_frames=total_frames,
                confirmed_frames=0,
                progress=0.0,
                current_page=CONTROL_PAGE_INDEX,
                current_block=None,
            )
            self._log(
                "info",
                f"页比较完成: 待写 {write_pages if write_pages else '无程序页，仅控制页'}",
                category="business",
            )

            self._phase("control_page", "发送 1 KB 控制页")
            current_page = CONTROL_PAGE_INDEX
            self._send_page(control_page, CONTROL_PAGE_INDEX)

            for page_index in write_pages:
                if self._cancel_event.is_set():
                    raise UpgradeCancelled("用户中止升级")
                current_page = page_index
                self._phase("program_pages", f"发送程序页 Page {page_index:02d}")
                page_data = firmware[page_index * PAGE_SIZE : (page_index + 1) * PAGE_SIZE]
                self._send_page(page_data, page_index)

            if self._cancel_event.is_set():
                raise UpgradeCancelled("用户中止升级")

            self._phase("finalize", "发送 SetEnd 结束请求")
            end_record = status_record(0x04, 0xFF, 0xFF, 0x00)
            self._send_with_ack(build_frame(SET_END, 0, end_record))

            self._phase("verifying", "等待设备最终校验回报")
            self._perform_final_handshake(local_crcs, program_id)

            self._update_task(
                active=False,
                phase="success",
                phase_zh=PHASE_ZH["success"],
                message="升级成功，设备已进入 VALID_BACKUP",
                progress=100.0,
                result="success",
                current_block=0xFF,
                finished_at=_iso_now(),
            )
            for page in self._state["pages"]:
                if page["status"] in ("sending", "waiting"):
                    page["status"] = "confirmed"
            self._log("success", "OTA 升级成功，最终目标页 CRC 校验通过", category="business")
        except Exception as exc:
            if self._cancel_event.is_set() and not isinstance(exc, RemoteOtaError):
                exc = UpgradeCancelled("用户中止升级")
            if isinstance(exc, UpgradeCancelled):
                self._send_abort_notice(current_page)
                if current_page is not None:
                    self._update_page(current_page, status="failed")
                self._fail_task(exc, aborted=True)
            else:
                if current_page is not None:
                    self._update_page(current_page, status="failed")
                self._fail_task(exc)
        finally:
            self._release_operation()

    def _apply_plan(
        self,
        control_page: bytes,
        control_crc: int,
        local_crcs: list[int],
        device_crcs: list[int],
        write_pages: list[int],
    ) -> None:
        with self._state_lock:
            for page in self._state["pages"]:
                index = page["index"]
                page["device_crc"] = device_crcs[index]
                if index < PROGRAM_PAGE_COUNT:
                    page["local_crc"] = local_crcs[index]
                    page["decision"] = "write" if index in write_pages else "skip"
                    page["status"] = "waiting" if index in write_pages else "skipped"
                else:
                    page["local_crc"] = control_crc
                    page["decision"] = "write"
                    page["status"] = "waiting"
        self._publish_state()
    def _send_page(self, page: bytes, page_index: int) -> None:
        if len(page) != PAGE_SIZE:
            raise OtaError(f"页长度错误: {len(page)}")
        self._update_page(page_index, status="sending")
        for block in range(BLOCKS_PER_PAGE):
            if self._cancel_event.is_set():
                raise UpgradeCancelled("用户中止升级")
            chunk = page[block * DATA_SIZE : (block + 1) * DATA_SIZE]
            self._update_task(current_page=page_index, current_block=block, publish=False)
            self._publish_state()
            self._send_with_ack(build_frame(SET_DAT, block, chunk))
            with self._state_lock:
                task = self._state["task"]
                task["confirmed_frames"] += 1
                total = task["total_frames"]
                task["progress"] = round((task["confirmed_frames"] / total) * 100, 2) if total else 0.0
            if block == BLOCKS_PER_PAGE - 1:
                self._log("info", f"Page {page_index:02X} 256 块已全部确认", category="business")
        self._update_page(page_index, status="confirmed")

    def _perform_final_handshake(self, local_crcs: list[int], program_id: int) -> None:
        final_crcs: dict[int, int] = {}
        final_mid = None
        final_sta = None
        final_end = None
        last_frame_at = time.monotonic()
        while final_end is None:
            remaining = FINAL_TIMEOUT_SECONDS - (time.monotonic() - last_frame_at)
            if remaining <= 0:
                raise OtaError("等待设备最终校验回报超时")
            frame = self._receive_any(remaining)
            if frame is None:
                continue
            last_frame_at = time.monotonic()
            if frame.command_type == SET_MID:
                self._ack_frame(frame)
                final_mid = frame.data_uint32 & 0xFFFF
            elif frame.command_type == SET_CRC:
                self._ack_frame(frame)
                if frame.number > 0x0F:
                    raise OtaError(f"最终 SetCRC 页号非法: {frame.number:02X}")
                final_crcs[frame.number] = frame.data_uint32
            elif frame.command_type == SET_STA:
                self._ack_frame(frame)
                final_sta = parse_status(frame.data)
                self._update_device_status(final_sta)
            elif frame.command_type == SET_ERO:
                self._ack_frame(frame)
                self._raise_remote_error(frame, "最终校验")
            elif frame.command_type == SET_END:
                self._ack_frame(frame)
                final_end = parse_status(frame.data)
            elif frame.command_type == ACK_CRC:
                continue
            else:
                self._ack_frame(frame)
                self._log("warn", f"最终握手收到未预期帧: {format_frame(frame)}", category="business")

        if final_mid != program_id:
            raise OtaError(f"最终程序 ID 不匹配: 0x{(final_mid or 0):04X} != 0x{program_id:04X}")
        if len(final_crcs) != 16:
            raise OtaError(f"最终页 CRC 数量不足: {len(final_crcs)}/16")
        mismatched = [index for index in range(PROGRAM_PAGE_COUNT) if final_crcs[index] != local_crcs[index]]
        if mismatched:
            raise OtaError(f"最终页 CRC 不一致: {mismatched}")
        if final_sta is None or final_sta["run_status"] != 0x05 or final_sta["error_code"] != 0:
            raise OtaError("最终 SetSta 未报告 VALID_BACKUP")
        if final_end["run_status"] != 0x05 or final_end["error_code"] != 0:
            raise OtaError("最终 SetEnd 未报告成功")
        with self._state_lock:
            self._state["device"]["crc_pages"] = [final_crcs[index] for index in range(16)]
            self._state["device"]["program_id"] = program_id
            for page in self._state["pages"]:
                page["device_crc"] = final_crcs[page["index"]]
        self._publish_state()

    def _send_abort_notice(self, page_index: int | None) -> None:
        if not self._link.is_open:
            return
        page = 0xFF if page_index is None else page_index
        block = self._state["task"].get("current_block")
        block = 0xFF if block is None else block
        record = status_record(0x03, page, block, 0x0D)
        try:
            self._send_with_ack(build_frame(SET_ERO, 0, record), allow_cancel=True)
            self._send_with_ack(build_frame(SET_END, 0, record), allow_cancel=True)
            self._log("warn", "已向设备发送中止通知", category="business")
        except Exception as exc:
            self._log("warn", f"中止通知未获确认: {exc}", category="business")

    # --------------------------------------------------------------- transport
    def _send_with_ack(self, raw: bytes, allow_cancel: bool = False) -> Frame:
        frame = parse_frame(raw)
        deferred: list[Frame] = []
        for attempt in range(MAX_RETRIES + 1):
            if self._cancel_event.is_set() and not allow_cancel:
                raise UpgradeCancelled("用户中止升级")
            self._update_task(retry=attempt)
            self._transmit(frame)
            deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                incoming = self._receive_any(remaining)
                if incoming is None:
                    break
                if incoming.command_type == ACK_CRC:
                    if incoming.number == frame.number and incoming.data_uint32 == frame.crc:
                        if deferred:
                            for item in reversed(deferred):
                                self._pending.appendleft(item)
                        return incoming
                    self._log(
                        "warn",
                        f"忽略不匹配 AckCRC: #{incoming.number:02X}",
                        category="business",
                    )
                else:
                    deferred.append(incoming)
            if attempt < MAX_RETRIES:
                self._log(
                    "warn",
                    f"{frame.command_type} #{frame.number:02X} 未确认，500 ms 后重发 ({attempt + 1}/{MAX_RETRIES})",
                    category="business",
                )
        if deferred:
            for item in reversed(deferred):
                self._pending.appendleft(item)
        raise OtaError(
            f"{frame.command_type} #{frame.number:02X} 连续 {MAX_RETRIES + 1} 次未收到匹配 AckCRC",
            error_code=0x0E,
        )

    def _transmit(self, frame: Frame) -> None:
        self._link.write(frame.raw)
        self._log("info", "TX " + format_frame(frame), category="tx")

    def _ack_frame(self, frame: Frame) -> None:
        if frame.command_type == ACK_CRC:
            return
        ack = ack_for(frame)
        self._link.write(ack)
        self._log("info", "TX " + format_frame(parse_frame(ack)), category="tx")

    def _receive_any(self, timeout: float) -> Frame | None:
        if self._pending:
            return self._pending.popleft()
        try:
            return self._link.frames.get(timeout=max(0.0, timeout))
        except queue.Empty:
            return None

    def shutdown(self) -> None:
        self._cancel_event.set()
        try:
            self._link.close("程序退出")
        except Exception:
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)
