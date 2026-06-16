"""Terminal output helpers for compact, status-line based logs.

This module is intentionally stdlib-only because ``start.py`` imports it
before the virtual environment and third-party dependencies are guaranteed.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import atexit
import unicodedata
from collections import deque
from pathlib import Path
from typing import Optional, TextIO


_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_DEFAULT_WIDTH = 120
_DIM = "\033[2m"
_RESET = "\033[0m"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def resolve_terminal_log_mode(default: str = "block") -> str:
    raw = (
        os.getenv("TERMINAL_LOG_MODE")
        or os.getenv("CONSOLE_LOG_MODE")
        or default
        or "block"
    )
    mode = str(raw).strip().lower()
    if mode == "compact":
        return "block"
    if mode in {"block", "status", "plain"}:
        return mode
    fallback = str(default or "block").strip().lower() or "block"
    return "block" if fallback == "compact" else fallback


def _stream_is_tty(stream: TextIO) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def _terminal_width(default: int = _DEFAULT_WIDTH) -> int:
    try:
        import shutil

        return max(40, int(shutil.get_terminal_size((default, 24)).columns))
    except Exception:
        return default


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", str(text or ""))


def compact_one_line(text: str, *, max_width: Optional[int] = None) -> str:
    raw = strip_ansi(str(text or "")).replace("\r", " ").replace("\n", " ")
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return ""
    width = max_width or _terminal_width()
    budget = max(20, int(width) - 10)
    if len(raw) <= budget:
        return raw
    return f"{raw[:budget - 1]}…"


def _block_line_count() -> int:
    raw = str(os.getenv("TERMINAL_LOG_BLOCK_LINES", "") or "").strip()
    if not raw:
        return 1
    try:
        value = int(raw)
    except Exception:
        return 5
    return min(20, max(1, value))


def start_message_is_permanent(message: str) -> bool:
    text = str(message or "").strip()
    if not text:
        return False
    if text.startswith(("[ERROR]", "[WARN]")):
        return True
    if text.startswith("错误:") or text.startswith("警告:"):
        return True
    if any(token in text for token in ("API 地址:", "控制面板:", "API 文档:", "按 Ctrl+C 停止服务")):
        return True
    if any(token in text for token in ("服务已停止", "服务异常退出", "检测到配置更新")):
        return True
    if "启动脚本" in text or "服务启动中" in text:
        return True
    if text.startswith("==="):
        return True
    return False


def log_record_is_permanent(level_name: str, logger_name: str, message: str) -> bool:
    level = str(level_name or "").upper()
    name = str(logger_name or "").upper()
    text = str(message or "").strip()

    if level in {"WARNING", "ERROR", "CRITICAL"}:
        return True
    if not text:
        return False

    important_tokens = (
        "异常退出",
        "启动完成",
        "服务已",
        "请求已取消",
        "请求取消",
        "强制退出",
        "超时",
        "失败",
        "错误",
        "bridge",
        "桥接",
        "已释放",
        "release",
        "released",
        "failed",
        "cancelled",
        "canceled",
    )
    if any(token in text.lower() for token in important_tokens if token.isascii()):
        return True
    if any(token in text for token in important_tokens if not token.isascii()):
        return True

    if name == "REQUEST" and ("完成" in text or "completed" in text.lower()):
        lowered = text.lower()
        return any(token in lowered for token in ("status=failed", "status=cancelled", "status=canceled"))
    if name in {"MAIN", "BROWSER"} and any(token in text for token in ("浏览器连接成功", "关闭浏览器连接")):
        return True
    return False


class TerminalDisplay:
    """Renderer that keeps noisy progress in a transient status area."""

    def __init__(
        self,
        *,
        stream: Optional[TextIO] = None,
        mode: Optional[str] = None,
        force_tty: bool = False,
    ) -> None:
        self.stream = stream or sys.stdout
        self.mode = mode or resolve_terminal_log_mode()
        self.force_tty = force_tty
        self._lock = threading.RLock()
        self._status_active = False
        self._block_active = False
        self._block_title = ""
        self._block_lines: deque[str] = deque(maxlen=_block_line_count())
        self._rendered_line_count = 0
        self._rendered_row_count = 0

    def _stream_width(self) -> int:
        try:
            fileno = getattr(self.stream, "fileno", lambda: None)()
            if fileno is not None:
                return max(40, int(os.get_terminal_size(fileno).columns))
        except Exception:
            pass
        return _terminal_width()

    @staticmethod
    def _display_width(text: str) -> int:
        width = 0
        for char in strip_ansi(str(text or "")):
            if unicodedata.combining(char):
                continue
            if unicodedata.category(char)[0] == "C":
                continue
            width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        return width

    def _screen_rows(self, text: str) -> int:
        columns = max(20, self._stream_width())
        # Keep one column of slack to avoid terminals entering pending-wrap state.
        usable = max(1, columns - 1)
        return max(1, (self._display_width(text) + usable - 1) // usable)

    @property
    def dynamic(self) -> bool:
        if self.mode not in {"status", "block"}:
            return False
        if _env_flag("TERMINAL_LOG_FORCE_DYNAMIC"):
            return True
        return self.force_tty or _stream_is_tty(self.stream)

    @property
    def compact_enabled(self) -> bool:
        return self.mode in {"status", "block"} and self.dynamic

    @property
    def block_enabled(self) -> bool:
        return self.mode == "block" and self.dynamic

    def should_suppress_child_output(self) -> bool:
        return self.compact_enabled

    def start_section(self, title: str) -> None:
        text = compact_one_line(title, max_width=self._stream_width() - 8)
        if not text:
            return
        with self._lock:
            if self.block_enabled:
                self._clear_transient_locked()
                self.stream.write(f"[STEP] {text}\n")
                self.stream.flush()
                self._block_title = ""
                self._block_lines.clear()
                return
            self.status(text)

    def ensure_section(self, title: str) -> None:
        text = compact_one_line(title, max_width=self._stream_width() - 4)
        if not text:
            return
        with self._lock:
            if not self.block_enabled:
                return
            if self._block_title != text:
                self._clear_transient_locked()
                self._block_title = text
                self._block_lines.clear()
                self._render_block_locked()

    def clear_status(self) -> None:
        if not self.dynamic:
            return
        with self._lock:
            self._clear_transient_locked()

    def _clear_transient_locked(self) -> None:
        if self._block_active:
            row_count = max(
                int(self._rendered_row_count or 0),
                int(self._rendered_line_count or 0),
                1,
            )
            if row_count > 1:
                self.stream.write(f"\033[{row_count - 1}A")
            self.stream.write("\r\033[J")
            self._block_active = False
            self._status_active = False
            self._rendered_line_count = 0
            self._rendered_row_count = 0
            self.stream.flush()
            return

        if self._status_active:
            self.stream.write("\r\033[2K")
            self.stream.flush()
            self._status_active = False

    def _dim(self, text: str) -> str:
        if os.environ.get("NO_COLOR"):
            return text
        return f"{_DIM}{text}{_RESET}"

    def _render_block_locked(self) -> None:
        if not self.dynamic:
            return
        rendered = [self._block_title] if self._block_title else []
        max_line_width = max(20, self._stream_width() - 4)
        rendered.extend(
            self._dim(f"  {compact_one_line(line, max_width=max_line_width)}")
            for line in self._block_lines
        )
        if not rendered:
            return
        self.stream.write("\r\033[2K")
        self.stream.write("\n\r\033[2K".join(rendered))
        self.stream.flush()
        self._block_active = True
        self._status_active = False
        self._rendered_line_count = len(rendered)
        self._rendered_row_count = sum(self._screen_rows(line) for line in rendered)

    def _append_block_locked(self, message: str) -> None:
        text = compact_one_line(message, max_width=max(20, self._stream_width() - 4))
        if not text:
            return
        self._clear_transient_locked()
        self._block_lines.append(text)
        self._render_block_locked()

    def permanent(self, message: str = "") -> None:
        with self._lock:
            if self.dynamic:
                self._clear_transient_locked()
            self.stream.write(f"{message}\n")
            self.stream.flush()

    def status(self, message: str) -> None:
        text = compact_one_line(message)
        if not text:
            return
        with self._lock:
            if self.block_enabled:
                self._append_block_locked(text)
                return
            if not self.dynamic:
                self.stream.write(f"{text}\n")
                self.stream.flush()
                return
            self.stream.write(f"\r\033[2Kstatus: {text}")
            self.stream.flush()
            self._status_active = True

    def write(self, message: str = "", *, permanent: Optional[bool] = None) -> None:
        if permanent is None:
            permanent = self.mode == "plain" or not self.dynamic or start_message_is_permanent(message)
        if permanent or self.mode == "plain":
            self.permanent(message)
            return
        self.status(message)


_GLOBAL_DISPLAY: Optional[TerminalDisplay] = None
_GLOBAL_DISPLAY_LOCK = threading.Lock()
_GLOBAL_DISPLAY_CLEANUP_REGISTERED = False


def get_terminal_display() -> TerminalDisplay:
    global _GLOBAL_DISPLAY, _GLOBAL_DISPLAY_CLEANUP_REGISTERED
    with _GLOBAL_DISPLAY_LOCK:
        if _GLOBAL_DISPLAY is None:
            _GLOBAL_DISPLAY = TerminalDisplay()
        if not _GLOBAL_DISPLAY_CLEANUP_REGISTERED:
            atexit.register(_GLOBAL_DISPLAY.clear_status)
            _GLOBAL_DISPLAY_CLEANUP_REGISTERED = True
        return _GLOBAL_DISPLAY


def startup_subprocess_log_path(project_dir: Path) -> Path:
    configured = str(os.getenv("STARTUP_SUBPROCESS_LOG_FILE", "") or "").strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path(project_dir) / path
        return path.resolve()
    return Path(project_dir) / "logs" / "startup-subprocess.log"
