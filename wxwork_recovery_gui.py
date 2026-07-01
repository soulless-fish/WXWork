import argparse
import binascii
import csv
import ctypes
import importlib.util
import json
import os
import queue
import re
import sqlite3
import struct
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

from PIL import Image, ImageOps, ImageTk


APP_TITLE = "企业微信聊天记录浏览工具"
LEGACY_OUTPUT_ROOT = None
CORP_ID_RE = re.compile(r"^\d+$")
MAX_LOG_LINES = 1200
RESIZE_DEBOUNCE_MS = 180
DEFAULT_LEFT_PANE_WIDTH = 860
MIN_RIGHT_PANE_WIDTH = 340
PREVIEW_MESSAGE_LIMIT = 0
PREVIEW_IMAGE_MAX_WIDTH = 360
PREVIEW_IMAGE_MAX_HEIGHT = 260
AUTO_RECOVERY_DEFAULT_INTERVAL_SECONDS = 1
AUTO_RECOVERY_MIN_INTERVAL_SECONDS = 1
AUTO_RECOVERY_BUSY_RETRY_DELAY_MS = 300
RIGHT_PANEL_TOP_RATIO = 0.3
RIGHT_PANEL_BOTTOM_RATIO = 0.7
RIGHT_PANEL_SECTION_GAP = 10
SPLIT_PANEL_MIN_WIDTH = 420
TARGET_LAYOUT_WIDE_THRESHOLD = 560
RIGHT_PANEL_CONTENT_BUFFER = 24
ORGANIZE_BUTTON_DENSE_THRESHOLD = 760
ORGANIZE_BUTTON_STACK_THRESHOLD = 500
PREVIEW_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
PREVIEW_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".webm"}
PREVIEW_AUDIO_EXTENSIONS = {".mp3", ".wav", ".aac", ".m4a", ".amr", ".silk", ".opus"}
SUPPORTED_CONVERSATION_KINDS = {"external_group", "single_chat"}
CONVERSATION_FILTERS = [
    ("外部群 (R:)", "external_group"),
    ("单聊 (S:)", "single_chat"),
]


def preview_limit_text(limit: int) -> str:
    if int(limit or 0) <= 0:
        return "全部聊天内容"
    return f"最近 {int(limit)} 条"


def normalize_auto_recovery_interval(raw_value, minimum_seconds: int = AUTO_RECOVERY_MIN_INTERVAL_SECONDS) -> int:
    """
    将界面填写的实时恢复间隔统一整理成合法秒数，避免过小间隔导致持续高频扫描企业微信进程。
    """
    text = str(raw_value).strip()
    if not text:
        raise ValueError("请先填写实时恢复间隔秒数。")
    if not text.isdigit():
        raise ValueError("实时恢复间隔只能填写整数秒。")

    seconds = int(text)
    if seconds < minimum_seconds:
        raise ValueError(f"实时恢复间隔不能小于 {minimum_seconds} 秒。")
    return seconds


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def get_resource_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def default_output_root(base_dir: Path) -> Path:
    """
    兼容当前仓库目录；如果换到新电脑运行，则默认落到当前用户文档目录，避免写入固定 E 盘路径。
    """
    if LEGACY_OUTPUT_ROOT and LEGACY_OUTPUT_ROOT.exists():
        return LEGACY_OUTPUT_ROOT

    candidates = []
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        candidates.append(Path(userprofile) / "Documents" / "WXWorkRecovered")
    candidates.append(Path.home() / "Documents" / "WXWorkRecovered")

    seen = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        parent = candidate.parent
        if parent.exists():
            return candidate
    return base_dir / "WXWorkRecovered"


def open_path(path: Path):
    try:
        os.startfile(str(path))
    except OSError as exc:
        messagebox.showerror("打开失败", f"无法打开：\n{path}\n\n{exc}")


def human_size(num_bytes: int) -> str:
    value = float(num_bytes)
    units = ["B", "KB", "MB", "GB"]
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{num_bytes} B"


def missing_recovery_sqlite_message(source_dir: Path) -> str:
    """
    生成新电脑首次使用时的空结果提示，避免把“还没有恢复过”误显示成程序异常。
    """
    return (
        f"当前恢复目录还没有可读取的恢复结果 sqlite：{source_dir}。"
        "如果这是新电脑第一次使用，请先确认企业微信已打开并登录目标账号，"
        "然后点击“立即提取一次聊天数据”；恢复完成后再刷新会话列表。"
    )


def enable_high_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class ScrollableFrame(ttk.Frame):
    def __init__(self, parent, padding=(0, 0, 0, 0), background="#edf2f7"):
        super().__init__(parent)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._canvas_resize_after_id = None
        self._pending_canvas_width = None

        self.canvas = tk.Canvas(self, bg=background, highlightthickness=0, bd=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        self.v_scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.v_scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.v_scrollbar.set)

        self.content = ttk.Frame(self.canvas, padding=padding)
        self.window_id = self.canvas.create_window((0, 0), window=self.content, anchor="nw")

        self.content.bind("<Configure>", self._on_content_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.content.bind("<Enter>", self._bind_mousewheel)
        self.content.bind("<Leave>", self._unbind_mousewheel)

    def _on_content_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self._pending_canvas_width = event.width
        if self._canvas_resize_after_id:
            self.after_cancel(self._canvas_resize_after_id)
        self._canvas_resize_after_id = self.after_idle(self._apply_canvas_width)

    def _apply_canvas_width(self):
        self._canvas_resize_after_id = None
        if self._pending_canvas_width is None:
            return
        current_width = int(float(self.canvas.itemcget(self.window_id, "width") or 0))
        if current_width == self._pending_canvas_width:
            return
        self.canvas.itemconfigure(self.window_id, width=self._pending_canvas_width)

    def _bind_mousewheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_mousewheel(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event):
        if self.canvas.winfo_height() >= self.content.winfo_reqheight():
            return
        delta = 0
        if getattr(event, "delta", 0):
            delta = -1 * int(event.delta / 120)
        if delta:
            self.canvas.yview_scroll(delta, "units")


class RecoveryApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.base_dir = get_base_dir()
        self.resource_dir = get_resource_dir()
        self.engine_module = None
        self.organizer_module = None
        self.engine_error = ""
        self.organizer_error = ""
        self.ui_queue = queue.Queue()
        self.busy = False
        self.worker_thread = None
        self.process_hits = []
        self.result_files = []
        self.all_conversation_rows = []
        self.conversation_rows = []
        self._resize_after_id = None
        self._last_configure_size = None
        self.auto_open_organized_timeline = False
        self.preview_thread = None
        self.conversation_self_user_id = ""
        self.conversation_metadata = {}
        self.conversation_index_by_id = {}
        self.conversation_preview_cache = {}
        self.preview_inline_images = []
        self.preview_inline_widgets = []
        self.preview_request_token = 0
        self.preview_requested_conversation_id = ""
        self.realtime_enabled = False
        self.realtime_after_id = None
        self.realtime_interval_seconds = AUTO_RECOVERY_DEFAULT_INTERVAL_SECONDS
        self.realtime_cycle_started_at = None
        self.preview_summary_details_visible = None
        self.preview_header_layout_mode = ""
        self.main_layout_mode = ""
        self.header_compact_mode = ""
        self.organize_layout_mode = ""
        self.right_panel_ratio_mode = ""

        self.docs_dir = self.detect_docs_dir()
        self.default_output_dir = default_output_root(self.base_dir)
        self.recovery_script_path = self.find_recovery_script()
        self.organizer_script_path = self.find_organizer_script()

        self.status_var = tk.StringVar(value="就绪")
        self.corp_id_var = tk.StringVar()
        self.pid_var = tk.StringVar()
        self.output_dir_var = tk.StringVar(value=str(self.default_output_dir))
        self.engine_status_var = tk.StringVar(value="未加载")
        self.organizer_status_var = tk.StringVar(value="未加载")
        self.engine_path_var = tk.StringVar(value=str(self.recovery_script_path) if self.recovery_script_path else "未找到")
        self.organizer_path_var = tk.StringVar(value=str(self.organizer_script_path) if self.organizer_script_path else "未找到")
        self.docs_dir_var = tk.StringVar(value=str(self.docs_dir) if self.docs_dir else "未找到")
        self.runtime_var = tk.StringVar(value=sys.executable)
        self.selection_hint_var = tk.StringVar(value="先选择企业账号ID，再扫描进程，然后执行恢复。")
        self.organize_output_dir_var = tk.StringVar(value=str(self.default_output_dir / "organized_external_groups"))
        self.latest_sqlite_var = tk.StringVar(value="未扫描")
        self.realtime_interval_var = tk.StringVar(value=str(AUTO_RECOVERY_DEFAULT_INTERVAL_SECONDS))
        self.realtime_status_var = tk.StringVar(value="未启动")
        self.realtime_last_run_var = tk.StringVar(value="尚未执行")
        self.conversation_filter_var = tk.StringVar(value=CONVERSATION_FILTERS[0][0])
        self.conversation_hint_var = tk.StringVar(value="先刷新会话列表。这里只保留外部群和单聊。")
        self.simple_error_var = tk.StringVar(value="按 1 -> 2 -> 3 的顺序点。出错时看右侧“恢复日志”最下面一行。")
        self.preview_title_var = tk.StringVar(value="未选择会话")
        self.preview_subtitle_var = tk.StringVar(value="先在左侧选择一个会话，再查看完整聊天内容。")
        self.preview_kind_var = tk.StringVar(value="-")
        self.preview_id_var = tk.StringVar(value="-")
        self.preview_counterpart_var = tk.StringVar(value="-")
        self.preview_stats_var = tk.StringVar(value="-")
        self.preview_range_var = tk.StringVar(value="-")
        self.preview_evidence_var = tk.StringVar(value="-")
        self.preview_participants_var = tk.StringVar(value="-")
        self.preview_status_var = tk.StringVar(value="会话聊天内容会显示在这里，可直接查看文本和本地媒体文件。")

        self.configure_root()
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<Configure>", self.on_root_configure)
        self.refresh_environment_status()
        self.refresh_corp_dirs()
        self.refresh_results()
        self.refresh_realtime_controls()
        self.root.after(220, self.initialize_responsive_layout)
        self.root.after(150, self.process_ui_queue)

    def configure_root(self):
        self.root.title(APP_TITLE)
        self.root.geometry("1280x820")
        # 允许窗口缩到更小尺寸，配合后面的响应式重排切换到上下布局。
        self.root.minsize(860, 650)
        self.root.configure(bg="#edf2f7")

        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#edf2f7")
        style.configure("TLabelframe", background="#edf2f7")
        style.configure("TLabelframe.Label", background="#edf2f7", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TLabel", background="#edf2f7", font=("Microsoft YaHei UI", 10))
        style.configure("Header.TLabel", background="#edf2f7", foreground="#16324f", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Hint.TLabel", background="#edf2f7", foreground="#425466")
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("SimpleStep.TButton", font=("Microsoft YaHei UI", 11, "bold"), padding=(10, 10))
        style.configure("CompactTool.TButton", font=("Microsoft YaHei UI", 9), padding=(6, 3))
        style.configure("CompactTool.TMenubutton", font=("Microsoft YaHei UI", 9), padding=(6, 3))
        style.configure("TPanedwindow", background="#edf2f7")
        style.configure("TNotebook", background="#edf2f7")
        style.configure("TNotebook.Tab", font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 8))
        style.configure("Treeview", rowheight=24, font=("Consolas", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("PreviewTitle.TLabel", background="#ffffff", foreground="#16324f", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure("PreviewSub.TLabel", background="#ffffff", foreground="#5b7083", font=("Microsoft YaHei UI", 10))
        style.configure("CardLabel.TLabel", background="#ffffff", foreground="#5b7083", font=("Microsoft YaHei UI", 9))
        style.configure("CardValue.TLabel", background="#ffffff", foreground="#16324f", font=("Microsoft YaHei UI", 10, "bold"))

    def build_ui(self):
        self.build_menu()
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(18, 14, 18, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self.header_frame = header

        ttk.Label(header, text="企业微信聊天记录浏览", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        self.header_hint_label = ttk.Label(
            header,
            text="当前界面按“设置恢复目标 -> 执行恢复 -> 查看会话 -> 导出会话”的顺序组织，并在命中本地图片时内嵌显示图片。",
            style="Hint.TLabel",
        )
        self.header_hint_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Label(header, textvariable=self.status_var, style="Hint.TLabel").grid(row=0, column=1, sticky="e")

        content = ttk.Frame(self.root, padding=(18, 0, 18, 18))
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=1, minsize=360)
        content.grid_columnconfigure(1, weight=0, minsize=0)
        content.grid_rowconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=0)
        self.content_frame = content

        left_wrapper = ttk.Frame(content, padding=(0, 0, 8, 0))
        left_wrapper.grid_columnconfigure(0, weight=1)
        left_wrapper.grid_rowconfigure(0, weight=0)
        left_wrapper.grid_rowconfigure(1, weight=1)
        self.left_wrapper = left_wrapper

        right_wrapper = ttk.Frame(content, padding=(8, 0, 0, 0))
        right_wrapper.grid_columnconfigure(0, weight=1)
        right_wrapper.grid_rowconfigure(0, weight=0)
        right_wrapper.grid_rowconfigure(1, weight=1)
        right_wrapper.bind("<Configure>", self.on_right_wrapper_configure)
        self.right_wrapper = right_wrapper

        preview_summary_frame = self.build_preview_summary_frame(left_wrapper)
        self.preview_summary_frame = preview_summary_frame
        preview_summary_frame.grid(row=0, column=0, sticky="ew")

        conversation_list_frame = self.build_conversation_list_frame(left_wrapper)
        self.conversation_list_frame = conversation_list_frame
        conversation_list_frame.grid(row=1, column=0, sticky="nsew", pady=(10, 0))

        organize_controls_frame = self.build_organize_controls_frame(right_wrapper)
        self.organize_controls_frame = organize_controls_frame
        organize_controls_frame.grid(row=0, column=0, sticky="nsew")

        browser_workspace_frame = self.build_browser_workspace_frame(right_wrapper)
        self.browser_workspace_frame = browser_workspace_frame
        browser_workspace_frame.grid(row=1, column=0, sticky="nsew", pady=(RIGHT_PANEL_SECTION_GAP, 0))

        self.apply_main_layout_mode(1280)

    def build_menu(self):
        menu_bar = tk.Menu(self.root)

        file_menu = tk.Menu(menu_bar, tearoff=False)
        file_menu.add_command(label="打开输出目录", command=self.open_output_dir)
        file_menu.add_command(label="打开整理目录", command=self.open_organized_dir)
        file_menu.add_separator()
        file_menu.add_command(label="刷新结果文件", command=self.refresh_results)
        file_menu.add_command(label="退出", command=self.on_close)
        menu_bar.add_cascade(label="文件", menu=file_menu)

        organize_menu = tk.Menu(menu_bar, tearoff=False)
        organize_menu.add_command(label="重新读取会话", command=self.start_conversation_scan)
        organize_menu.add_command(label="刷新当前聊天预览", command=lambda: self.refresh_selected_conversation_preview(force=True))
        organize_menu.add_separator()
        organize_menu.add_command(label="立即提取一次聊天数据", command=self.start_recovery)
        organize_menu.add_command(label="开启实时恢复", command=self.start_realtime_recovery)
        organize_menu.add_command(label="停止实时恢复", command=self.stop_realtime_recovery_by_user)
        organize_menu.add_separator()
        organize_menu.add_command(label="导出当前会话", command=self.start_organize_selected)
        organize_menu.add_command(label="批量导出当前筛选结果", command=self.start_organize_filtered)
        organize_menu.add_separator()
        organize_menu.add_command(label="打开最近导出索引", command=self.open_organized_index)
        organize_menu.add_command(label="打开全部会话总表", command=self.open_all_conversations_index)
        organize_menu.add_command(label="打开最近整理时间线", command=self.open_latest_organized_timeline)
        menu_bar.add_cascade(label="聊天整理", menu=organize_menu)

        help_menu = tk.Menu(menu_bar, tearoff=False)
        help_menu.add_command(label="打开 GUI 使用说明", command=lambda: self.open_named_file("WXWork_Recovery_GUI_Guide.md"))
        help_menu.add_command(label="打开按钮功能详解", command=lambda: self.open_named_file("WXWorkRecoveryGUI_按钮功能详解.md"))
        help_menu.add_command(label="打开操作文档", command=lambda: self.open_named_file("WXWork_Chat_Recovery_Playbook.md"))
        help_menu.add_command(label="打开交接文档", command=lambda: self.open_named_file("WXWork_Recovery_Codex_Handoff.md"))
        help_menu.add_command(label="打开链路说明", command=lambda: self.open_named_file("项目详细链路说明-聊天恢复与知识库接入.md"))
        help_menu.add_separator()
        help_menu.add_command(label="打开企业微信目录", command=self.open_docs_dir)
        menu_bar.add_cascade(label="帮助", menu=help_menu)

        self.root.configure(menu=menu_bar)

    def build_simple_frame(self, parent):
        frame = ttk.Frame(parent)
        frame.grid_columnconfigure(0, weight=1)

        intro = ttk.LabelFrame(frame, text="给普通人的最短流程", padding=12)
        intro.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(
            intro,
            text="只按下面 1 -> 2 -> 3 的顺序点。出错时不用看复杂参数，只看右侧“恢复日志”最下面一行。",
            style="Hint.TLabel",
            wraplength=380,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

        step1 = ttk.LabelFrame(frame, text="1. 先点什么", padding=12)
        step1.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        step1.grid_columnconfigure(0, weight=1)
        self.simple_refresh_button = ttk.Button(step1, text="刷新账号列表", style="SimpleStep.TButton", command=self.refresh_corp_dirs)
        self.simple_refresh_button.grid(row=0, column=0, sticky="ew")
        ttk.Label(step1, text="当前企业账号ID").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(step1, textvariable=self.corp_id_var, state="readonly").grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(
            step1,
            text="程序会尽量自动识别账号；如果这里还是空的，先在企业微信里打开目标账号。",
            style="Hint.TLabel",
            wraplength=380,
            justify="left",
        ).grid(row=3, column=0, sticky="w", pady=(8, 0))

        step2 = ttk.LabelFrame(frame, text="2. 后点什么", padding=12)
        step2.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        step2.grid_columnconfigure(0, weight=1)
        self.simple_scan_button = ttk.Button(step2, text="扫描匹配进程", style="SimpleStep.TButton", command=self.start_process_scan)
        self.simple_scan_button.grid(row=0, column=0, sticky="ew")
        ttk.Label(step2, text="当前已选 PID").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(step2, textvariable=self.pid_var, state="readonly").grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(
            step2,
            text="扫描后会自动选中最可能的企业微信进程。普通情况下不用手动改。",
            style="Hint.TLabel",
            wraplength=380,
            justify="left",
        ).grid(row=3, column=0, sticky="w", pady=(8, 0))

        step3 = ttk.LabelFrame(frame, text="3. 最后点什么", padding=12)
        step3.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        step3.grid_columnconfigure(0, weight=1)
        self.simple_run_button = ttk.Button(step3, text="开始恢复", style="SimpleStep.TButton", command=self.start_recovery)
        self.simple_run_button.grid(row=0, column=0, sticky="ew")

        result_buttons = ttk.Frame(step3)
        result_buttons.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(result_buttons, text="打开最新 CSV", command=lambda: self.open_latest_file("_readable.csv")).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(result_buttons, text="打开输出目录", command=self.open_output_dir).grid(row=0, column=1)

        ttk.Label(
            step3,
            text="恢复完成后，直接点“打开最新 CSV”查看结果。",
            style="Hint.TLabel",
            wraplength=380,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(8, 0))

        error_frame = ttk.LabelFrame(frame, text="出错看哪里", padding=12)
        error_frame.grid(row=4, column=0, sticky="ew")
        error_frame.grid_columnconfigure(0, weight=1)
        ttk.Label(
            error_frame,
            text="先看下面“当前状态”，再看右侧“恢复日志”最下面一行。把那一行发出来就够了。",
            style="Hint.TLabel",
            wraplength=380,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(error_frame, text="当前状态").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(error_frame, textvariable=self.status_var, state="readonly").grid(row=2, column=0, sticky="ew", pady=(4, 0))
        ttk.Label(error_frame, text="最后错误/提示").grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Label(
            error_frame,
            textvariable=self.simple_error_var,
            style="Hint.TLabel",
            wraplength=380,
            justify="left",
        ).grid(row=4, column=0, sticky="w", pady=(4, 0))

        return frame

    def build_environment_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="运行环境", padding=12)
        frame.grid_columnconfigure(1, weight=1)

        ttk.Label(frame, text="运行时").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.runtime_var, state="readonly").grid(row=0, column=1, sticky="ew", padx=(8, 8))

        ttk.Label(frame, text="恢复引擎").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.engine_status_var, state="readonly").grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        ttk.Button(frame, text="重新加载", command=lambda: self.refresh_environment_status(force=True)).grid(row=1, column=2, sticky="e", pady=(8, 0))

        ttk.Label(frame, text="引擎路径").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.engine_path_var, state="readonly").grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))

        ttk.Label(frame, text="企业微信目录").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.docs_dir_var, state="readonly").grid(row=3, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        ttk.Button(frame, text="打开", command=self.open_docs_dir).grid(row=3, column=2, sticky="e", pady=(8, 0))

        ttk.Label(frame, text="整理引擎").grid(row=4, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.organizer_status_var, state="readonly").grid(row=4, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        ttk.Button(frame, text="重新加载", command=lambda: self.refresh_environment_status(force=True)).grid(row=4, column=2, sticky="e", pady=(8, 0))

        ttk.Label(frame, text="整理脚本").grid(row=5, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.organizer_path_var, state="readonly").grid(row=5, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))

        return frame

    def build_target_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="目标设置", padding=12)
        frame.grid_columnconfigure(1, weight=1)

        ttk.Label(frame, text="企业账号ID").grid(row=0, column=0, sticky="w")
        ttk.Entry(frame, textvariable=self.corp_id_var).grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(frame, text="刷新账号列表", command=self.refresh_corp_dirs).grid(row=0, column=2, sticky="e")

        ttk.Label(frame, text="可选 PID").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.pid_var).grid(row=1, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))
        ttk.Button(frame, text="清空 PID", command=lambda: self.pid_var.set("")).grid(row=1, column=2, sticky="e", pady=(8, 0))

        ttk.Label(frame, text="输出目录").grid(row=2, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(frame, textvariable=self.output_dir_var).grid(row=2, column=1, sticky="ew", padx=(8, 8), pady=(8, 0))

        output_buttons = ttk.Frame(frame)
        output_buttons.grid(row=2, column=2, sticky="e", pady=(8, 0))
        ttk.Button(output_buttons, text="浏览", command=self.choose_output_dir).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(output_buttons, text="打开", command=self.open_output_dir).grid(row=0, column=1)

        ttk.Label(frame, textvariable=self.selection_hint_var, style="Hint.TLabel").grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))

        return frame

    def build_corp_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="检测到的企业账号目录", padding=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        columns = ("corp_id", "has_db", "modified")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=8)
        tree.heading("corp_id", text="企业账号ID")
        tree.heading("has_db", text="存在 message.db")
        tree.heading("modified", text="修改时间")
        tree.column("corp_id", width=180, minwidth=140, anchor="w", stretch=True)
        tree.column("has_db", width=100, minwidth=90, anchor="center", stretch=False)
        tree.column("modified", width=170, minwidth=150, anchor="center", stretch=False)
        tree.grid(row=0, column=0, sticky="nsew")
        tree.bind("<<TreeviewSelect>>", self.on_corp_selected)
        tree.bind("<Double-1>", self.on_corp_double_clicked)

        y_scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        x_scrollbar.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        tree.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)

        self.corp_tree = tree
        return frame

    def build_process_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="候选 WXWork 进程", padding=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.grid_columnconfigure(4, weight=1)

        ttk.Button(toolbar, text="扫描匹配进程", command=self.start_process_scan).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(toolbar, text="使用所选 PID", command=self.apply_selected_pid).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(toolbar, text="打开企业微信目录", command=self.open_docs_dir).grid(row=0, column=2)

        columns = ("pid", "wow64", "hits", "sample")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=8)
        tree.heading("pid", text="PID")
        tree.heading("wow64", text="WOW64")
        tree.heading("hits", text="路径命中")
        tree.heading("sample", text="示例命中地址")
        tree.column("pid", width=80, minwidth=70, anchor="center", stretch=False)
        tree.column("wow64", width=80, minwidth=70, anchor="center", stretch=False)
        tree.column("hits", width=90, minwidth=80, anchor="center", stretch=False)
        tree.column("sample", width=220, minwidth=160, anchor="w", stretch=True)
        tree.grid(row=1, column=0, sticky="nsew")
        tree.bind("<Double-1>", lambda _event: self.apply_selected_pid())

        y_scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        y_scrollbar.grid(row=1, column=1, sticky="ns")
        x_scrollbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        x_scrollbar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        tree.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)

        self.process_tree = tree
        return frame

    def build_actions_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="执行操作", padding=12)
        frame.grid_columnconfigure(0, weight=1)

        button_grid = ttk.Frame(frame)
        button_grid.grid(row=0, column=0, sticky="ew")
        for index in range(3):
            button_grid.grid_columnconfigure(index, weight=1)

        self.run_button = ttk.Button(button_grid, text="开始恢复", style="Accent.TButton", command=self.start_recovery)
        self.run_button.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 6))
        ttk.Button(button_grid, text="刷新结果", command=self.refresh_results).grid(row=0, column=1, sticky="ew", padx=6, pady=(0, 6))
        ttk.Button(button_grid, text="打开最新 CSV", command=lambda: self.open_latest_file("_readable.csv")).grid(row=0, column=2, sticky="ew", padx=(6, 0), pady=(0, 6))
        ttk.Button(button_grid, text="打开最新报告", command=lambda: self.open_latest_file("_report.md")).grid(row=1, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(button_grid, text="打开操作文档", command=lambda: self.open_named_file("WXWork_Chat_Recovery_Playbook.md")).grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(button_grid, text="打开交接文档", command=lambda: self.open_named_file("WXWork_Recovery_Codex_Handoff.md")).grid(row=1, column=2, sticky="ew", padx=(6, 0))

        tips = (
            "执行前请保持企业微信开启，切到目标账号，打开目标聊天，并上滑更多历史消息，"
            "让更多已解密的 SQLite 页面进入缓存。"
        )
        ttk.Label(frame, text=tips, wraplength=420, style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(10, 0))
        return frame

    def build_preview_summary_frame(self, parent):
        frame = ttk.Frame(parent, padding=(0, 0, 0, 4))
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        summary_notebook = ttk.Notebook(frame)
        summary_notebook.grid(row=0, column=0, sticky="ew")
        summary_notebook.bind("<<NotebookTabChanged>>", self.on_preview_summary_tab_changed)
        self.preview_summary_notebook = summary_notebook

        summary_tab = ttk.Frame(summary_notebook)
        summary_tab.grid_columnconfigure(0, weight=1)
        summary_notebook.add(summary_tab, text="工作概览")
        self.preview_summary_overview_tab = summary_tab

        summary_card = tk.Frame(summary_tab, bg="#ffffff", highlightbackground="#d7dee6", highlightthickness=1, bd=0)
        summary_card.grid(row=0, column=0, sticky="ew")
        summary_card.grid_columnconfigure(1, weight=1)
        self.preview_summary_card = summary_card

        tk.Label(
            summary_card,
            text="工作概览",
            bg="#ffffff",
            fg="#16324f",
            font=("Microsoft YaHei UI", 15, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=16, pady=(14, 2))
        self.summary_card_hint_label = tk.Label(
            summary_card,
            text="默认只保留主流程信息；导出索引、时间线和会话详情收进二级入口，缩小时优先保证会话列表与聊天内容可用。",
            bg="#ffffff",
            fg="#5b7083",
            font=("Microsoft YaHei UI", 10),
            anchor="w",
            justify="left",
        )
        self.summary_card_hint_label.grid(row=1, column=0, columnspan=3, sticky="ew", padx=16)

        ttk.Label(summary_card, text="导出目录", style="CardLabel.TLabel").grid(row=2, column=0, sticky="w", padx=16, pady=(12, 0))
        ttk.Entry(summary_card, textvariable=self.organize_output_dir_var, state="readonly").grid(
            row=2, column=1, sticky="ew", padx=(0, 12), pady=(12, 0)
        )
        ttk.Button(summary_card, text="打开目录", command=self.open_organized_dir).grid(row=2, column=2, sticky="e", padx=(0, 16), pady=(12, 0))

        ttk.Label(summary_card, text="最新 sqlite", style="CardLabel.TLabel").grid(row=3, column=0, sticky="w", padx=16, pady=(8, 8))
        ttk.Entry(summary_card, textvariable=self.latest_sqlite_var, state="readonly").grid(
            row=3, column=1, sticky="ew", padx=(0, 12), pady=(8, 8)
        )
        summary_actions = ttk.Frame(summary_card)
        summary_actions.grid(row=3, column=2, sticky="e", padx=(0, 16), pady=(8, 8))
        self.summary_more_button = ttk.Menubutton(summary_actions, text="更多操作")
        self.summary_more_menu = tk.Menu(self.summary_more_button, tearoff=False)
        self.summary_more_menu.add_command(label="打开最近导出索引", command=self.open_organized_index)
        self.summary_more_menu.add_command(label="打开全部会话总表", command=self.open_all_conversations_index)
        self.summary_more_menu.add_command(label="打开最近整理时间线", command=self.open_latest_organized_timeline)
        self.summary_more_button["menu"] = self.summary_more_menu
        self.summary_more_button.grid(row=0, column=0, sticky="ew")
        ttk.Label(summary_card, text="实时恢复", style="CardLabel.TLabel").grid(row=4, column=0, sticky="w", padx=16, pady=(0, 0))
        ttk.Entry(summary_card, textvariable=self.realtime_status_var, state="readonly").grid(
            row=4, column=1, columnspan=2, sticky="ew", padx=(0, 16), pady=(0, 0)
        )
        ttk.Label(summary_card, text="最近执行", style="CardLabel.TLabel").grid(row=5, column=0, sticky="w", padx=16, pady=(8, 14))
        ttk.Entry(summary_card, textvariable=self.realtime_last_run_var, state="readonly").grid(
            row=5, column=1, columnspan=2, sticky="ew", padx=(0, 16), pady=(8, 14)
        )

        detail_tab = ttk.Frame(summary_notebook)
        detail_tab.grid_columnconfigure(0, weight=1)
        summary_notebook.add(detail_tab, text="当前会话概览")
        self.preview_summary_detail_tab = detail_tab

        info_card = tk.Frame(detail_tab, bg="#ffffff", highlightbackground="#d7dee6", highlightthickness=1, bd=0)
        info_card.grid(row=0, column=0, sticky="ew")
        for column in range(3):
            info_card.grid_columnconfigure(column, weight=1)

        header_frame = tk.Frame(info_card, bg="#ffffff")
        header_frame.grid(row=0, column=0, columnspan=3, sticky="ew", padx=12, pady=(10, 4))
        header_frame.grid_columnconfigure(0, weight=1)

        header_text_frame = tk.Frame(header_frame, bg="#ffffff")
        header_text_frame.grid(row=0, column=0, sticky="ew")
        header_text_frame.grid_columnconfigure(0, weight=1)

        self.preview_title_label = ttk.Label(
            header_text_frame,
            textvariable=self.preview_title_var,
            style="PreviewTitle.TLabel",
            justify="left",
        )
        self.preview_title_label.grid(row=0, column=0, sticky="ew")
        self.preview_subtitle_label = ttk.Label(
            header_text_frame,
            textvariable=self.preview_subtitle_var,
            style="PreviewSub.TLabel",
            wraplength=760,
            justify="left",
        )
        self.preview_subtitle_label.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        self.preview_action_bar = ttk.Frame(header_frame)
        self.preview_action_buttons = [
            ttk.Button(self.preview_action_bar, text="导出当前会话", style="CompactTool.TButton", command=self.start_organize_selected),
            ttk.Button(self.preview_action_bar, text="查看详情", style="CompactTool.TButton", command=self.open_preview_details_dialog),
        ]
        self.preview_more_actions_button = ttk.Menubutton(self.preview_action_bar, text="更多操作", style="CompactTool.TMenubutton")
        self.preview_more_actions_menu = tk.Menu(self.preview_more_actions_button, tearoff=False)
        self.preview_more_actions_menu.add_command(label="刷新当前聊天预览", command=lambda: self.refresh_selected_conversation_preview(force=True))
        self.preview_more_actions_menu.add_command(label="打开最近整理时间线", command=self.open_latest_organized_timeline)
        self.preview_more_actions_menu.add_command(label="打开整理目录", command=self.open_organized_dir)
        self.preview_more_actions_button["menu"] = self.preview_more_actions_menu
        self.preview_action_buttons.append(self.preview_more_actions_button)

        self.preview_summary_info_card = info_card
        self.preview_header_frame = header_frame
        self.preview_header_text_frame = header_text_frame
        info_card.bind("<Configure>", self.on_preview_summary_card_configure)
        self.apply_preview_header_layout()

        def add_info_field(row_index, column_index, title, variable, colspan=1):
            ttk.Label(info_card, text=title, style="CardLabel.TLabel").grid(
                row=row_index, column=column_index, columnspan=colspan, sticky="w", padx=12, pady=(2, 0)
            )
            ttk.Label(
                info_card,
                textvariable=variable,
                style="CardValue.TLabel",
                wraplength=220 * colspan,
                justify="left",
            ).grid(
                row=row_index + 1, column=column_index, columnspan=colspan, sticky="w", padx=12, pady=(1, 4)
            )

        add_info_field(1, 0, "会话类型", self.preview_kind_var)
        add_info_field(1, 1, "消息 / 发送人", self.preview_stats_var)
        add_info_field(1, 2, "时间范围", self.preview_range_var)
        # 标题下方的副标题已经展示当前状态，这里不再重复渲染“当前提示”，把更多高度留给会话列表。
        self.set_preview_summary_details_visible(False)
        return frame

    def build_conversation_list_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="会话列表", padding=10)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(3, weight=1)

        ttk.Label(
            frame,
            text="单击会话会在右侧加载完整聊天内容；双击仍可直接整理导出。",
            style="Hint.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        filter_row = ttk.Frame(frame)
        filter_row.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        filter_row.grid_columnconfigure(1, weight=1)
        self.filter_row = filter_row

        ttk.Label(filter_row, text="筛选会话").grid(row=0, column=0, padx=(0, 6))
        self.conversation_filter_combo = ttk.Combobox(
            filter_row,
            textvariable=self.conversation_filter_var,
            state="readonly",
            values=[item[0] for item in CONVERSATION_FILTERS],
            width=20,
        )
        self.conversation_filter_combo.grid(row=0, column=1, sticky="w")
        self.conversation_filter_combo.bind("<<ComboboxSelected>>", self.on_conversation_filter_changed)

        button_grid = ttk.Frame(frame)
        button_grid.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.organize_button_grid = button_grid

        self.scan_conversations_button = ttk.Button(
            button_grid,
            text="重新读取会话",
            style="CompactTool.TButton",
            command=self.start_conversation_scan,
        )
        self.refresh_preview_button = ttk.Button(
            button_grid,
            text="刷新当前聊天预览",
            style="CompactTool.TButton",
            command=lambda: self.refresh_selected_conversation_preview(force=True),
        )
        self.organize_selected_button = ttk.Button(
            button_grid,
            text="导出当前会话",
            style="CompactTool.TButton",
            command=self.start_organize_selected,
        )
        self.organize_all_button = ttk.Button(
            button_grid,
            text="批量导出当前筛选结果",
            style="CompactTool.TButton",
            command=self.start_organize_filtered,
        )
        self.organize_more_button = ttk.Menubutton(button_grid, text="更多操作", style="CompactTool.TMenubutton")
        self.organize_more_menu = tk.Menu(self.organize_more_button, tearoff=False)
        self.organize_more_menu.add_command(label="打开导出文件夹", command=self.open_organized_dir)
        self.organize_more_menu.add_command(label="打开最近导出索引", command=self.open_organized_index)
        self.organize_more_menu.add_command(label="打开全部会话总表", command=self.open_all_conversations_index)
        self.organize_more_menu.add_command(label="打开运行日志", command=self.select_log_tab)
        self.organize_more_button["menu"] = self.organize_more_menu
        button_grid.bind("<Configure>", self.on_organize_button_grid_configure)
        self.apply_organize_button_layout()

        columns = (
            "conversation_id",
            "counterpart_id",
            "sender_count",
            "conversation_display_name",
            "last_time",
            "message_count",
            "conversation_kind_label",
        )
        tree = ttk.Treeview(
            frame,
            columns=columns,
            displaycolumns=("conversation_display_name", "last_time", "message_count", "conversation_kind_label"),
            show="headings",
            height=11,
        )
        tree.heading("conversation_display_name", text="群名称")
        tree.heading("last_time", text="最近时间")
        tree.heading("message_count", text="消息数")
        tree.heading("conversation_kind_label", text="类型")
        tree.column("conversation_display_name", width=260, minwidth=220, anchor="w", stretch=True)
        tree.column("last_time", width=145, minwidth=140, anchor="center", stretch=False)
        tree.column("message_count", width=70, minwidth=70, anchor="center", stretch=False)
        tree.column("conversation_kind_label", width=110, minwidth=100, anchor="center", stretch=False)
        tree.grid(row=3, column=0, sticky="nsew")
        tree.bind("<<TreeviewSelect>>", self.on_conversation_selected)
        tree.bind("<Double-1>", lambda _event: self.start_organize_selected())

        y_scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        y_scrollbar.grid(row=3, column=1, sticky="ns")
        x_scrollbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        x_scrollbar.grid(row=4, column=0, sticky="ew", pady=(6, 0))
        tree.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)

        ttk.Label(frame, textvariable=self.conversation_hint_var, style="Hint.TLabel").grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.conversation_tree = tree
        self.update_conversation_tree_heading()
        return frame

    def build_organize_controls_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="恢复设置与执行", padding=10)
        frame.grid_columnconfigure(0, weight=1)
        self.organize_controls_container = frame
        self.organize_hint_label = None

        step_notebook = ttk.Notebook(frame)
        step_notebook.grid(row=0, column=0, sticky="ew")
        self.organize_step_notebook = step_notebook

        # 将“设置恢复目标”和“执行恢复”收进页签，并进一步收紧内边距，避免默认首屏再出现内容被裁掉。
        target_frame = ttk.Frame(step_notebook, padding=6)
        target_frame.grid_columnconfigure(1, weight=1)
        self.target_frame = target_frame

        self.target_corp_label = ttk.Label(target_frame, text="企业账号ID")
        self.target_corp_label.grid(row=0, column=0, sticky="w")
        self.target_corp_entry = ttk.Entry(target_frame, textvariable=self.corp_id_var)
        self.target_corp_entry.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        self.target_refresh_button = ttk.Button(target_frame, text="查找企业账号", command=self.refresh_corp_dirs)
        self.target_refresh_button.grid(row=0, column=2, sticky="ew", padx=(0, 4))
        self.target_scan_button = ttk.Button(target_frame, text="查找正在运行的企业微信", command=self.start_process_scan)
        self.target_scan_button.grid(row=0, column=3, sticky="ew")

        self.target_pid_label = ttk.Label(target_frame, text="可选 PID")
        self.target_pid_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.target_pid_entry = ttk.Entry(target_frame, textvariable=self.pid_var)
        self.target_pid_entry.grid(row=1, column=1, sticky="ew", padx=(6, 6), pady=(4, 0))
        self.target_clear_pid_button = ttk.Button(target_frame, text="清空 PID", command=lambda: self.pid_var.set(""))
        self.target_clear_pid_button.grid(row=1, column=2, sticky="ew", padx=(0, 4), pady=(4, 0))
        self.run_button = ttk.Button(target_frame, text="立即提取一次聊天数据", style="Accent.TButton", command=self.start_recovery)
        self.run_button.grid(row=1, column=3, sticky="ew", pady=(4, 0))

        self.target_output_label = ttk.Label(target_frame, text="输出根目录")
        self.target_output_label.grid(row=2, column=0, sticky="w", pady=(4, 0))
        self.target_output_entry = ttk.Entry(target_frame, textvariable=self.output_dir_var)
        self.target_output_entry.grid(row=2, column=1, sticky="ew", padx=(6, 6), pady=(4, 0))
        self.output_buttons_frame = ttk.Frame(target_frame)
        self.output_buttons_frame.grid(row=2, column=2, columnspan=2, sticky="e", pady=(4, 0))
        self.target_browse_button = ttk.Button(self.output_buttons_frame, text="浏览", command=self.choose_output_dir)
        self.target_browse_button.grid(row=0, column=0, padx=(0, 4))
        self.target_open_output_button = ttk.Button(self.output_buttons_frame, text="打开", command=self.open_output_dir)
        self.target_open_output_button.grid(row=0, column=1)

        self.target_selection_hint_label = ttk.Label(
            target_frame,
            textvariable=self.selection_hint_var,
            style="Hint.TLabel",
            wraplength=560,
            justify="left",
        )
        self.target_selection_hint_label.grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))

        realtime_frame = ttk.Frame(step_notebook, padding=6)
        realtime_frame.grid_columnconfigure(4, weight=1)
        self.realtime_frame = realtime_frame
        ttk.Label(realtime_frame, text="间隔秒数").grid(row=0, column=0, padx=(0, 8))
        self.realtime_interval_entry = ttk.Entry(realtime_frame, textvariable=self.realtime_interval_var, width=10)
        self.realtime_interval_entry.grid(row=0, column=1, sticky="w")
        self.realtime_start_button = ttk.Button(realtime_frame, text="开启实时恢复", command=self.start_realtime_recovery)
        self.realtime_start_button.grid(row=0, column=2, padx=(8, 6))
        self.realtime_stop_button = ttk.Button(realtime_frame, text="停止实时恢复", command=self.stop_realtime_recovery_by_user)
        self.realtime_stop_button.grid(row=0, column=3, padx=(0, 6))
        self.realtime_help_label = ttk.Label(
            realtime_frame,
            text="开启后会按间隔自动重跑恢复，并自动刷新最新 sqlite、会话列表和当前聊天预览；如果 PID 留空，会自动尝试全部匹配进程。",
            style="Hint.TLabel",
            wraplength=520,
            justify="left",
        )
        self.realtime_help_label.grid(row=1, column=0, columnspan=5, sticky="w", pady=(6, 0))
        ttk.Label(realtime_frame, textvariable=self.realtime_status_var, style="Hint.TLabel").grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(4, 0)
        )
        ttk.Label(realtime_frame, textvariable=self.realtime_last_run_var, style="Hint.TLabel").grid(
            row=3, column=0, columnspan=5, sticky="w", pady=(4, 0)
        )

        step_notebook.add(target_frame, text="第 1 步：设置恢复目标")
        step_notebook.add(realtime_frame, text="第 2 步：执行恢复")

        target_frame.bind("<Configure>", self.on_target_frame_configure)
        self.apply_target_layout()

        return frame

    def build_browser_workspace_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="聊天内容与日志", padding=8)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        notebook = ttk.Notebook(frame)
        notebook.grid(row=0, column=0, sticky="nsew")

        chat_browser_frame = self.build_chat_browser_frame(notebook, embedded=True)
        log_frame = self.build_log_frame(notebook, embedded=True)
        notebook.add(chat_browser_frame, text=f"聊天预览（{preview_limit_text(PREVIEW_MESSAGE_LIMIT)}）")
        notebook.add(log_frame, text="运行日志")

        self.browser_notebook = notebook
        self.chat_browser_frame = chat_browser_frame
        self.log_frame = log_frame
        return frame

    def build_chat_browser_frame(self, parent, embedded: bool = False):
        if embedded:
            frame = ttk.Frame(parent, padding=12)
        else:
            frame = ttk.LabelFrame(parent, text=f"聊天内容浏览（{preview_limit_text(PREVIEW_MESSAGE_LIMIT)}）", padding=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        preview_text = tk.Text(
            frame,
            wrap=tk.WORD,
            font=("Microsoft YaHei UI", 10),
            bg="#ffffff",
            fg="#1f2937",
            insertbackground="#1f2937",
            relief=tk.FLAT,
            padx=10,
            pady=10,
        )
        preview_text.grid(row=0, column=0, sticky="nsew")
        preview_text.tag_configure("meta", foreground="#64748b", font=("Consolas", 9))
        preview_text.tag_configure("sender", foreground="#0f4c81", font=("Microsoft YaHei UI", 10, "bold"))
        preview_text.tag_configure("type", foreground="#a16207", font=("Microsoft YaHei UI", 9, "bold"))
        preview_text.tag_configure("quote", foreground="#475569", background="#f8fafc", lmargin1=18, lmargin2=18, spacing1=3, spacing3=6)
        preview_text.tag_configure("body", foreground="#1f2937", spacing3=10)
        preview_text.tag_configure("separator", foreground="#cbd5e1")
        preview_text.insert(tk.END, "选择左侧会话后，这里会显示完整聊天内容，并在命中本地图片时直接显示缩略图。\n", "body")
        preview_text.configure(state="disabled")

        preview_y = ttk.Scrollbar(frame, orient="vertical", command=preview_text.yview)
        preview_y.grid(row=0, column=1, sticky="ns")
        preview_x = ttk.Scrollbar(frame, orient="horizontal", command=preview_text.xview)
        preview_x.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        preview_text.configure(yscrollcommand=preview_y.set, xscrollcommand=preview_x.set)

        ttk.Label(frame, textvariable=self.preview_status_var, style="Hint.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.preview_text = preview_text
        return frame

    def build_log_frame(self, parent, embedded: bool = False):
        if embedded:
            frame = ttk.Frame(parent, padding=12)
        else:
            frame = ttk.LabelFrame(parent, text="运行日志", padding=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(0, weight=1)

        log_panel = ttk.Frame(frame)
        log_panel.grid(row=0, column=0, sticky="nsew")
        log_panel.grid_columnconfigure(0, weight=1)
        log_panel.grid_rowconfigure(0, weight=1)

        text = tk.Text(
            log_panel,
            wrap=tk.NONE,
            font=("Consolas", 10),
            bg="#0f1720",
            fg="#d9e2ec",
            insertbackground="#d9e2ec",
        )
        text.grid(row=0, column=0, sticky="nsew")

        y_scrollbar = ttk.Scrollbar(log_panel, orient="vertical", command=text.yview)
        y_scrollbar.grid(row=0, column=1, sticky="ns")
        x_scrollbar = ttk.Scrollbar(log_panel, orient="horizontal", command=text.xview)
        x_scrollbar.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        text.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)

        text.insert(tk.END, "图形界面已初始化。\n")
        text.configure(state="disabled")
        self.log_widget = text
        return frame

    def build_results_frame(self, parent):
        frame = ttk.LabelFrame(parent, text="输出文件", padding=12)
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        toolbar = ttk.Frame(frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.grid_columnconfigure(4, weight=1)

        ttk.Button(toolbar, text="打开所选文件", command=self.open_selected_result).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(toolbar, text="打开输出目录", command=self.open_output_dir).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(toolbar, text="刷新", command=self.refresh_results).grid(row=0, column=2)

        columns = ("name", "size", "modified")
        tree = ttk.Treeview(frame, columns=columns, show="headings", height=10)
        tree.heading("name", text="文件名")
        tree.heading("size", text="大小")
        tree.heading("modified", text="修改时间")
        tree.column("name", width=420, minwidth=240, anchor="w", stretch=True)
        tree.column("size", width=90, minwidth=80, anchor="center", stretch=False)
        tree.column("modified", width=160, minwidth=150, anchor="center", stretch=False)
        tree.grid(row=1, column=0, sticky="nsew")
        tree.bind("<Double-1>", lambda _event: self.open_selected_result())

        y_scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        y_scrollbar.grid(row=1, column=1, sticky="ns")
        x_scrollbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        x_scrollbar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        tree.configure(yscrollcommand=y_scrollbar.set, xscrollcommand=x_scrollbar.set)

        self.results_tree = tree
        return frame

    def initialize_responsive_layout(self):
        """
        启动时直接应用稳定首屏布局，不再依赖 PanedWindow 默认分栏比例和用户手动拖拽修正。
        """
        self.root.update_idletasks()
        self.refresh_responsive_layout()

    def on_root_configure(self, event):
        if event.widget is not self.root:
            return
        current_size = (event.width, event.height)
        if current_size == self._last_configure_size:
            return
        self._last_configure_size = current_size
        if self._resize_after_id:
            self.root.after_cancel(self._resize_after_id)
        self._resize_after_id = self.root.after(RESIZE_DEBOUNCE_MS, self.refresh_responsive_layout)

    def refresh_responsive_layout(self):
        self._resize_after_id = None
        self.apply_main_layout_mode()
        self.apply_header_compaction()
        self.resize_tree_columns()
        self.apply_preview_header_layout()
        self.apply_target_layout()
        self.apply_organize_button_layout()

    def on_preview_summary_card_configure(self, _event=None):
        self.apply_preview_header_layout()

    def on_preview_summary_tab_changed(self, _event=None):
        """
        左侧摘要改为页签后，需要在用户手动切换页签时同步记录当前是否停留在“当前会话概览”。
        """
        summary_notebook = getattr(self, "preview_summary_notebook", None)
        detail_tab = getattr(self, "preview_summary_detail_tab", None)
        if not summary_notebook or not detail_tab:
            return

        try:
            selected_tab = summary_notebook.select()
        except tk.TclError:
            return

        self.preview_summary_details_visible = str(selected_tab) == str(detail_tab)
        if self.preview_summary_details_visible:
            self.apply_preview_header_layout()

    def set_preview_summary_details_visible(self, visible: bool):
        """
        左侧摘要区改为“工作概览 / 当前会话概览”页签后，通过切换页签避免两块内容上下同时展开。
        """
        summary_notebook = getattr(self, "preview_summary_notebook", None)
        overview_tab = getattr(self, "preview_summary_overview_tab", None)
        detail_tab = getattr(self, "preview_summary_detail_tab", None)
        if not summary_notebook or not overview_tab or not detail_tab:
            return

        target_tab = detail_tab if visible else overview_tab
        if bool(visible) == self.preview_summary_details_visible:
            try:
                if str(summary_notebook.select()) != str(target_tab):
                    summary_notebook.select(target_tab)
            except tk.TclError:
                pass
            return

        try:
            summary_notebook.select(target_tab)
        except tk.TclError:
            return

        self.preview_summary_details_visible = bool(visible)
        if visible:
            self.apply_preview_header_layout()

    def apply_preview_header_layout(self, available_width=None):
        """
        根据摘要卡当前可用宽度在“标题右侧按钮”和“标题下方按钮”之间切换，避免长会话名把主操作按钮挤出可视区。
        """
        info_card = getattr(self, "preview_summary_info_card", None)
        action_bar = getattr(self, "preview_action_bar", None)
        title_label = getattr(self, "preview_title_label", None)
        subtitle_label = getattr(self, "preview_subtitle_label", None)
        if not info_card or not action_bar or not title_label or not subtitle_label:
            return
        if not info_card.winfo_exists():
            return

        width = int(available_width or info_card.winfo_width() or 0)
        if width <= 0:
            return

        wide_mode = width >= 760
        stack_buttons = width < 540
        layout_mode = f"{'wide' if wide_mode else 'compact'}_{'stacked' if stack_buttons else 'inline'}"
        if layout_mode != self.preview_header_layout_mode:
            action_bar.grid_forget()
            if wide_mode:
                action_bar.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))
            else:
                action_bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

            for index, button in enumerate(getattr(self, "preview_action_buttons", [])):
                button.grid_forget()
                if stack_buttons:
                    pady = (0, 4) if index < len(self.preview_action_buttons) - 1 else 0
                    button.grid(row=index, column=0, sticky="ew", pady=pady)
                else:
                    padx = (0, 4) if index < len(self.preview_action_buttons) - 1 else 0
                    button.grid(row=0, column=index, sticky="ew", padx=padx)

            self.preview_header_layout_mode = layout_mode

        title_wrap = max(240, width - (330 if wide_mode else 40))
        subtitle_wrap = max(300, width - (330 if wide_mode else 40))
        title_label.configure(wraplength=title_wrap)
        subtitle_label.configure(wraplength=subtitle_wrap)

    def apply_main_layout_mode(self, total_width=None):
        """
        根据窗口宽度在固定双栏和上下双层之间切换。
        默认由程序控制首屏分区，不再使用可拖拽分栏决定界面是否完整可用。
        """
        total_width = int(total_width or self.root.winfo_width() or 0)
        if total_width <= 0:
            return

        content = getattr(self, "content_frame", None)
        left_wrapper = getattr(self, "left_wrapper", None)
        right_wrapper = getattr(self, "right_wrapper", None)
        if not content or not left_wrapper or not right_wrapper:
            return

        compact_mode = total_width < 860
        target_mode = "stacked" if compact_mode else "split"
        if target_mode == self.main_layout_mode:
            return

        left_wrapper.grid_forget()
        right_wrapper.grid_forget()

        if compact_mode:
            total_height = int(self.root.winfo_height() or 0)
            top_minsize = 260 if 0 < total_height < 760 else 320
            bottom_minsize = 280 if 0 < total_height < 760 else 360
            self.left_wrapper.configure(padding=(0, 0, 0, 8))
            self.right_wrapper.configure(padding=(0, 8, 0, 0))
            content.grid_columnconfigure(0, weight=1, minsize=0, uniform="")
            content.grid_columnconfigure(1, weight=0, minsize=0, uniform="")
            content.grid_rowconfigure(0, weight=1, minsize=top_minsize)
            content.grid_rowconfigure(1, weight=1, minsize=bottom_minsize)
            self.right_wrapper.grid_rowconfigure(0, weight=0)
            self.right_wrapper.grid_rowconfigure(1, weight=1)
            self.organize_controls_frame.grid_propagate(True)
            self.browser_workspace_frame.grid_propagate(True)
            left_wrapper.grid(row=0, column=0, sticky="nsew")
            right_wrapper.grid(row=1, column=0, sticky="nsew")
        else:
            self.left_wrapper.configure(padding=(0, 0, 8, 0))
            self.right_wrapper.configure(padding=(8, 0, 0, 0))
            # 双栏模式下左右保持 1:1，避免全屏时左侧明显宽于右侧。
            content.grid_columnconfigure(0, weight=1, minsize=SPLIT_PANEL_MIN_WIDTH, uniform="main_split")
            content.grid_columnconfigure(1, weight=1, minsize=SPLIT_PANEL_MIN_WIDTH, uniform="main_split")
            content.grid_rowconfigure(0, weight=1, minsize=0)
            content.grid_rowconfigure(1, weight=0, minsize=0)
            self.right_wrapper.grid_rowconfigure(0, weight=3)
            self.right_wrapper.grid_rowconfigure(1, weight=7)
            self.organize_controls_frame.grid_propagate(False)
            self.browser_workspace_frame.grid_propagate(False)
            left_wrapper.grid(row=0, column=0, sticky="nsew")
            right_wrapper.grid(row=0, column=1, sticky="nsew")
        self.main_layout_mode = target_mode
        self.apply_right_panel_ratio()

    def apply_header_compaction(self, total_width=None):
        """
        顶部和控制区的说明文字在中小窗口下自动收起，把可用高度留给会话列表和聊天预览。
        """
        header_hint_label = getattr(self, "header_hint_label", None)
        summary_hint_label = getattr(self, "summary_card_hint_label", None)
        realtime_help_label = getattr(self, "realtime_help_label", None)
        if not header_hint_label or not summary_hint_label:
            return

        total_width = int(total_width or self.root.winfo_width() or 0)
        total_height = int(self.root.winfo_height() or 0)
        if total_width <= 0:
            return

        compact_mode = total_width < 1320 or (0 < total_height < 860)
        compact_controls_mode = total_width < 1320 or (0 < total_height < 860)
        target_mode = f"{'compact' if compact_mode else 'full'}_{'tight' if compact_controls_mode else 'normal'}"
        if target_mode == self.header_compact_mode:
            return

        if compact_mode:
            header_hint_label.grid_remove()
            summary_hint_label.grid_remove()
        else:
            header_hint_label.grid()
            summary_hint_label.grid()

        if realtime_help_label:
            if compact_controls_mode:
                realtime_help_label.grid_remove()
            else:
                realtime_help_label.grid()

        target_selection_hint_label = getattr(self, "target_selection_hint_label", None)
        if target_selection_hint_label:
            if compact_controls_mode:
                target_selection_hint_label.grid_remove()
            else:
                target_selection_hint_label.grid()
        self.header_compact_mode = target_mode

    def on_right_wrapper_configure(self, _event=None):
        self.apply_right_panel_ratio()

    def apply_right_panel_ratio(self, available_height=None):
        """
        右侧“恢复设置与执行 / 聊天内容与日志”优先按 3:7 分配高度；
        如果步骤页签的实际请求高度更高，则优先补足上半区，避免默认首屏内容被裁掉。
        """
        right_wrapper = getattr(self, "right_wrapper", None)
        organize_controls_frame = getattr(self, "organize_controls_frame", None)
        browser_workspace_frame = getattr(self, "browser_workspace_frame", None)
        step_notebook = getattr(self, "organize_step_notebook", None)
        if not right_wrapper or not organize_controls_frame or not browser_workspace_frame:
            return
        if not right_wrapper.winfo_exists():
            return

        if self.main_layout_mode != "split":
            if self.right_panel_ratio_mode != "auto":
                organize_controls_frame.configure(height=1)
                browser_workspace_frame.configure(height=1)
                self.right_panel_ratio_mode = "auto"
            return

        height = int(available_height or right_wrapper.winfo_height() or 0)
        if height <= 0:
            return

        usable_height = max(0, height - RIGHT_PANEL_SECTION_GAP)
        if usable_height <= 1:
            return

        top_height = max(1, int(round(usable_height * RIGHT_PANEL_TOP_RATIO)))
        if step_notebook and step_notebook.winfo_exists():
            required_top_height = step_notebook.winfo_reqheight() + RIGHT_PANEL_CONTENT_BUFFER
            # 顶部步骤页内容必须完整显示，避免默认首屏再次出现下半截被裁掉。
            top_height = max(top_height, required_top_height)
        top_height = min(usable_height - 1, top_height)
        bottom_height = max(1, usable_height - top_height)
        organize_controls_frame.configure(height=top_height)
        browser_workspace_frame.configure(height=bottom_height)
        self.right_panel_ratio_mode = f"split_{top_height}_{bottom_height}"

    def on_target_frame_configure(self, _event=None):
        self.apply_target_layout()

    def apply_target_layout(self, available_width=None):
        """
        恢复目标表单在窄宽度下改成两列堆叠，避免输入框和按钮横向硬挤。
        """
        target_frame = getattr(self, "target_frame", None)
        if not target_frame or not target_frame.winfo_exists():
            return

        width = int(available_width or target_frame.winfo_width() or 0)
        if width <= 0:
            return

        compact_mode = width < TARGET_LAYOUT_WIDE_THRESHOLD
        target_mode = "compact" if compact_mode else "wide"
        if target_mode == self.organize_layout_mode:
            return

        widgets = [
            self.target_corp_label,
            self.target_corp_entry,
            self.target_refresh_button,
            self.target_scan_button,
            self.target_pid_label,
            self.target_pid_entry,
            self.target_clear_pid_button,
            self.run_button,
            self.target_output_label,
            self.target_output_entry,
            self.output_buttons_frame,
            self.target_selection_hint_label,
        ]
        for widget in widgets:
            widget.grid_forget()

        if compact_mode:
            self.target_corp_label.grid(row=0, column=0, sticky="w")
            self.target_corp_entry.grid(row=0, column=1, columnspan=3, sticky="ew", padx=(8, 0))
            self.target_refresh_button.grid(row=1, column=1, sticky="ew", pady=(6, 0))
            self.target_scan_button.grid(row=1, column=2, columnspan=2, sticky="ew", padx=(8, 0), pady=(6, 0))

            self.target_pid_label.grid(row=2, column=0, sticky="w", pady=(6, 0))
            self.target_pid_entry.grid(row=2, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
            self.target_clear_pid_button.grid(row=3, column=1, sticky="ew", pady=(6, 0))
            self.run_button.grid(row=3, column=2, columnspan=2, sticky="ew", padx=(8, 0), pady=(6, 0))

            self.target_output_label.grid(row=4, column=0, sticky="w", pady=(6, 0))
            self.target_output_entry.grid(row=4, column=1, columnspan=3, sticky="ew", padx=(8, 0), pady=(6, 0))
            self.output_buttons_frame.grid(row=5, column=1, columnspan=3, sticky="e", pady=(6, 0))
            self.target_selection_hint_label.grid(row=6, column=0, columnspan=4, sticky="w", pady=(8, 0))
        else:
            self.target_corp_label.grid(row=0, column=0, sticky="w")
            self.target_corp_entry.grid(row=0, column=1, sticky="ew", padx=(6, 6))
            self.target_refresh_button.grid(row=0, column=2, sticky="ew", padx=(0, 4))
            self.target_scan_button.grid(row=0, column=3, sticky="ew")

            self.target_pid_label.grid(row=1, column=0, sticky="w", pady=(4, 0))
            self.target_pid_entry.grid(row=1, column=1, sticky="ew", padx=(6, 6), pady=(4, 0))
            self.target_clear_pid_button.grid(row=1, column=2, sticky="ew", padx=(0, 4), pady=(4, 0))
            self.run_button.grid(row=1, column=3, sticky="ew", pady=(4, 0))

            self.target_output_label.grid(row=2, column=0, sticky="w", pady=(4, 0))
            self.target_output_entry.grid(row=2, column=1, sticky="ew", padx=(6, 6), pady=(4, 0))
            self.output_buttons_frame.grid(row=2, column=2, columnspan=2, sticky="e", pady=(4, 0))
            self.target_selection_hint_label.grid(row=3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        self.organize_layout_mode = target_mode
    def on_organize_button_grid_configure(self, _event=None):
        self.apply_organize_button_layout()

    def apply_organize_button_layout(self, available_width=None):
        """
        会话列表工具条在默认窗口下优先使用更省高度的紧凑布局，把更多垂直空间留给树表。
        """
        button_grid = getattr(self, "organize_button_grid", None)
        if not button_grid or not button_grid.winfo_exists():
            return

        width = int(available_width or button_grid.winfo_width() or 0)
        if width <= 0:
            return

        compact_mode = width < ORGANIZE_BUTTON_DENSE_THRESHOLD
        stacked_mode = width < ORGANIZE_BUTTON_STACK_THRESHOLD
        for column_index in range(3):
            button_grid.grid_columnconfigure(column_index, weight=0)

        buttons = [
            self.scan_conversations_button,
            self.refresh_preview_button,
            self.organize_selected_button,
            self.organize_all_button,
            self.organize_more_button,
        ]
        for button in buttons:
            button.grid_forget()

        if stacked_mode:
            button_grid.grid_columnconfigure(0, weight=1)
            button_grid.grid_columnconfigure(1, weight=1)
            self.scan_conversations_button.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
            self.refresh_preview_button.grid(row=0, column=1, sticky="ew", pady=(0, 4))
            self.organize_selected_button.grid(row=1, column=0, sticky="ew", padx=(0, 4))
            self.organize_all_button.grid(row=1, column=1, sticky="ew")
            self.organize_more_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        elif compact_mode:
            for column_index in range(3):
                button_grid.grid_columnconfigure(column_index, weight=1)
            self.scan_conversations_button.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))
            self.refresh_preview_button.grid(row=0, column=1, sticky="ew", padx=4, pady=(0, 4))
            self.organize_more_button.grid(row=0, column=2, sticky="ew", padx=(4, 0), pady=(0, 4))
            self.organize_selected_button.grid(row=1, column=0, sticky="ew", padx=(0, 4))
            self.organize_all_button.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(4, 0))
        else:
            for column_index in range(3):
                button_grid.grid_columnconfigure(column_index, weight=1)
            self.scan_conversations_button.grid(row=0, column=0, sticky="ew", padx=(0, 6), pady=(0, 6))
            self.refresh_preview_button.grid(row=0, column=1, sticky="ew", padx=6, pady=(0, 6))
            self.organize_selected_button.grid(row=0, column=2, sticky="ew", padx=(6, 0), pady=(0, 6))
            self.organize_all_button.grid(row=1, column=0, columnspan=2, sticky="ew", padx=(0, 6))
            self.organize_more_button.grid(row=1, column=2, sticky="ew", padx=(6, 0))

    def open_preview_details_dialog(self):
        """
        默认只显示会话概览，详细字段通过弹窗查看，避免摘要卡在窄窗口里被细节挤爆。
        """
        detail_window = tk.Toplevel(self.root)
        detail_window.title("当前会话详情")
        detail_window.transient(self.root)
        detail_window.resizable(True, True)
        detail_window.geometry("620x420")
        detail_window.minsize(540, 360)

        container = ttk.Frame(detail_window, padding=16)
        container.grid(row=0, column=0, sticky="nsew")
        detail_window.grid_columnconfigure(0, weight=1)
        detail_window.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(1, weight=1)

        detail_items = [
            ("会话名称", self.preview_title_var.get()),
            ("会话类型", self.preview_kind_var.get()),
            ("会话 ID", self.preview_id_var.get()),
            ("对方 ID", self.preview_counterpart_var.get()),
            ("消息 / 发送人", self.preview_stats_var.get()),
            ("时间范围", self.preview_range_var.get()),
            ("高频发送人", self.preview_participants_var.get()),
            ("命名依据", self.preview_evidence_var.get()),
            ("当前提示", self.preview_status_var.get()),
        ]
        for row_index, (title, value) in enumerate(detail_items):
            ttk.Label(container, text=title).grid(row=row_index, column=0, sticky="nw", pady=(0, 10))
            ttk.Label(container, text=value or "-", justify="left", wraplength=420).grid(
                row=row_index, column=1, sticky="nw", pady=(0, 10), padx=(12, 0)
            )

        ttk.Button(container, text="关闭", command=detail_window.destroy).grid(row=len(detail_items), column=1, sticky="e", pady=(8, 0))

    def select_log_tab(self):
        notebook = getattr(self, "browser_notebook", None)
        log_frame = getattr(self, "log_frame", None)
        if notebook and log_frame:
            notebook.select(log_frame)

    def select_chat_tab(self):
        notebook = getattr(self, "browser_notebook", None)
        chat_frame = getattr(self, "chat_browser_frame", None)
        if notebook and chat_frame:
            notebook.select(chat_frame)

    def resize_tree_columns(self):
        self.resize_tree_stretch_column(getattr(self, "corp_tree", None), "corp_id", {"has_db": 100, "modified": 170}, min_width=150)
        self.resize_tree_stretch_column(getattr(self, "process_tree", None), "sample", {"pid": 80, "wow64": 80, "hits": 90}, min_width=180)
        conversation_tree = getattr(self, "conversation_tree", None)
        if conversation_tree and conversation_tree.winfo_exists():
            total_width = conversation_tree.winfo_width()
            if total_width < 540:
                conversation_tree.configure(displaycolumns=("conversation_display_name", "last_time"))
                fixed_columns = {"last_time": 145}
            elif total_width < 700:
                conversation_tree.configure(displaycolumns=("conversation_display_name", "last_time", "message_count"))
                fixed_columns = {"last_time": 145, "message_count": 70}
            else:
                conversation_tree.configure(displaycolumns=("conversation_display_name", "last_time", "message_count", "conversation_kind_label"))
                fixed_columns = {"last_time": 145, "message_count": 70, "conversation_kind_label": 110}
            self.resize_tree_stretch_column(conversation_tree, "conversation_display_name", fixed_columns, min_width=220)
        self.resize_tree_stretch_column(getattr(self, "results_tree", None), "name", {"size": 90, "modified": 160}, min_width=240)

    def resize_tree_stretch_column(self, tree, stretch_column: str, fixed_columns: dict, min_width: int = 160):
        if not tree or not tree.winfo_exists():
            return
        total_width = tree.winfo_width()
        if total_width < 120:
            return

        padding = 28
        available = max(total_width - padding, min_width)
        fixed_total = sum(fixed_columns.values())
        stretch_width = max(min_width, available - fixed_total)

        for column_name, width in fixed_columns.items():
            tree.column(column_name, width=width, minwidth=width, stretch=False)
        tree.column(stretch_column, width=stretch_width, minwidth=min_width, stretch=True)

    def detect_docs_dir(self):
        candidates = self.wxwork_docs_dir_candidates()
        seen = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists():
                return candidate
        return candidates[0] if candidates else None

    def wxwork_docs_dir_candidates(self):
        """
        汇总企业微信常见文档目录；部分商务电脑把“文档”同步到了 OneDrive，不能只查 USERPROFILE\\Documents。
        """
        candidates = []
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            profile_path = Path(userprofile)
            candidates.append(profile_path / "Documents" / "WXWork")
            candidates.append(profile_path / "OneDrive" / "Documents" / "WXWork")
            for one_drive_dir in profile_path.glob("OneDrive*"):
                candidates.append(one_drive_dir / "Documents" / "WXWork")
                candidates.append(one_drive_dir / "文档" / "WXWork")
        for env_name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
            env_path = os.environ.get(env_name)
            if env_path:
                candidates.append(Path(env_path) / "Documents" / "WXWork")
                candidates.append(Path(env_path) / "文档" / "WXWork")
        candidates.append(Path.home() / "Documents" / "WXWork")

        unique_candidates = []
        seen = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            unique_candidates.append(candidate)
        return unique_candidates

    def corp_id_manual_help_text(self):
        """
        生成企业账号 ID 手动查找提示，避免没有自动识别时用户不知道去哪里找数字目录。
        """
        candidates = self.wxwork_docs_dir_candidates()
        path_lines = "\n".join(f"- {path}" for path in candidates[:6])
        return (
            "没有自动识别到企业账号ID时，可以到企业微信文档目录里找数字文件夹名。\n\n"
            "常见位置：\n"
            f"{path_lines}\n\n"
            "打开 WXWork 目录后，形如 1688857851789652 的纯数字文件夹名就是企业账号ID；"
            "如果这些目录不存在，请先打开并登录企业微信，再点击“查找正在运行的企业微信”。"
        )

    def find_recovery_script(self):
        candidates = [
            self.resource_dir / "recover_wxwork_partial_messages.py",
            self.base_dir / "recover_wxwork_partial_messages.py",
        ]
        seen = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists():
                return candidate
        return candidates[0]

    def find_organizer_script(self):
        candidates = [
            self.resource_dir / "organize_wxwork_recovered_messages.py",
            self.base_dir / "organize_wxwork_recovered_messages.py",
        ]
        seen = set()
        for candidate in candidates:
            key = str(candidate).lower()
            if key in seen:
                continue
            seen.add(key)
            if candidate.exists():
                return candidate
        return candidates[0]

    def load_engine(self, force=False):
        if self.engine_module is not None and not force:
            return self.engine_module

        path = self.find_recovery_script()
        self.recovery_script_path = path
        self.engine_path_var.set(str(path))
        if not path.exists():
            raise FileNotFoundError(f"未找到恢复脚本：{path}")

        spec = importlib.util.spec_from_file_location("recover_wxwork_partial_messages", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法从以下路径加载恢复引擎：{path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.engine_module = module
        return module

    def load_organizer(self, force=False):
        if self.organizer_module is not None and not force:
            return self.organizer_module

        path = self.find_organizer_script()
        self.organizer_script_path = path
        self.organizer_path_var.set(str(path))
        if not path.exists():
            raise FileNotFoundError(f"未找到整理脚本：{path}")

        spec = importlib.util.spec_from_file_location("organize_wxwork_recovered_messages", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法从以下路径加载整理脚本：{path}")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.organizer_module = module
        return module

    def get_organize_output_dir(self):
        base = Path(self.output_dir_var.get().strip() or self.default_output_dir)
        path = base / "organized_external_groups"
        path.mkdir(parents=True, exist_ok=True)
        self.organize_output_dir_var.set(str(path))
        return path

    def refresh_environment_status(self, force=False):
        self.docs_dir = self.detect_docs_dir()
        self.docs_dir_var.set(str(self.docs_dir) if self.docs_dir else "未找到")
        self.recovery_script_path = self.find_recovery_script()
        self.organizer_script_path = self.find_organizer_script()
        self.engine_path_var.set(str(self.recovery_script_path))
        self.organizer_path_var.set(str(self.organizer_script_path))
        self.runtime_var.set(sys.executable)
        self.get_organize_output_dir()

        try:
            self.load_engine(force=force)
            self.engine_error = ""
            self.engine_status_var.set("已就绪")
            self.simple_error_var.set("恢复引擎已就绪。按 1 -> 2 -> 3 的顺序操作即可。")
            self.log("恢复引擎已加载。")
        except Exception as exc:
            self.engine_module = None
            self.engine_error = str(exc)
            self.engine_status_var.set(f"加载失败：{exc}")
            self.simple_error_var.set(f"恢复引擎加载失败：{exc}")
            self.log(f"恢复引擎加载失败：{exc}")

        try:
            self.load_organizer(force=force)
            self.organizer_error = ""
            self.organizer_status_var.set("已就绪")
            self.log("整理引擎已加载。")
        except Exception as exc:
            self.organizer_module = None
            self.organizer_error = str(exc)
            self.organizer_status_var.set(f"加载失败：{exc}")
            self.log(f"整理引擎加载失败：{exc}")

    def log(self, message: str):
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_widget.configure(state="normal")
        self.log_widget.insert(tk.END, f"[{timestamp}] {message}\n")
        line_count = int(self.log_widget.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            trim_to = line_count - MAX_LOG_LINES
            self.log_widget.delete("1.0", f"{trim_to + 1}.0")
        self.log_widget.see(tk.END)
        self.log_widget.configure(state="disabled")

    def localize_log_message(self, message: str) -> str:
        if not message:
            return ""

        exact_map = {
            "No WXWork.exe process contains the target corp_id path in memory.": "没有发现内存中包含目标企业账号ID路径的 WXWork.exe 进程。",
            "Open the target account in WXWork and retry.": "请先打开企业微信目标账号后再重试。",
            "No page1 candidates found. Open a target chat and scroll more history, then retry.": "没有找到可用的 page1 候选页。请先打开目标聊天并继续上滑更多历史后再重试。",
            "Failed to resolve a usable PCache from the page1 candidates.": "未能从 page1 候选页中解析出可用的 PCache。",
            "message_table rootpage not found in sqlite_master.": "未在 sqlite_master 中找到 message_table 的 rootpage。",
            "Outputs:": "输出文件：",
            "Fast tips:": "快速提示：",
            "  1. Keep WXWork open.": "  1. 请保持企业微信处于打开状态。",
            "  2. Open the target chat and scroll more history before rerunning.": "  2. 重跑前请先打开目标聊天并上滑更多历史消息。",
            "  3. This method recovers cached pages only, so the result is partial by design.": "  3. 该方法只恢复当前缓存页，所以结果天然是部分恢复。",
        }
        if message in exact_map:
            return exact_map[message]

        prefix_map = [
            ("Selected PID: ", "已选择 PID："),
            ("Path hits: ", "路径命中数："),
            ("Sample hits: ", "示例命中地址："),
            ("Pointer size: ", "指针宽度："),
            ("Page1 candidates: ", "Page1 候选页："),
            ("Chosen page1: ", "已选 page1："),
            ("Chosen PgHdr: ", "已选 PgHdr："),
            ("Chosen pCache: ", "已选 pCache："),
            ("Cached pages: ", "缓存页数量："),
            ("message_table rootpage: ", "message_table rootpage："),
            ("Recovered rows: ", "恢复行数："),
            ("Time range: ", "时间范围："),
            ("json: ", "JSON："),
            ("sqlite: ", "SQLite："),
            ("csv: ", "CSV："),
            ("report: ", "报告："),
            ("  json: ", "  JSON："),
            ("  sqlite: ", "  SQLite："),
            ("  csv: ", "  CSV："),
            ("  report: ", "  报告："),
        ]
        for prefix, replacement in prefix_map:
            if message.startswith(prefix):
                return replacement + message[len(prefix) :]
        return message

    def set_busy(self, busy: bool, status_text: str):
        self.busy = busy
        self.status_var.set(status_text)
        if hasattr(self, "run_button"):
            self.run_button.configure(state="disabled" if busy else "normal")
        if hasattr(self, "simple_refresh_button"):
            state = "disabled" if busy else "normal"
            self.simple_refresh_button.configure(state=state)
            self.simple_scan_button.configure(state=state)
            self.simple_run_button.configure(state=state)
        if hasattr(self, "scan_conversations_button"):
            state = "disabled" if busy else "normal"
            self.scan_conversations_button.configure(state=state)
            self.refresh_preview_button.configure(state=state)
            self.organize_selected_button.configure(state=state)
            self.organize_all_button.configure(state=state)
        self.refresh_realtime_controls()

    def summarize_error(self, message: str) -> str:
        if not message:
            return ""
        lines = [line.strip() for line in str(message).splitlines() if line.strip()]
        if not lines:
            return ""
        return lines[-1]

    def select_corp_tree_item(self, corp_id: str):
        if not hasattr(self, "corp_tree"):
            return
        for item_id in self.corp_tree.get_children():
            values = self.corp_tree.item(item_id, "values")
            if values and values[0] == corp_id:
                self.corp_tree.selection_set(item_id)
                self.corp_tree.focus(item_id)
                self.corp_tree.see(item_id)
                return

    def refresh_realtime_controls(self):
        if not hasattr(self, "realtime_start_button"):
            return
        self.realtime_start_button.configure(state="disabled" if self.busy or self.realtime_enabled else "normal")
        self.realtime_stop_button.configure(state="normal" if self.realtime_enabled else "disabled")
        self.realtime_interval_entry.configure(state="disabled" if self.realtime_enabled else "normal")

    def validate_corp_id(self, show_warning: bool = True):
        corp_id = self.corp_id_var.get().strip()
        if not corp_id:
            if show_warning:
                messagebox.showwarning("缺少企业账号ID", self.corp_id_manual_help_text())
            return None
        if not CORP_ID_RE.match(corp_id):
            if show_warning:
                messagebox.showwarning("企业账号ID无效", "企业账号ID必须是纯数字。")
            return None
        return corp_id

    def parsed_pid(self, show_warning: bool = True):
        value = self.pid_var.get().strip()
        if not value:
            return 0
        if not value.isdigit():
            if show_warning:
                messagebox.showwarning("PID 无效", "PID 只能留空或填写数字。")
            return None
        return int(value)

    def get_realtime_interval_seconds(self, show_warning: bool = True):
        try:
            seconds = normalize_auto_recovery_interval(self.realtime_interval_var.get())
        except ValueError as exc:
            if show_warning:
                messagebox.showwarning("实时恢复间隔无效", str(exc))
            return None
        self.realtime_interval_var.set(str(seconds))
        return seconds

    def schedule_realtime_recovery(self, delay_ms: int | None = None):
        # 只保留下一轮未执行的定时任务，避免重复排队导致多轮自动恢复同时触发。
        if not self.realtime_enabled:
            return
        if self.realtime_after_id:
            try:
                self.root.after_cancel(self.realtime_after_id)
            except tk.TclError:
                pass
            self.realtime_after_id = None

        delay_ms = self.realtime_interval_seconds * 1000 if delay_ms is None else max(0, int(delay_ms))
        self.realtime_after_id = self.root.after(delay_ms, self.run_scheduled_realtime_recovery)

    def start_realtime_recovery(self):
        corp_id = self.validate_corp_id(show_warning=True)
        if corp_id is None:
            return
        interval_seconds = self.get_realtime_interval_seconds(show_warning=True)
        if interval_seconds is None:
            return

        if self.realtime_enabled:
            self.realtime_interval_seconds = interval_seconds
            self.realtime_status_var.set(f"实时恢复已在运行，新的间隔为 {interval_seconds} 秒。")
            self.schedule_realtime_recovery()
            self.log(f"已更新实时恢复间隔：企业账号ID {corp_id}，间隔 {interval_seconds} 秒。")
            return

        self.realtime_enabled = True
        self.realtime_interval_seconds = interval_seconds
        pid_text = self.pid_var.get().strip()
        scope_hint = "PID 留空，将尝试全部匹配进程" if not pid_text else f"已锁定 PID {pid_text}"
        self.realtime_status_var.set(f"已启动，每 {interval_seconds} 秒自动恢复一次；{scope_hint}。")
        self.simple_error_var.set("实时恢复已启动，会自动抓取当前账号缓存中的全部会话消息。")
        self.log(f"已开启实时恢复：企业账号ID {corp_id}，间隔 {interval_seconds} 秒，{scope_hint}。")
        self.schedule_realtime_recovery(delay_ms=200)
        self.refresh_realtime_controls()

    def stop_realtime_recovery(self, message: str, write_log: bool = True):
        had_schedule = self.realtime_enabled or bool(self.realtime_after_id)
        self.realtime_enabled = False
        self.realtime_cycle_started_at = None
        if self.realtime_after_id:
            try:
                self.root.after_cancel(self.realtime_after_id)
            except tk.TclError:
                pass
            self.realtime_after_id = None
        self.realtime_status_var.set(message)
        if write_log and had_schedule:
            self.log(message)
        self.refresh_realtime_controls()

    def stop_realtime_recovery_by_user(self):
        if self.busy:
            self.stop_realtime_recovery("已停止实时恢复；当前这一轮结束后不再自动继续。")
            return
        self.stop_realtime_recovery("已停止实时恢复。")

    def update_realtime_status_after_cycle(self, status: str, auto_mode: bool, success: bool):
        timestamp_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mode_text = "自动恢复" if auto_mode else "手动恢复"
        self.realtime_last_run_var.set(f"{timestamp_text} {mode_text}：{status}")
        if not self.realtime_enabled:
            self.realtime_cycle_started_at = None
            return

        result_text = "成功" if success else "失败"
        self.realtime_status_var.set(
            f"实时恢复运行中：上一轮{result_text}，将在 {self.realtime_interval_seconds} 秒后继续。"
        )
        delay_ms = None
        if auto_mode and self.realtime_cycle_started_at is not None:
            # 自动恢复按“上一轮启动时刻”计算节奏；如果单轮执行时间已经超过设定间隔，则结束后立即补跑下一轮。
            elapsed_seconds = max(0.0, time.monotonic() - self.realtime_cycle_started_at)
            remaining_seconds = max(0.0, float(self.realtime_interval_seconds) - elapsed_seconds)
            delay_ms = int(remaining_seconds * 1000)
            if delay_ms <= 0:
                self.realtime_status_var.set("实时恢复运行中：上一轮已结束，当前按设定节奏立即补跑下一轮。")
        self.realtime_cycle_started_at = None
        self.schedule_realtime_recovery(delay_ms=delay_ms)

    def run_scheduled_realtime_recovery(self):
        self.realtime_after_id = None
        if not self.realtime_enabled:
            return
        if self.busy:
            self.realtime_status_var.set("实时恢复等待当前任务完成，稍后自动重试。")
            self.schedule_realtime_recovery(delay_ms=AUTO_RECOVERY_BUSY_RETRY_DELAY_MS)
            return

        corp_id = self.validate_corp_id(show_warning=False)
        if corp_id is None:
            self.stop_realtime_recovery("实时恢复已停止：缺少有效的企业账号ID。")
            self.simple_error_var.set("实时恢复已停止：缺少有效的企业账号ID。")
            return

        pid = self.parsed_pid(show_warning=False)
        if pid is None:
            self.stop_realtime_recovery("实时恢复已停止：PID 只能留空或填写数字。")
            self.simple_error_var.set("实时恢复已停止：PID 只能留空或填写数字。")
            return

        self.realtime_status_var.set(f"正在自动恢复最新消息（每 {self.realtime_interval_seconds} 秒一次）...")
        started = self.start_recovery(auto_mode=True)
        if not started and self.realtime_enabled:
            self.realtime_status_var.set("实时恢复暂未启动成功，稍后自动重试。")
            self.schedule_realtime_recovery(delay_ms=AUTO_RECOVERY_BUSY_RETRY_DELAY_MS)

    def ensure_output_dir(self):
        path = Path(self.output_dir_var.get().strip() or self.default_output_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.output_dir_var.set(str(path))
        return path

    def refresh_corp_dirs(self):
        # 兼容当前精简布局：即使没有旧版账号列表表格，也要继续自动识别企业账号 ID。
        corp_tree = getattr(self, "corp_tree", None)
        if corp_tree:
            corp_tree.delete(*corp_tree.get_children())
        docs_dir = self.detect_docs_dir()
        if not docs_dir or not docs_dir.exists():
            self.selection_hint_var.set("没有找到企业微信文档目录；可点击“查找正在运行的企业微信”从内存路径反查账号ID。")
            self.simple_error_var.set("没有找到企业微信文档目录。账号ID通常是 Documents\\WXWork 下的纯数字文件夹名。")
            return

        dirs = []
        for item in docs_dir.iterdir():
            if not item.is_dir():
                continue
            if not CORP_ID_RE.match(item.name):
                continue
            db_path = item / "Data" / "message.db"
            modified_ts = item.stat().st_mtime
            modified = datetime.fromtimestamp(modified_ts).strftime("%Y-%m-%d %H:%M:%S")
            dirs.append(
                {
                    "corp_id": item.name,
                    "has_db": db_path.exists(),
                    "modified": modified,
                    "modified_ts": modified_ts,
                }
            )

        if corp_tree:
            for item in sorted(dirs, key=lambda row: row["corp_id"]):
                corp_tree.insert(
                    "",
                    tk.END,
                    values=(item["corp_id"], "是" if item["has_db"] else "否", item["modified"]),
                )

        current_corp_id = self.corp_id_var.get().strip()
        available_ids = {item["corp_id"] for item in dirs}
        auto_selected = False
        if current_corp_id not in available_ids and dirs:
            preferred = max(dirs, key=lambda item: (1 if item["has_db"] else 0, item["modified_ts"]))
            current_corp_id = preferred["corp_id"]
            self.corp_id_var.set(current_corp_id)
            auto_selected = True

        if current_corp_id:
            self.select_corp_tree_item(current_corp_id)

        if auto_selected:
            self.selection_hint_var.set(f"已在 {docs_dir} 下检测到 {len(dirs)} 个企业账号目录，已自动选中 {current_corp_id}。")
            self.simple_error_var.set(f"已自动识别企业账号ID：{current_corp_id}")
        else:
            self.selection_hint_var.set(f"已在 {docs_dir} 下检测到 {len(dirs)} 个企业账号目录。")
            if dirs:
                self.simple_error_var.set("账号列表已刷新，可以继续点“扫描匹配进程”。")
            else:
                self.simple_error_var.set("没有检测到企业账号目录。请先打开企业微信并登录目标账号。")

    def on_corp_selected(self, _event=None):
        if not hasattr(self, "corp_tree"):
            return
        item_id = self.corp_tree.selection()
        if not item_id:
            return
        values = self.corp_tree.item(item_id[0], "values")
        if values:
            self.corp_id_var.set(values[0])

    def on_corp_double_clicked(self, _event=None):
        self.on_corp_selected()
        self.start_process_scan()

    def start_corp_id_discovery_from_processes(self):
        if self.busy:
            messagebox.showinfo("任务进行中", "请先等待当前任务完成。")
            return False

        self.set_busy(True, "正在从运行中的企业微信反查账号ID...")
        self.selection_hint_var.set("正在扫描 WXWork.exe 内存里的 message.db 路径，尝试自动识别企业账号ID。")
        self.simple_error_var.set("正在从正在运行的企业微信里查找企业账号ID...")
        self.log("企业账号ID为空，正在从运行中的 WXWork.exe 内存路径反查账号ID...")
        self.worker_thread = threading.Thread(target=self.corp_id_discovery_worker, daemon=True)
        self.worker_thread.start()
        return True

    def corp_id_discovery_worker(self):
        try:
            engine = self.load_engine()
            discover = getattr(engine, "discover_running_wxwork_corp_ids", None)
            if discover is None:
                raise RuntimeError("当前恢复引擎不支持从运行进程反查企业账号ID。")
            candidates = discover()
            self.ui_queue.put(("corp_discovery_result", candidates))
        except Exception as exc:
            details = "".join(traceback.format_exception(exc))
            self.ui_queue.put(("task_failed", {"status": "账号ID反查失败", "log": details}))

    def populate_process_tree(self, hits):
        self.process_hits = sorted(hits, key=lambda item: item.get("count", 0), reverse=True)
        # 多候选 PID 时不要自动锁定第一个进程，PID 留空才能让恢复引擎逐个尝试全部匹配进程。
        process_tree = getattr(self, "process_tree", None)
        if process_tree:
            process_tree.delete(*process_tree.get_children())
        first_item_id = None
        for item in self.process_hits:
            sample = hex(item["sample_hits"][0]) if item.get("sample_hits") else ""
            if process_tree:
                item_id = process_tree.insert("", tk.END, values=(item["pid"], "是" if item.get("wow64") else "否", item["count"], sample))
                if first_item_id is None:
                    first_item_id = item_id

        if first_item_id and self.process_hits:
            top_pid = str(self.process_hits[0]["pid"])
            process_tree.selection_set(first_item_id)
            process_tree.focus(first_item_id)
            process_tree.see(first_item_id)
            if len(self.process_hits) == 1:
                self.pid_var.set(top_pid)
                self.simple_error_var.set(f"已自动选中唯一匹配进程 PID：{top_pid}")
                self.selection_hint_var.set(f"扫描到唯一候选 PID：{top_pid}，现在可以直接恢复。")
            else:
                self.pid_var.set("")
                self.simple_error_var.set(f"已扫描到 {len(self.process_hits)} 个候选进程，PID 已留空，将自动尝试全部匹配进程。")
                self.selection_hint_var.set(f"已扫描到 {len(self.process_hits)} 个候选进程；PID 留空表示恢复全部匹配进程，手动填写 PID 才会只抓一个进程。")
        elif self.process_hits:
            top_pid = str(self.process_hits[0]["pid"])
            if len(self.process_hits) == 1:
                self.pid_var.set(top_pid)
                self.simple_error_var.set(f"已自动选中唯一匹配进程 PID：{top_pid}")
                self.selection_hint_var.set(f"扫描到唯一候选 PID：{top_pid}，现在可以直接恢复。")
            else:
                self.pid_var.set("")
                self.simple_error_var.set(f"已扫描到 {len(self.process_hits)} 个候选进程，PID 已留空，将自动尝试全部匹配进程。")
                self.selection_hint_var.set(f"已扫描到 {len(self.process_hits)} 个候选进程；PID 留空表示恢复全部匹配进程，手动填写 PID 才会只抓一个进程。")
        else:
            self.pid_var.set("")
            self.simple_error_var.set("没有扫描到匹配进程。请确认目标账号聊天窗口已经打开。")
            self.selection_hint_var.set("没有扫描到匹配进程，请先打开目标账号的聊天窗口，再重新扫描。")

    def apply_selected_pid(self):
        if not hasattr(self, "process_tree"):
            messagebox.showinfo("未显示进程列表", "当前界面未显示候选进程表，请直接修改上方 PID 输入框。")
            return
        selection = self.process_tree.selection()
        if not selection:
            messagebox.showinfo("未选择进程", "请先选择一行候选进程。")
            return
        values = self.process_tree.item(selection[0], "values")
        if values:
            self.pid_var.set(str(values[0]))
            self.simple_error_var.set(f"已手动锁定 PID：{values[0]}")
            self.log(f"已锁定 PID：{values[0]}")

    def start_process_scan(self):
        corp_id = self.corp_id_var.get().strip()
        if not corp_id:
            self.refresh_corp_dirs()
            corp_id = self.corp_id_var.get().strip()
        if not corp_id:
            self.start_corp_id_discovery_from_processes()
            return
        if not CORP_ID_RE.match(corp_id):
            self.validate_corp_id(show_warning=True)
            return
        if self.busy:
            messagebox.showinfo("任务进行中", "请先等待当前任务完成。")
            return

        self.set_busy(True, "正在扫描候选进程...")
        self.log(f"正在为企业账号ID {corp_id} 扫描匹配的 WXWork.exe 进程...")
        self.worker_thread = threading.Thread(target=self.process_scan_worker, args=(corp_id,), daemon=True)
        self.worker_thread.start()

    def process_scan_worker(self, corp_id: str):
        try:
            engine = self.load_engine()
            hits = engine.find_processes_with_target_path(corp_id)
            self.ui_queue.put(("process_hits", hits))
            self.ui_queue.put(("task_done", {"status": "就绪", "log": f"已找到 {len(hits)} 个匹配的候选进程。"}))
        except Exception as exc:
            details = "".join(traceback.format_exception(exc))
            self.ui_queue.put(("task_failed", {"status": "进程扫描失败", "log": details}))

    def start_recovery(self, auto_mode: bool = False):
        corp_id = self.validate_corp_id(show_warning=not auto_mode)
        if corp_id is None:
            return False
        pid = self.parsed_pid(show_warning=not auto_mode)
        if pid is None:
            if auto_mode:
                self.stop_realtime_recovery("实时恢复已停止：PID 只能留空或填写数字。")
            return False
        if self.busy:
            if not auto_mode:
                messagebox.showinfo("任务进行中", "请先等待当前任务完成。")
            return False

        output_dir = self.ensure_output_dir()
        self.set_busy(True, "正在自动恢复最新消息..." if auto_mode else "正在执行恢复...")
        if auto_mode:
            # 记录自动恢复这一轮真正开始的时刻，供完成后计算下一轮是否需要立即补跑。
            self.realtime_cycle_started_at = time.monotonic()
        self.log(f"{'开始自动恢复' if auto_mode else '开始恢复'}，企业账号ID：{corp_id} -> {output_dir}")
        if pid:
            self.log(f"使用锁定的 PID：{pid}")
        else:
            self.log("PID 留空：本轮会尝试全部匹配的 WXWork.exe 进程；恢复范围是该账号当前缓存中的全部会话，不限定单个群。")
        self.worker_thread = threading.Thread(target=self.recovery_worker, args=(corp_id, output_dir, pid, auto_mode), daemon=True)
        self.worker_thread.start()
        return True

    def recovery_worker(self, corp_id: str, output_dir: Path, pid: int, auto_mode: bool = False):
        try:
            engine = self.load_engine()

            def logger(message):
                self.ui_queue.put(("log", str(message)))

            result = engine.run_recovery(corp_id, output_dir, pid=pid, logger=logger)
            self.ui_queue.put(("recovery_result", result))
            if result.get("ok"):
                try:
                    organizer = self.load_organizer()
                    conversation_data = organizer.list_conversations(source_dir=output_dir, prefix="")
                    self.ui_queue.put(("conversation_list", conversation_data))
                except Exception as refresh_exc:
                    refresh_details = "".join(traceback.format_exception(refresh_exc))
                    self.ui_queue.put(("log", f"恢复后自动刷新会话列表失败：\n{refresh_details}"))
            final_status = "恢复完成" if result.get("ok") else f"恢复结束，退出码 {result.get('exit_code')}"
            self.ui_queue.put(
                (
                    "task_done",
                    {
                        "status": final_status,
                        "log": f"企业账号ID {corp_id} 的{'自动恢复' if auto_mode else '恢复'}任务已结束。",
                        "task_type": "recovery",
                        "auto_mode": auto_mode,
                    },
                )
            )
        except Exception as exc:
            details = "".join(traceback.format_exception(exc))
            self.ui_queue.put(("task_failed", {"status": "恢复失败", "log": details, "task_type": "recovery", "auto_mode": auto_mode}))

    def populate_conversation_tree(self, conversations):
        previous_id = self.selected_conversation_id()
        self.conversation_tree.delete(*self.conversation_tree.get_children())
        self.conversation_rows = conversations
        source_rows = self.all_conversation_rows if self.all_conversation_rows else conversations
        self.conversation_index_by_id = {item["conversation_id"]: item for item in source_rows}
        first_item_id = None
        selected_item_id = None
        for item in conversations:
            item_id = self.conversation_tree.insert(
                "",
                tk.END,
                values=(
                    item["conversation_id"],
                    item.get("counterpart_id", ""),
                    item["sender_count"],
                    item.get("conversation_display_name", ""),
                    item["last_time_iso"],
                    item["message_count"],
                    item.get("conversation_kind_label", item.get("conversation_kind", "")),
                ),
            )
            if first_item_id is None:
                first_item_id = item_id
            if item["conversation_id"] == previous_id:
                selected_item_id = item_id

        target_item_id = selected_item_id or first_item_id
        if target_item_id:
            self.conversation_tree.selection_set(target_item_id)
            self.conversation_tree.focus(target_item_id)
            self.conversation_tree.see(target_item_id)
            self.on_conversation_selected()
        else:
            self.clear_conversation_preview("当前筛选结果为空，请切换筛选条件或先刷新会话列表。")

    def clear_preview_inline_assets(self):
        for widget in self.preview_inline_widgets:
            try:
                widget.destroy()
            except Exception:
                pass
        self.preview_inline_widgets.clear()
        self.preview_inline_images.clear()

    def set_preview_text_message(self, message: str):
        self.clear_preview_inline_assets()
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", tk.END)
        self.preview_text.insert(tk.END, f"{message.rstrip()}\n", "body")
        self.preview_text.configure(state="disabled")
        self.preview_text.see("1.0")
        self.select_chat_tab()

    def clear_conversation_preview(self, message: str):
        self.preview_title_var.set("未选择会话")
        self.preview_subtitle_var.set("先在左侧选择一个会话，再查看完整聊天内容。")
        self.preview_kind_var.set("-")
        self.preview_id_var.set("-")
        self.preview_counterpart_var.set("-")
        self.preview_stats_var.set("-")
        self.preview_range_var.set("-")
        self.preview_evidence_var.set("-")
        self.preview_participants_var.set("-")
        self.preview_status_var.set(message)
        self.set_preview_summary_details_visible(False)
        self.set_preview_text_message(message)

    def current_selected_conversation_row(self):
        conversation_id = self.selected_conversation_id()
        if not conversation_id:
            return None
        return self.conversation_index_by_id.get(conversation_id)

    def insert_preview_image(self, image_path: str):
        image_file = Path(image_path)
        if not image_file.exists():
            return False

        try:
            with Image.open(image_file) as raw_image:
                preview_image = ImageOps.exif_transpose(raw_image).copy()
            preview_image.thumbnail((PREVIEW_IMAGE_MAX_WIDTH, PREVIEW_IMAGE_MAX_HEIGHT), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(preview_image)
        except Exception as exc:
            self.preview_text.insert(tk.END, f"图片加载失败：{image_file.name}（{exc}）\n", "meta")
            return False

        # Text 组件需要保留图片对象引用，否则 Tk 会把已插入的缩略图回收掉。
        self.preview_inline_images.append(photo)
        self.preview_text.insert(tk.END, f"图片文件：{image_file.name}\n", "meta")
        self.preview_text.image_create(tk.END, image=photo, padx=0, pady=4)
        self.preview_text.insert(tk.END, "\n", "body")
        return True

    def preview_media_render_kind(self, message, media_file: str):
        path = Path(media_file)
        suffix = path.suffix.lower()
        if suffix in PREVIEW_IMAGE_EXTENSIONS:
            return "image"
        if suffix in PREVIEW_VIDEO_EXTENSIONS:
            return "video"
        if suffix in PREVIEW_AUDIO_EXTENSIONS:
            return "audio"
        if message.get("media_kind") in {"图片", "动画表情"}:
            return "image"
        if message.get("media_kind") == "视频":
            return "video"
        if message.get("media_kind") == "语音/音频":
            return "audio"
        return "file"

    def append_open_media_button(self, path: Path, label: str):
        # Text 组件里嵌入按钮时也要保留引用，避免按钮对象被回收后无法点击。
        button = ttk.Button(self.preview_text, text=label, style="CompactTool.TButton", command=lambda target=path: open_path(target))
        self.preview_inline_widgets.append(button)
        self.preview_text.window_create(tk.END, window=button, padx=0, pady=2)
        self.preview_text.insert(tk.END, "\n", "body")

    def append_media_preview(self, message):
        media_files = message.get("media_files") or []
        if not media_files:
            missing_messages = {
                "图片": "未在本机缓存或历史导出目录中找到可直接显示的图片原文件。\n",
                "动画表情": "未在本机缓存或历史导出目录中找到可直接显示的表情原文件。\n",
                "视频": "未在本机缓存或历史导出目录中找到可直接打开的视频文件。\n",
                "语音/音频": "未在本机缓存或历史导出目录中找到可直接打开的音频文件。\n",
            }
            if message.get("media_kind") in missing_messages:
                self.preview_text.insert(tk.END, missing_messages[message.get("media_kind")], "meta")
            missing_count = message.get("missing_media_file_count", 0)
            if missing_count:
                self.preview_text.insert(tk.END, f"该消息还有 {missing_count} 个媒体线索未找到本地原文件。\n", "meta")
            return

        shown_count = 0
        for media_file in media_files[:3]:
            render_kind = self.preview_media_render_kind(message, media_file)
            media_path = Path(media_file)
            if render_kind == "image":
                if self.insert_preview_image(media_file):
                    shown_count += 1
                continue
            if render_kind == "video":
                self.preview_text.insert(tk.END, f"视频文件：{media_path.name}\n", "meta")
                self.append_open_media_button(media_path, "打开本地视频")
                shown_count += 1
                continue
            if render_kind == "audio":
                self.preview_text.insert(tk.END, f"音频文件：{media_path.name}\n", "meta")
                self.append_open_media_button(media_path, "打开本地音频")
                shown_count += 1
                continue
            self.preview_text.insert(tk.END, f"本地媒体文件：{media_path.name}\n", "meta")
            self.append_open_media_button(media_path, "打开本地文件")
            shown_count += 1

        if shown_count == 0 and message.get("media_kind") in {"图片", "动画表情"}:
            self.preview_text.insert(tk.END, "已识别为图片或表情消息，但当前媒体文件无法显示。\n", "meta")
        missing_count = message.get("missing_media_file_count", 0)
        if missing_count:
            self.preview_text.insert(tk.END, f"另有 {missing_count} 个媒体线索未找到本地原文件。\n", "meta")
        if len(media_files) > 3:
            self.preview_text.insert(tk.END, f"其余 {len(media_files) - 3} 个媒体文件未在当前窗口继续展开。\n", "meta")

    def update_preview_card_from_conversation(self, item, subtitle="正在加载该会话的完整聊天内容..."):
        if not item:
            self.clear_conversation_preview("先在左侧选择一个会话，再查看完整聊天内容。")
            return
        self.set_preview_summary_details_visible(True)
        self.preview_title_var.set(item.get("conversation_display_name", "") or item.get("conversation_id", "未命名会话"))
        self.preview_subtitle_var.set(subtitle)
        self.preview_kind_var.set(item.get("conversation_kind_label", item.get("conversation_kind", "")) or "-")
        self.preview_id_var.set(item.get("conversation_id", "") or "-")
        self.preview_counterpart_var.set(item.get("counterpart_id", "") or "无")
        self.preview_stats_var.set(f"{item.get('message_count', 0)} 条消息 / {item.get('sender_count', 0)} 位发送人")
        self.preview_range_var.set(
            f"{item.get('first_time_iso', '') or '未知'} -> {item.get('last_time_iso', '') or '未知'}"
        )
        self.preview_evidence_var.set(item.get("conversation_name_evidence", "") or item.get("conversation_kind_evidence", "") or "暂无")
        self.preview_participants_var.set("正在统计发送人...")
        self.preview_status_var.set(subtitle)

    def render_conversation_preview(self, payload):
        conversation = payload.get("conversation", {})
        messages = payload.get("messages", [])
        self.set_preview_summary_details_visible(True)
        displayed = conversation.get("displayed_message_count", len(messages))
        total = conversation.get("message_count", len(messages))
        marker_count = conversation.get("recovery_gap_marker_count", 0)
        display_total = conversation.get("display_total_message_count", total + marker_count)
        omitted = conversation.get("omitted_message_count", max(0, display_total - displayed))
        marker_note = f"，含 {marker_count} 条疑似缺失提示" if marker_count else ""
        subtitle = f"{conversation.get('conversation_kind_label', '会话')}，已显示全部 {displayed} 条记录{marker_note}。"
        if omitted > 0:
            subtitle = f"{conversation.get('conversation_kind_label', '会话')}，当前显示 {displayed}/{display_total} 条记录{marker_note}。省略更早的 {omitted} 条。"

        self.preview_title_var.set(conversation.get("conversation_display_name", "") or conversation.get("conversation_id", "未命名会话"))
        self.preview_subtitle_var.set(subtitle)
        self.preview_kind_var.set(conversation.get("conversation_kind_label", "") or "-")
        self.preview_id_var.set(conversation.get("conversation_id", "") or "-")
        self.preview_counterpart_var.set(conversation.get("counterpart_id", "") or "无")
        stats_text = f"{conversation.get('message_count', 0)} 条消息 / {conversation.get('sender_count', 0)} 位发送人"
        if marker_count:
            stats_text = f"{stats_text} / 疑似缺口 {marker_count} 处"
        self.preview_stats_var.set(stats_text)
        self.preview_range_var.set(
            f"{conversation.get('first_time_iso', '') or '未知'} -> {conversation.get('last_time_iso', '') or '未知'}"
        )
        self.preview_evidence_var.set(
            conversation.get("conversation_name_evidence", "")
            or conversation.get("conversation_kind_evidence", "")
            or "暂无"
        )

        top_senders = conversation.get("top_senders", [])
        if top_senders:
            participant_text = "；".join(
                f"{item.get('sender_name') or item.get('sender_id') or '未知'} x{item.get('message_count', 0)}"
                for item in top_senders[:3]
            )
        else:
            participant_text = "暂无发送人统计"
        self.preview_participants_var.set(participant_text)

        self.clear_preview_inline_assets()
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", tk.END)
        if omitted > 0:
            self.preview_text.insert(
                tk.END,
                f"仅显示最近 {displayed} 条消息；更早的 {omitted} 条未在此窗口展开。\n\n",
                "meta",
            )
        if not messages:
            self.preview_text.insert(tk.END, "该会话暂时没有可展示的聊天内容。\n", "body")
        else:
            for message in messages:
                meta_parts = [f"[{message.get('seq', '')}]", message.get("send_time_iso", "") or "未知时间"]
                if message.get("flag") not in {"", None}:
                    meta_parts.append(f"flag={message.get('flag')}")
                if message.get("is_quote_reply"):
                    meta_parts.append("引用回复")
                if message.get("has_media") and message.get("media_kind"):
                    meta_parts.append(message.get("media_kind"))
                self.preview_text.insert(tk.END, " | ".join(part for part in meta_parts if part) + "\n", "meta")
                self.preview_text.insert(tk.END, f"{message.get('sender_display', '未知发送人')}\n", "sender")
                content_type_label = message.get("content_type_label", "")
                if message.get("is_quote_reply") and content_type_label:
                    content_type_label = f"引用回复 / {content_type_label}"
                self.preview_text.insert(tk.END, f"{content_type_label}\n", "type")
                if message.get("is_quote_reply"):
                    quote_sender = message.get("quote_sender", "")
                    quote_text = message.get("quote_text", "")
                    reply_text = message.get("quote_reply_text", "") or message.get("preview", "(空)")
                    if quote_sender or quote_text:
                        quote_prefix = f"引用 {quote_sender}" if quote_sender else "引用"
                        self.preview_text.insert(tk.END, f"{quote_prefix}：{quote_text or '未解析到引用内容'}\n", "quote")
                    else:
                        self.preview_text.insert(tk.END, "引用：已识别为引用回复，但当前恢复字段里没有完整引用正文。\n", "quote")
                    self.preview_text.insert(tk.END, f"回复：{reply_text or '(空)'}\n", "body")
                else:
                    self.preview_text.insert(tk.END, f"{message.get('preview', '(空)')}\n", "body")
                self.append_media_preview(message)
                self.preview_text.insert(tk.END, "-" * 72 + "\n", "separator")
        self.preview_text.configure(state="disabled")
        self.preview_text.see("1.0")
        self.preview_status_var.set(subtitle)
        self.select_chat_tab()

    def request_conversation_preview(self, conversation_id: str, force: bool = False):
        if not conversation_id:
            self.clear_conversation_preview("先在左侧选择一个会话，再查看完整聊天内容。")
            return

        sqlite_path = self.latest_sqlite_var.get().strip()
        cache_key = (sqlite_path, conversation_id)
        if not force and cache_key in self.conversation_preview_cache:
            self.render_conversation_preview(self.conversation_preview_cache[cache_key])
            return

        current_row = self.conversation_index_by_id.get(conversation_id)
        self.update_preview_card_from_conversation(current_row)
        self.set_preview_text_message("正在读取该会话的完整聊天内容、发送人、时间和本地媒体文件，请稍候...")

        self.preview_request_token += 1
        request_token = self.preview_request_token
        self.preview_requested_conversation_id = conversation_id
        source_dir = Path(self.output_dir_var.get().strip() or self.default_output_dir)
        input_sqlite = sqlite_path if sqlite_path and sqlite_path != "未扫描" else ""
        self.preview_thread = threading.Thread(
            target=self.conversation_preview_worker,
            args=(conversation_id, input_sqlite, source_dir, self.conversation_self_user_id, request_token),
            daemon=True,
        )
        self.preview_thread.start()

    def refresh_selected_conversation_preview(self, force: bool = False):
        conversation_id = self.selected_conversation_id()
        if not conversation_id:
            messagebox.showinfo("未选择会话", "请先在会话列表中选择一个会话。")
            return
        self.request_conversation_preview(conversation_id, force=force)

    def conversation_preview_worker(
        self,
        conversation_id: str,
        input_sqlite: str,
        source_dir: Path,
        self_user_id: str,
        request_token: int,
    ):
        try:
            organizer = self.load_organizer()
            payload = organizer.get_conversation_preview(
                conversation_id=conversation_id,
                input_sqlite=input_sqlite,
                source_dir=source_dir,
                self_user_id=self_user_id,
                limit=PREVIEW_MESSAGE_LIMIT,
            )
            self.ui_queue.put(("conversation_preview", {"token": request_token, "conversation_id": conversation_id, "payload": payload}))
        except Exception as exc:
            details = "".join(traceback.format_exception(exc))
            self.ui_queue.put(
                (
                    "conversation_preview_failed",
                    {"token": request_token, "conversation_id": conversation_id, "log": details},
                )
            )

    def on_conversation_selected(self, _event=None):
        item = self.current_selected_conversation_row()
        if not item:
            self.clear_conversation_preview("先在左侧选择一个会话，再查看完整聊天内容。")
            return
        self.update_preview_card_from_conversation(item)
        self.request_conversation_preview(item["conversation_id"])

    def current_conversation_filter_kind(self):
        label = self.conversation_filter_var.get().strip()
        for option_label, kind in CONVERSATION_FILTERS:
            if option_label == label:
                return kind
        return ""

    def supported_conversation_rows(self, conversations):
        # 按当前产品范围收口列表，只保留外部群和单聊，避免把助手/F/M 等会话继续带到界面里。
        return [item for item in conversations if item.get("conversation_kind") in SUPPORTED_CONVERSATION_KINDS]

    def conversation_display_heading_text(self):
        if self.current_conversation_filter_kind() == "single_chat":
            return "客户 / 联系人"
        return "群名称"

    def update_conversation_tree_heading(self):
        if hasattr(self, "conversation_tree"):
            self.conversation_tree.heading("conversation_display_name", text=self.conversation_display_heading_text())

    def filtered_conversation_rows(self):
        kind = self.current_conversation_filter_kind()
        if not kind:
            return list(self.all_conversation_rows)
        return [item for item in self.all_conversation_rows if item.get("conversation_kind") == kind]

    def apply_conversation_filter(self):
        self.update_conversation_tree_heading()
        filtered = self.filtered_conversation_rows()
        self.populate_conversation_tree(filtered)
        current_label = self.conversation_filter_var.get().strip() or "当前筛选"
        if self.all_conversation_rows:
            self.conversation_hint_var.set(
                f"已扫描到 {len(self.all_conversation_rows)} 个可用会话，当前显示 {len(filtered)} 个。筛选：{current_label}。"
            )
        elif self.conversation_metadata.get("missing_recovery_sqlite"):
            self.conversation_hint_var.set("当前恢复目录还没有恢复结果。请先在右侧点击“立即提取一次聊天数据”。")
        else:
            self.conversation_hint_var.set("先刷新会话列表。这里只保留外部群和单聊。")

    def on_conversation_filter_changed(self, _event=None):
        self.apply_conversation_filter()

    def selected_conversation_id(self):
        selection = self.conversation_tree.selection()
        if not selection:
            return ""
        values = self.conversation_tree.item(selection[0], "values")
        return values[0] if values else ""

    def start_conversation_scan(self):
        if self.busy:
            messagebox.showinfo("任务进行中", "请先等待当前任务完成。")
            return
        self.get_organize_output_dir()
        self.set_busy(True, "正在扫描会话...")
        self.log("正在读取最新恢复 sqlite，并扫描全部会话列表...")
        self.worker_thread = threading.Thread(target=self.conversation_scan_worker, daemon=True)
        self.worker_thread.start()

    def conversation_scan_worker(self):
        source_dir = Path(self.output_dir_var.get().strip() or self.default_output_dir)
        try:
            organizer = self.load_organizer()
            data = organizer.list_conversations(
                source_dir=source_dir,
                prefix="",
            )
            self.ui_queue.put(("conversation_list", data))
            self.ui_queue.put(("task_done", {"status": "就绪", "log": f"已扫描到 {len(data['conversations'])} 个会话。"}))
        except FileNotFoundError:
            message = missing_recovery_sqlite_message(source_dir)
            self.ui_queue.put(
                (
                    "conversation_list",
                    {
                        "sqlite_path": "未找到恢复结果",
                        "conversations": [],
                        "metadata": {
                            "missing_recovery_sqlite": True,
                            "source_dir": str(source_dir),
                        },
                        "self_user_id": "",
                    },
                )
            )
            self.ui_queue.put(("task_done", {"status": "等待恢复", "log": message}))
        except Exception as exc:
            details = "".join(traceback.format_exception(exc))
            self.ui_queue.put(("task_failed", {"status": "会话扫描失败", "log": details}))

    def start_organize_selected(self):
        conversation_id = self.selected_conversation_id()
        if not conversation_id:
            messagebox.showinfo("未选择会话", "请先在会话列表中选择一个会话。")
            return
        self.auto_open_organized_timeline = True
        self.start_organize(conversation_id=conversation_id)

    def start_organize_filtered(self):
        filtered = self.filtered_conversation_rows()
        if not filtered:
            messagebox.showinfo("没有可整理的会话", "当前筛选结果为空，请先刷新会话列表或切换筛选条件。")
            return
        self.auto_open_organized_timeline = False
        label = self.conversation_filter_var.get().strip() or "全部会话"
        self.start_organize(conversation_ids=[item["conversation_id"] for item in filtered], label=f"当前筛选结果：{label}")

    def start_organize(self, conversation_id: str = "", conversation_ids=None, label: str = ""):
        if self.busy:
            messagebox.showinfo("任务进行中", "请先等待当前任务完成。")
            return
        organize_output_dir = self.get_organize_output_dir()
        source_dir = Path(self.output_dir_var.get().strip() or self.default_output_dir)
        label = label or conversation_id or "当前筛选结果"
        self.set_busy(True, "正在整理聊天记录...")
        self.log(f"开始整理：{label}")
        self.log(f"来源目录：{source_dir}")
        self.log(f"整理输出目录：{organize_output_dir}")
        self.worker_thread = threading.Thread(
            target=self.organize_worker,
            args=(source_dir, organize_output_dir, conversation_id, conversation_ids or [], label),
            daemon=True,
        )
        self.worker_thread.start()

    def organize_worker(self, source_dir: Path, organize_output_dir: Path, conversation_id: str, conversation_ids, label: str):
        try:
            organizer = self.load_organizer()

            def logger(message):
                self.ui_queue.put(("log", str(message)))

            result = organizer.run_organize(
                source_dir=source_dir,
                output_dir=organize_output_dir,
                conversation_id=conversation_id,
                conversation_ids=conversation_ids,
                prefix="",
                logger=logger,
            )
            self.ui_queue.put(("organize_result", result))
            self.ui_queue.put(("task_done", {"status": "整理完成", "log": f"聊天整理已完成：{label}。"}))
        except Exception as exc:
            details = "".join(traceback.format_exception(exc))
            self.ui_queue.put(("task_failed", {"status": "聊天整理失败", "log": details}))

    def result_candidates(self):
        output_dir = Path(self.output_dir_var.get().strip() or self.default_output_dir)
        if not output_dir.exists():
            return []

        corp_id = self.corp_id_var.get().strip()
        files = []
        for item in output_dir.iterdir():
            if not item.is_file():
                continue
            name = item.name
            if name.startswith("wxwork_") and "_partial_" in name:
                if corp_id and not name.startswith(f"wxwork_{corp_id}_partial_"):
                    continue
                files.append(item)
                continue
            if not corp_id and name.startswith("message_table_partial_"):
                files.append(item)

        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files

    def refresh_results(self):
        output_dir = Path(self.output_dir_var.get().strip() or self.default_output_dir)
        if hasattr(self, "results_tree"):
            self.results_tree.delete(*self.results_tree.get_children())
        self.result_files = []

        if not output_dir.exists():
            self.latest_sqlite_var.set("未扫描")
            return

        candidates = self.result_candidates()
        latest_sqlite = next((item for item in candidates if item.suffix.lower() == ".sqlite"), None)
        self.latest_sqlite_var.set(str(latest_sqlite) if latest_sqlite else "未扫描")

        for item in candidates:
            modified = datetime.fromtimestamp(item.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            if hasattr(self, "results_tree"):
                self.results_tree.insert("", tk.END, values=(item.name, human_size(item.stat().st_size), modified))
            self.result_files.append(item)

    def selected_result_path(self):
        if not hasattr(self, "results_tree"):
            return None
        selection = self.results_tree.selection()
        if not selection:
            return None
        values = self.results_tree.item(selection[0], "values")
        if not values:
            return None
        return Path(self.output_dir_var.get().strip() or self.default_output_dir) / values[0]

    def open_selected_result(self):
        path = self.selected_result_path()
        if path is None:
            messagebox.showinfo("未选择结果文件", "请先选择一个结果文件。")
            return
        open_path(path)

    def open_latest_file(self, suffix: str):
        for item in self.result_candidates():
            if item.name.endswith(suffix):
                open_path(item)
                return
        messagebox.showinfo("未找到文件", f"输出目录中没有找到以 {suffix} 结尾的文件。")

    def open_organized_dir(self):
        open_path(self.get_organize_output_dir())

    def open_organized_index(self):
        candidates = sorted(
            [
                item
                for item in self.get_organize_output_dir().glob("*.csv")
                if item.is_file() and "索引" in item.name and "总表" not in item.name
            ],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            candidates = sorted(
                [
                    item
                    for item in self.get_organize_output_dir().glob("*_index*.csv")
                    if item.is_file() and not item.name.startswith("all_recovered_conversations")
                ],
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        if candidates:
            open_path(candidates[0])
            return
        messagebox.showinfo("未找到文件", "还没有导出索引文件，请先执行一次聊天整理。")

    def open_all_conversations_index(self):
        candidates = sorted(
            [item for item in self.get_organize_output_dir().glob("*.csv") if item.is_file() and "总表" in item.name],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            candidates = sorted(self.get_organize_output_dir().glob("all_recovered_conversations*.csv"), key=lambda item: item.stat().st_mtime, reverse=True)
        if candidates:
            open_path(candidates[0])
            return
        messagebox.showinfo("未找到文件", "还没有全部会话总表，请先执行一次聊天整理。")

    def open_latest_organized_timeline(self):
        candidates = sorted(
            [
                item
                for item in self.get_organize_output_dir().rglob("*.csv")
                if item.is_file() and ("时间线" in item.name or item.name.startswith("timeline"))
            ],
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            open_path(candidates[0])
            return
        messagebox.showinfo("未找到文件", "还没有整理出的聊天时间线，请先执行一次聊天整理。")

    def open_named_file(self, name: str):
        candidates = [
            self.base_dir / name,
            self.base_dir / "文档" / "使用说明" / name,
            self.base_dir / "文档" / "交接资料" / name,
            self.base_dir / "文档" / "设计资料" / name,
            get_resource_dir() / name,
        ]
        for path in candidates:
            if path.exists():
                open_path(path)
                return
        messagebox.showinfo("未找到文件", f"未找到文件：\n{name}")

    def choose_output_dir(self):
        selected = filedialog.askdirectory(initialdir=self.output_dir_var.get().strip() or str(self.default_output_dir))
        if selected:
            self.output_dir_var.set(selected)
            self.get_organize_output_dir()
            self.latest_sqlite_var.set("未扫描")
            self.all_conversation_rows = []
            self.conversation_rows = []
            self.conversation_index_by_id = {}
            self.conversation_preview_cache = {}
            if hasattr(self, "conversation_tree"):
                self.populate_conversation_tree([])
            self.refresh_results()

    def open_output_dir(self):
        path = self.ensure_output_dir()
        open_path(path)

    def open_docs_dir(self):
        docs_dir = self.detect_docs_dir()
        if docs_dir and docs_dir.exists():
            open_path(docs_dir)
            return
        messagebox.showinfo("未找到目录", "当前机器上没有找到企业微信文档目录。")

    def on_close(self):
        self.stop_realtime_recovery("窗口关闭，已停止实时恢复。", write_log=False)
        self.root.destroy()

    def process_ui_queue(self):
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                self.log(self.localize_log_message(payload))
            elif kind == "process_hits":
                self.populate_process_tree(payload)
            elif kind == "corp_discovery_result":
                self.set_busy(False, "账号ID反查完成")
                candidates = list(payload or [])
                if not candidates:
                    self.selection_hint_var.set("没有从运行中的企业微信里识别到账号ID。请确认企业微信已登录，再查看 Documents\\WXWork 下的数字目录。")
                    self.simple_error_var.set("没有识别到企业账号ID。可以手动填写 Documents\\WXWork 下的纯数字文件夹名。")
                    self.log("没有从运行中的 WXWork.exe 内存路径里识别到企业账号ID。")
                    messagebox.showinfo("未识别到企业账号ID", self.corp_id_manual_help_text())
                    continue

                selected = candidates[0]
                selected_corp_id = str(selected.get("corp_id") or "")
                self.corp_id_var.set(selected_corp_id)
                self.select_corp_tree_item(selected_corp_id)
                candidate_text = "，".join(
                    f"{item.get('corp_id')}({item.get('total_hits', 0)}次)"
                    for item in candidates[:5]
                )
                self.selection_hint_var.set(f"已从运行中的企业微信识别到账号ID：{selected_corp_id}。候选：{candidate_text}")
                self.simple_error_var.set(f"已自动识别企业账号ID：{selected_corp_id}，正在继续扫描匹配进程。")
                self.log(f"已从运行中的 WXWork.exe 识别到企业账号ID：{selected_corp_id}；候选：{candidate_text}")
                self.start_process_scan()
            elif kind == "conversation_list":
                self.latest_sqlite_var.set(payload["sqlite_path"])
                self.conversation_metadata = payload.get("metadata", {})
                self.conversation_self_user_id = payload.get("self_user_id", "")
                self.all_conversation_rows = self.supported_conversation_rows(payload["conversations"])
                self.conversation_preview_cache = {}
                self.apply_conversation_filter()
            elif kind == "conversation_preview":
                if payload.get("token") != self.preview_request_token:
                    continue
                preview_payload = payload.get("payload", {})
                cache_key = (preview_payload.get("sqlite_path", self.latest_sqlite_var.get().strip()), payload.get("conversation_id", ""))
                self.conversation_preview_cache[cache_key] = preview_payload
                self.render_conversation_preview(preview_payload)
            elif kind == "conversation_preview_failed":
                if payload.get("token") != self.preview_request_token:
                    continue
                current_row = self.conversation_index_by_id.get(payload.get("conversation_id", ""))
                self.update_preview_card_from_conversation(current_row, subtitle="聊天内容加载失败。")
                summary = self.summarize_error(payload.get("log", "")) or "聊天内容加载失败。"
                self.preview_status_var.set(summary)
                self.set_preview_text_message(summary)
                self.log(payload.get("log", ""))
            elif kind == "recovery_result":
                outputs = payload.get("outputs", {})
                if outputs:
                    for key, value in outputs.items():
                        self.log(self.localize_log_message(f"{key}: {value}"))
                self.refresh_results()
            elif kind == "organize_result":
                self.log(f"整理索引：{payload.get('index_csv', '')}")
                self.log(f"全部会话总表：{payload.get('all_conversations_csv', '')}")
                self.log(f"批次清单：{payload.get('manifest_json', '')}")
                exported = payload.get("exported", [])
                if len(exported) == 1:
                    item = exported[0]
                    folder = item.get("folder", "")
                    timeline_csv = item.get("timeline_csv", "")
                    senders_csv = item.get("senders_csv", "")
                    media_index_csv = item.get("media_index_csv", "")
                    if folder:
                        self.log(f"会话目录：{folder}")
                    if timeline_csv:
                        self.log(f"聊天记录时间线：{timeline_csv}")
                    if senders_csv:
                        self.log(f"发送人映射：{senders_csv}")
                    if media_index_csv:
                        self.log(f"媒体索引：{media_index_csv}")

                    if self.auto_open_organized_timeline:
                        target_path = Path(timeline_csv) if timeline_csv else None
                        if target_path and target_path.exists():
                            open_path(target_path)
                            self.conversation_hint_var.set("已自动打开该会话的聊天记录 CSV。索引 CSV 只是索引，不是聊天明细。")
                        elif folder and Path(folder).exists():
                            open_path(Path(folder))
                            self.conversation_hint_var.set("已自动打开该会话目录。聊天记录在目录内的“会话名_聊天记录.csv”。")
                        else:
                            self.conversation_hint_var.set("该会话已整理完成。聊天记录在会话目录内的“会话名_聊天记录.csv”，不是索引 CSV。")
                    else:
                        self.conversation_hint_var.set("该会话已整理完成。聊天记录在会话目录内的“会话名_聊天记录.csv”，不是索引 CSV。")
                else:
                    self.conversation_hint_var.set(f"最近一次已整理 {len(exported)} 个会话。可打开“全部会话总表”或“打开最近整理时间线”继续查看。")
                self.auto_open_organized_timeline = False
            elif kind == "task_done":
                self.set_busy(False, payload["status"])
                task_type = payload.get("task_type", "")
                auto_mode = bool(payload.get("auto_mode"))
                if task_type == "recovery":
                    self.update_realtime_status_after_cycle(payload["status"], auto_mode=auto_mode, success=payload["status"] == "恢复完成")
                if payload["status"] == "恢复完成":
                    self.simple_error_var.set("自动恢复已完成一轮，可继续查看最新会话。" if auto_mode else "恢复完成。现在可以点“打开最新 CSV”查看结果。")
                else:
                    self.simple_error_var.set(payload["log"])
                self.log(payload["log"])
            elif kind == "task_failed":
                self.set_busy(False, payload["status"])
                self.log(payload["log"])
                summary = self.summarize_error(payload["log"]) or payload["status"]
                task_type = payload.get("task_type", "")
                auto_mode = bool(payload.get("auto_mode"))
                if task_type == "recovery":
                    self.update_realtime_status_after_cycle(summary, auto_mode=auto_mode, success=False)
                self.simple_error_var.set(summary)
                if not auto_mode:
                    messagebox.showerror(payload["status"], f"{summary}\n\n详细信息请看右侧“恢复日志”最下面一行。")

        self.root.after(150, self.process_ui_queue)


def main():
    enable_high_dpi_awareness()
    root = tk.Tk()
    app = RecoveryApp(root)
    root.mainloop()
    return app


if __name__ == "__main__":
    main()
