from abc import ABC, abstractmethod


class AlgorithmService(ABC):
    """正式算法实现需要遵循的最小接口。"""

    @abstractmethod
    def start(self, solve_task_id, input_params, aps_items, plan_items):
        raise NotImplementedError

    @abstractmethod
    def stop(self, solve_task_id):
        raise NotImplementedError
