"""
自定义日志 Handler，缓解 Windows 下按时间轮转时的文件锁问题。

TimedRotatingFileHandler 轮转会对当前日志文件执行 os.rename。若 app.log 被其他进程
占用（IDE 预览、资源管理器、Get-Content -Wait、杀毒实时扫描等），会抛出
PermissionError / WinError 32，标准库会打印 “Logging error”，且 rolloverAt 未更新
会导致此后每次写入都反复尝试轮转。

失败时：推迟下一次 rollover 时间，并重新打开原路径继续追加写入，保证业务日志不中断。
"""

from __future__ import annotations

import logging.handlers
import time


def _is_file_lock_during_rotate(exc: OSError) -> bool:
    if isinstance(exc, PermissionError):
        return True
    # Windows ERROR_SHARING_VIOLATION
    if getattr(exc, 'winerror', None) == 32:
        return True
    return False


class SafeTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """与 TimedRotatingFileHandler 相同，轮转失败时在 Windows 上降级而不是抛错。"""

    def doRollover(self) -> None:
        try:
            super().doRollover()
        except OSError as exc:
            if not _is_file_lock_during_rotate(exc):
                raise
            self._recover_after_failed_rollover(exc)

    def _recover_after_failed_rollover(self, exc: OSError) -> None:
        if self.stream:
            try:
                self.stream.close()
            except OSError:
                pass
            self.stream = None

        if not self.delay:
            self.stream = self._open()
        self.rolloverAt = self.computeRollover(int(time.time()))
