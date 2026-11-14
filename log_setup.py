#!/usr/bin/env python3
"""
log_setup.py — 统一日志配置模块

特性：
  - 按日期分文件：logs/mcttk_YYYY-MM-DD.log（跨午夜自动切换文件）
  - 同时输出到控制台（写原始 stdout，不会与落盘重复）
  - 接管 print() / traceback 的 stdout、stderr：第三方库和遗留 print 也进日志
  - 按天数自动清理旧日志
  - 未调用 setup_logging() 时（测试、单模块调试）退化为输出到 stdout，不吞消息

环境变量：
  MCTTK_LOG_DIR        日志目录（默认 logs）
  MCTTK_LOG_LEVEL      日志级别 DEBUG/INFO/WARNING/ERROR（默认 INFO）
  MCTTK_LOG_CONSOLE    是否输出到控制台 1/0（默认 1）
  MCTTK_LOG_KEEP_DAYS  日志保留天数（默认 90；0 表示不清理）
  MCTTK_LOG_CAPTURE    是否把 stdout/stderr 一并写入日志 1/0（默认 1）

用法：
  from log_setup import setup_logging, log_info
  setup_logging()                      # 入口调用一次
  log_info("[主] 下载完成: %s", path)   # 之后各处用 log_* 记录
"""

import contextlib
import logging
import os
import re
import sys
import threading
from datetime import datetime, timedelta

LOGGER_NAME = "mcttk"
DEFAULT_LOG_DIR = "logs"
DEFAULT_KEEP_DAYS = 90
DEFAULT_LEVEL = logging.INFO

FILE_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
CONSOLE_FORMAT = "[%(levelname)s] %(message)s"

_LOG_NAME_RE = re.compile(r"^mcttk_(\d{4}-\d{2}-\d{2})\.log(?:\.\d+)?$")

# 安装到 sys.stdout / sys.stderr 上的分流器（进程内单例）
_stdout_tee = None
_stderr_tee = None


# ── 大小格式化 ────────────────────────────────────────

def human_size(num_bytes) -> str:
    """把字节数格式化成 12.3 KB / 4.5 MB 这样的短串。"""
    if num_bytes is None:
        return "-"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{size:.0f} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def _format_duration(seconds) -> str:
    """秒数格式化为 1.2s / 3m20.5s。"""
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}m{rest:.1f}s"


# ── 配置解析 ──────────────────────────────────────────

def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_level(default=DEFAULT_LEVEL) -> int:
    raw = (os.environ.get("MCTTK_LOG_LEVEL") or "").strip().upper()
    if not raw:
        return default
    level = logging.getLevelName(raw)
    return level if isinstance(level, int) else default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


# ── 按日文件处理器 ────────────────────────────────────

class DailyFileHandler(logging.Handler):
    """按日期写文件：logs/mcttk_YYYY-MM-DD.log，跨天自动换文件。"""

    def __init__(self, log_dir: str = DEFAULT_LOG_DIR, days_to_keep: int = DEFAULT_KEEP_DAYS,
                 encoding: str = "utf-8"):
        super().__init__()
        self.log_dir = os.path.abspath(log_dir)
        os.makedirs(self.log_dir, exist_ok=True)
        self.encoding = encoding
        self.days_to_keep = max(int(days_to_keep), 0)
        self._date = ""
        self._stream = None
        self._suppress_begin()
        self._switch_to(datetime.now())
        self._prune(datetime.now())

    # -- 内部 --------------------------------------------------

    def _suppress_begin(self):
        """确保 self.lock 一定存在（Handler 在部分实现下可能未建锁）。"""
        if getattr(self, "lock", None) is None:
            self.lock = threading.RLock()

    def _path_for(self, now: datetime) -> str:
        return os.path.join(self.log_dir, f"mcttk_{now:%Y-%m-%d}.log")

    def _switch_to(self, now: datetime):
        date = now.strftime("%Y-%m-%d")
        if self._stream is not None and date == self._date:
            return
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.close()
        path = self._path_for(now)
        self._stream = open(path, "a", encoding=self.encoding)  # noqa: SIM115 - 生命周期由 close() 管理
        self._date = date

    def _prune(self, now: datetime):
        """删除超过保留期的日志文件（含旧的 .1/.2 轮转后缀）。"""
        if self.days_to_keep <= 0:
            return
        cutoff = (now - timedelta(days=self.days_to_keep)).date()
        removed = 0
        try:
            names = os.listdir(self.log_dir)
        except OSError:
            return
        for name in names:
            match = _LOG_NAME_RE.match(name)
            if not match:
                continue
            try:
                file_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if file_date < cutoff:
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(self.log_dir, name))
                    removed += 1
        if removed:
            self.write_text("日志", f"已清理 {removed} 个超过 {self.days_to_keep} 天的旧日志")

    def _write(self, line: str):
        """统一写入入口：持锁、按日切换、立即 flush（容器被 kill 也不丢日志）。"""
        with self.lock:
            self._switch_to(datetime.now())
            self._stream.write(line + "\n")
            self._stream.flush()

    # -- 对外 --------------------------------------------------

    @property
    def path(self) -> str:
        """当前正在写入的日志文件路径。"""
        return os.path.join(self.log_dir, f"mcttk_{self._date}.log")

    def write_text(self, tag: str, text: str):
        """把非 logging 来源的文本（print / stderr）写进日志文件。"""
        stamp = datetime.now().strftime(DATE_FORMAT)
        self._write(f"{stamp} [{tag}] {text}")

    def emit(self, record):
        try:
            self._write(self.format(record))
        except Exception:
            self.handleError(record)

    def close(self):
        with self.lock:
            if self._stream is not None:
                with contextlib.suppress(Exception):
                    self._stream.flush()
                    self._stream.close()
                self._stream = None
        super().close()


# ── 控制台处理器 ──────────────────────────────────────

class _StdoutHandler(logging.Handler):
    """把日志写到"当前"的 sys.stdout。

    与 logging.StreamHandler 的区别：发射时动态取 sys.stdout，
    因此 pytest capsys、入口处替换的 TextIOWrapper 都能正确捕获。
    """

    def __init__(self, level=logging.NOTSET):
        super().__init__(level)
        self.setFormatter(logging.Formatter(CONSOLE_FORMAT))

    def emit(self, record):
        try:
            stream = sys.stdout
            if stream is None:
                return
            stream.write(self.format(record) + "\n")
            stream.flush()
        except Exception:
            self.handleError(record)


# ── stdout / stderr 分流 ──────────────────────────────

class _StreamTee:
    """包住原始文本流：照常写到原流，同时按行同步进日志文件。

    这样才能覆盖 `print()`、`traceback.print_exc()`、第三方库的输出——
    否则这些内容只会进 `docker logs`，永远进不了日志文件。
    """

    def __init__(self, stream, handler: DailyFileHandler, tag: str):
        self._stream = stream
        self.handler = handler
        self.tag = tag
        self._pending = ""

    # -- logging.Handler 只关心 write/flush --------------------
    def write(self, data):
        if not isinstance(data, str):
            data = str(data)
        with contextlib.suppress(Exception):
            self._stream.write(data)
        self._pending += data
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self._log_line(line)
        return len(data)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        with contextlib.suppress(Exception):
            self._stream.flush()
        if self._pending:
            line, self._pending = self._pending, ""
            self._log_line(line)

    def _log_line(self, line: str):
        line = line.rstrip("\r")
        if not line.strip():
            return
        with contextlib.suppress(Exception):
            self.handler.write_text(self.tag, line)

    # -- 其余属性/方法全部委派给原始流 -------------------------
    def __getattr__(self, name):
        return getattr(self._stream, name)

    @property
    def encoding(self):
        return getattr(self._stream, "encoding", "utf-8")

    def isatty(self):
        return False


def _install_stream_capture(handler: DailyFileHandler):
    """接管 sys.stdout / sys.stderr，使所有输出都能落到日志文件。"""
    global _stdout_tee, _stderr_tee

    if _stdout_tee is not None:
        # 重复 setup：只换 handler，避免层层套娃
        _stdout_tee.handler = handler
        if _stderr_tee is not None:
            _stderr_tee.handler = handler
        return

    stdout_tee = _StreamTee(sys.stdout, handler, "STDOUT")
    stderr_tee = _StreamTee(sys.stderr, handler, "STDERR")
    _stdout_tee, _stderr_tee = stdout_tee, stderr_tee
    sys.stdout = stdout_tee
    sys.stderr = stderr_tee


# ── 主入口 ────────────────────────────────────────────

def setup_logging(
    log_dir: str = None,
    log_level: int = None,
    console_output: bool = None,
    capture_streams: bool = None,
    days_to_keep: int = None,
) -> logging.Logger:
    """配置统一日志系统（入口脚本调用一次）。

    Args:
        log_dir: 日志目录，默认环境变量 MCTTK_LOG_DIR 或 "logs"
        log_level: 日志级别，默认环境变量 MCTTK_LOG_LEVEL 或 INFO
        console_output: 是否同时输出到控制台，默认环境变量 MCTTK_LOG_CONSOLE 或 True
        capture_streams: 是否把 stdout/stderr 同步到日志文件，默认 MCTTK_LOG_CAPTURE 或 True
        days_to_keep: 日志保留天数，默认 MCTTK_LOG_KEEP_DAYS 或 90

    Returns:
        配置好的 logger 实例
    """
    log_dir = log_dir or os.environ.get("MCTTK_LOG_DIR") or DEFAULT_LOG_DIR
    if log_level is None:
        log_level = _env_level()
    if console_output is None:
        console_output = _env_flag("MCTTK_LOG_CONSOLE", True)
    if capture_streams is None:
        capture_streams = _env_flag("MCTTK_LOG_CAPTURE", True)
    if days_to_keep is None:
        days_to_keep = _env_int("MCTTK_LOG_KEEP_DAYS", DEFAULT_KEEP_DAYS)

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(log_level)
    logger.propagate = False

    # 移除旧处理器（避免重复写入 / 文件句柄泄漏）
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()

    formatter = logging.Formatter(FILE_FORMAT, datefmt=DATE_FORMAT)

    file_handler = DailyFileHandler(log_dir, days_to_keep=days_to_keep)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console_output:
        # 绑定到"安装分流器之前"的原始 stdout，避免控制台输出被重复写进日志文件。
        # 重复调用 setup_logging 时 sys.stdout 已经是分流器，要取它包住的原始流。
        console_stream = _stdout_tee._stream if _stdout_tee is not None else sys.stdout
        console = logging.StreamHandler(console_stream)
        console.setLevel(log_level)
        console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        logger.addHandler(console)

    if capture_streams:
        _install_stream_capture(file_handler)

    logger.info(
        "[日志] 写入 %s（级别 %s，保留 %s，控制台%s，标准输出捕获%s）",
        file_handler.path,
        logging.getLevelName(log_level),
        f"{days_to_keep} 天" if days_to_keep else "永久",
        "开" if console_output else "关",
        "开" if capture_streams else "关",
    )
    return logger


def get_logger() -> logging.Logger:
    """获取 logger 实例。

    若尚未调用 setup_logging()（例如测试或单模块运行），自动挂一个
    写向当前 sys.stdout 的兜底处理器，保证消息不会被静默丢弃。
    """
    logger = logging.getLogger(LOGGER_NAME)
    if not logger.handlers:
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        logger.addHandler(_StdoutHandler(level=logging.DEBUG))
    return logger


def reset_logging():
    """撤销日志配置并恢复 sys.stdout / sys.stderr（测试用）。"""
    global _stdout_tee, _stderr_tee

    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()

    if _stdout_tee is not None:
        with contextlib.suppress(Exception):
            _stdout_tee.flush()
        if sys.stdout is _stdout_tee:
            sys.stdout = _stdout_tee._stream
        _stdout_tee = None
    if _stderr_tee is not None:
        with contextlib.suppress(Exception):
            _stderr_tee.flush()
        if sys.stderr is _stderr_tee:
            sys.stderr = _stderr_tee._stream
        _stderr_tee = None


def current_log_path() -> str:
    """返回当前日志文件路径（未配置时返回空串）。"""
    for handler in get_logger().handlers:
        if isinstance(handler, DailyFileHandler):
            return handler.path
    return ""


# ── 便捷函数 ──────────────────────────────────────────

def log_info(message: str, *args, **kwargs):
    """记录信息级别日志"""
    get_logger().info(message, *args, **kwargs)


def log_error(message: str, *args, **kwargs):
    """记录错误级别日志（传 exc_info=True 可带堆栈）"""
    get_logger().error(message, *args, **kwargs)


def log_debug(message: str, *args, **kwargs):
    """记录调试级别日志"""
    get_logger().debug(message, *args, **kwargs)


def log_warning(message: str, *args, **kwargs):
    """记录警告级别日志"""
    get_logger().warning(message, *args, **kwargs)


def log_section(title: str):
    """记录一条分段横幅，便于在长日志里定位阶段。"""
    get_logger().info("%s %s %s", "=" * 18, title, "=" * 18)


def elapsed_since(start_monotonic: float) -> str:
    """配合 time.monotonic() 使用，返回 "1.2s" 形式的耗时文本。"""
    import time as _time
    return _format_duration(_time.monotonic() - start_monotonic)
