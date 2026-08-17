"""应用启动时恢复算法任务状态的轻量入口。"""

from core.models import SolveTask


def register_process_exit_hooks():
    # 算法子进程持有自己的停止协议；Web 进程退出时不应强制终止正在求解的任务。
    return None


def recover_orphaned_running_solves():
    from algorithm.adapter import sync_solve_task

    for solve_task_id in SolveTask.objects.filter(
        solveStatus__in=[0, 1],
        isDeleted=0,
    ).values_list("solveTaskId", flat=True):
        sync_solve_task(solve_task_id)
