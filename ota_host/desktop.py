"""Python/.NET desktop controller for the OTA Console.

The Python process owns the local HTTP/SSE service and exposes a compact native
WinForms control panel.  The operator UI is the HTML application in the system
browser.  Closing the controller releases all backend resources.
"""

from __future__ import annotations

import os
import threading
import webbrowser
from pathlib import Path

from .engine import LOG_DIR, OtaEngine
from .web_server import create_server, run as run_browser

ROOT_DIR = Path(__file__).resolve().parents[1]
WINDOW_TITLE = "OTA Console 控制台"


def run_desktop(host: str = "127.0.0.1", port: int = 8765) -> int:
    try:
        import clr

        clr.AddReference("System.Windows.Forms")
        clr.AddReference("System.Drawing")
        from System.Drawing import Color, Font, FontStyle, Point, Size
        from System.Windows.Forms import (
            Application,
            BorderStyle,
            Button,
            Cursors,
            FlatStyle,
            Form,
            FormBorderStyle,
            FormStartPosition,
            Label,
            Panel,
            TextBox,
            Timer,
        )
    except Exception:
        print("WinForms 不可用，回退到浏览器模式。")
        return run_browser(host, port, open_browser=True)

    engine = OtaEngine()
    server = create_server(host, port, engine)
    actual_port = server.server_address[1]
    url = f"http://{host}:{actual_port}/"
    server_thread = threading.Thread(target=server.serve_forever, name="ota-http", daemon=True)
    server_thread.start()
    engine._log("info", f"桌面控制台已启动: {url}")

    shutdown_lock = threading.Lock()
    shutdown_done = False

    def color(r: int, g: int, b: int):
        return Color.FromArgb(r, g, b)

    bg = color(8, 16, 23)
    panel_bg = color(16, 30, 42)
    line = color(45, 70, 91)
    text = color(231, 239, 246)
    muted = color(140, 161, 180)
    accent = color(48, 213, 174)
    button_bg = color(23, 41, 55)
    danger_bg = color(42, 23, 28)

    Application.EnableVisualStyles()
    form = Form()
    form.Text = WINDOW_TITLE
    form.ClientSize = Size(580, 380)
    form.MinimumSize = Size(580, 380)
    form.MaximumSize = Size(720, 520)
    form.StartPosition = FormStartPosition.CenterScreen
    form.FormBorderStyle = FormBorderStyle.FixedSingle
    form.MaximizeBox = False
    form.BackColor = bg
    form.ForeColor = text
    form.Font = Font("Microsoft YaHei UI", 10)
    form.TopMost = False

    title = Label()
    title.Text = "OTA Console"
    title.Font = Font("Segoe UI", 22, FontStyle.Bold)
    title.ForeColor = text
    title.AutoSize = True
    title.Location = Point(24, 18)
    form.Controls.Add(title)

    subtitle = Label()
    subtitle.Text = "STM32 OTA 上位机 · OTA-PROTO-1.0"
    subtitle.Font = Font("Microsoft YaHei UI", 9)
    subtitle.ForeColor = muted
    subtitle.AutoSize = True
    subtitle.Location = Point(27, 59)
    form.Controls.Add(subtitle)

    panel = Panel()
    panel.BackColor = panel_bg
    panel.BorderStyle = BorderStyle.FixedSingle
    panel.Location = Point(24, 92)
    panel.Size = Size(532, 230)
    form.Controls.Add(panel)

    status_dot = Label()
    status_dot.Text = "●"
    status_dot.Font = Font("Segoe UI", 12, FontStyle.Bold)
    status_dot.ForeColor = accent
    status_dot.AutoSize = True
    status_dot.Location = Point(18, 15)
    panel.Controls.Add(status_dot)

    status_text = Label()
    status_text.Text = "后台服务运行中"
    status_text.Font = Font("Microsoft YaHei UI", 10, FontStyle.Bold)
    status_text.ForeColor = text
    status_text.AutoSize = True
    status_text.Location = Point(42, 15)
    panel.Controls.Add(status_text)

    status_port = Label()
    status_port.Text = "串口未连接"
    status_port.Font = Font("Microsoft YaHei UI", 9)
    status_port.ForeColor = muted
    status_port.AutoSize = True
    status_port.Location = Point(430, 17)
    panel.Controls.Add(status_port)

    address_label = Label()
    address_label.Text = "主界面地址"
    address_label.Font = Font("Microsoft YaHei UI", 9)
    address_label.ForeColor = muted
    address_label.AutoSize = True
    address_label.Location = Point(18, 54)
    panel.Controls.Add(address_label)

    address_box = TextBox()
    address_box.Text = url
    address_box.ReadOnly = True
    address_box.BorderStyle = BorderStyle.FixedSingle
    address_box.BackColor = color(9, 20, 29)
    address_box.ForeColor = color(154, 219, 201)
    address_box.Font = Font("Consolas", 10)
    address_box.Location = Point(18, 78)
    address_box.Size = Size(492, 28)
    panel.Controls.Add(address_box)

    open_button = Button()
    open_button.Text = "打开 OTA 主界面"
    open_button.Font = Font("Microsoft YaHei UI", 11, FontStyle.Bold)
    open_button.FlatStyle = FlatStyle.Flat
    open_button.FlatAppearance.BorderSize = 0
    open_button.BackColor = accent
    open_button.ForeColor = color(5, 37, 30)
    open_button.Cursor = Cursors.Hand
    open_button.Location = Point(18, 122)
    open_button.Size = Size(492, 42)
    open_button.Click += lambda sender, args: webbrowser.open(url)
    panel.Controls.Add(open_button)

    logs_button = Button()
    logs_button.Text = "打开日志目录"
    logs_button.Font = Font("Microsoft YaHei UI", 9)
    logs_button.FlatStyle = FlatStyle.Flat
    logs_button.FlatAppearance.BorderSize = 1
    logs_button.FlatAppearance.BorderColor = line
    logs_button.BackColor = button_bg
    logs_button.ForeColor = text
    logs_button.Location = Point(18, 178)
    logs_button.Size = Size(240, 34)
    logs_button.Click += lambda sender, args: os.startfile(str(LOG_DIR))
    panel.Controls.Add(logs_button)

    quit_button = Button()
    quit_button.Text = "退出软件"
    quit_button.Font = Font("Microsoft YaHei UI", 9)
    quit_button.FlatStyle = FlatStyle.Flat
    quit_button.FlatAppearance.BorderSize = 1
    quit_button.FlatAppearance.BorderColor = color(90, 45, 54)
    quit_button.BackColor = danger_bg
    quit_button.ForeColor = color(255, 177, 182)
    quit_button.Location = Point(270, 178)
    quit_button.Size = Size(240, 34)
    quit_button.Click += lambda sender, args: form.Close()
    panel.Controls.Add(quit_button)

    note = Label()
    note.Text = "关闭此控制台将停止后台服务；OTA 主界面可随时重新打开。"
    note.Font = Font("Microsoft YaHei UI", 9)
    note.ForeColor = color(95, 118, 137)
    note.AutoSize = True
    note.Location = Point(24, 338)
    form.Controls.Add(note)

    def refresh_status(sender=None, args=None) -> None:
        state = engine.snapshot(include_logs=False)
        task = state["task"]
        connection = state["connection"]
        if task.get("active"):
            status_text.Text = f"任务执行中 · {task.get('phase_zh', '')}"
            status_dot.ForeColor = color(243, 182, 76)
        else:
            status_text.Text = "后台服务运行中"
            status_dot.ForeColor = accent
        if connection.get("connected"):
            status_port.Text = f"{connection.get('port', '')} · 已连接"
            status_port.ForeColor = accent
        else:
            status_port.Text = "串口未连接"
            status_port.ForeColor = muted

    status_timer = Timer()
    status_timer.Interval = 800
    status_timer.Tick += refresh_status
    status_timer.Start()

    def shutdown() -> None:
        nonlocal shutdown_done
        with shutdown_lock:
            if shutdown_done:
                return
            shutdown_done = True
        try:
            engine._log("info", "桌面控制台关闭，正在释放串口与 Web 服务")
        except Exception:
            pass
        try:
            server.shutdown()
        except Exception:
            pass
        try:
            server.server_close()
        except Exception:
            pass
        try:
            engine.shutdown()
        except Exception:
            pass

    form.FormClosing += lambda sender, args: shutdown()

    smoke_seconds = float(os.environ.get("OTA_DESKTOP_SMOKE_TEST_SECONDS", "0") or 0)
    if smoke_seconds > 0:
        engine._log("info", f"桌面烟测将在 {smoke_seconds:g} 秒后自动关闭")
        smoke_timer = Timer()
        smoke_timer.Interval = max(100, int(smoke_seconds * 1000))
        smoke_timer.Tick += lambda sender, args: form.Close()
        smoke_timer.Start()
    else:
        open_timer = Timer()
        open_timer.Interval = 600
        open_timer.Tick += lambda sender, args: (open_timer.Stop(), webbrowser.open(url))
        open_timer.Start()

    try:
        Application.Run(form)
    finally:
        shutdown()
    return 0
