import time

from ortools.sat.python import cp_model


class ScheduleSolveProgressLogger:
    def __init__(self, interval_seconds=120, emit=None, clock=None):
        self.interval_seconds = float(interval_seconds)
        self.emit = emit or print
        self.clock = clock or time.time
        self.started = False
        self.first_solution_reported = False
        self.better_solution_since_last_emit = False
        self.last_periodic_emit_time = None

    def on_start(self):
        now = self.clock()
        self.started = True
        self.last_periodic_emit_time = now
        self._emit("开始计算排产方案...")

    def on_first_solution_found(self):
        if self.first_solution_reported:
            return
        self.first_solution_reported = True
        self._emit("已找到一个排产方案，正在进一步计算搜索...")

    def on_better_solution_found(self):
        self.better_solution_since_last_emit = True

    def maybe_emit_periodic(self):
        now = self.clock()
        if self.last_periodic_emit_time is None:
            self.last_periodic_emit_time = now
            return
        if now - self.last_periodic_emit_time < self.interval_seconds:
            return

        if self.better_solution_since_last_emit:
            self._emit("搜索到更好的排产方案，正在进一步计算搜索...")
            self.better_solution_since_last_emit = False
        else:
            self._emit("正在计算搜索...")
        self.last_periodic_emit_time = now

    def on_optimal_found(self):
        self._emit("已找到最优排产方案")

    def on_time_limit(self):
        self._emit("搜索结束，已返回当前最佳排产方案")

    def on_infeasible(self):
        self._emit("未找到可行的排产方案")

    def on_exception(self):
        self._emit("排产计算异常")

    def on_stop_requested(self):
        self._emit("收到停止请求，正在导出当前最佳方案")

    def on_stopped_with_result(self):
        self._emit("已停止搜索并输出当前最佳方案")

    def _emit(self, message):
        self.emit(message)


class CpSearchProgressCallback(cp_model.CpSolverSolutionCallback):
    def __init__(self, progress_logger, stop_checker=None):
        super().__init__()
        self.progress_logger = progress_logger
        self.stop_checker = stop_checker

    def OnSolutionCallback(self):
        self.progress_logger.on_better_solution_found()
        self.progress_logger.maybe_emit_periodic()
        if self.stop_checker is not None and self.stop_checker():
            self.StopSearch()
