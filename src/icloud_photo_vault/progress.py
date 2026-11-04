"""任务进度回调：一句说明，加上可选的完成数和总数，状态栏用它画进度条。"""

from typing import Protocol


class Progress(Protocol):
    def __call__(self, message: str, done: int | None = None, total: int | None = None) -> None: ...


def no_progress(message: str, done: int | None = None, total: int | None = None) -> None:
    pass
