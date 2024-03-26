import time
from enum import Enum

from rich.prompt import Prompt


class ChaoxingProcessState(Enum):
    """
    超星进程状态
    """
    INIT = 0  # 初始化, 未开始任务
    RUNNING = 1  # 任务进行中
    SUCCESS = 2  # 任务成功
    Failed = 3  # 任务失败


def check_timeout(process):
    if process.state == ChaoxingProcessState.RUNNING:  # 如果进程正在运行
        if time.time() - process.last_refresh_time > 86400:  # 如果超过86400秒(24小时)没有刷新
            return True
    else:  # 如果进程不在运行
        if time.time() - process.last_refresh_time > 300:  # 如果进程已经开始超过300秒(5分钟)
            return True
    return False


class ChaoxingWebPrompt:

    def __init__(self):
        super().__init__()
        self.input_queue = {}  # 输入队列  {process_id: value}

    def ask(self, text, console):
        if console.mode:
            process_id = console.process.process_id

            # 如果不在输入队列中, 则将其加入输入队列
            if process_id not in list(self.input_queue.keys()):
                # 加入输入队列
                self.input_queue[process_id] = None

                # 这是证明是第一次尝试获取输入，把提示信息输出
                console.print(text)
            if self.input_queue[process_id] is not None:
                return self.input_queue.pop(process_id)
            else:
                # web模式下避免卡死，超时后返回一个"timeout"字符串
                if check_timeout(console.process):
                    # 更改进程为死亡状态
                    console.process.alive = False
                    return "timeout"
                else:
                    return None
        else:  # 如果不是web模式，正常使用Prompt.ask获取返回值
            return Prompt.ask(text, console=console)


chaoxing_web_prompt = ChaoxingWebPrompt()
