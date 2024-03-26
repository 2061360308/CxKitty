import json
import threading
import time
import uuid

import uvicorn
from starlette.responses import StreamingResponse, Response, HTMLResponse

from web.chaoxingWorker import Multitasking

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from web.utils import chaoxing_web_prompt

app = FastAPI()
# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有方法
    allow_headers=["*"],  # 允许所有头部
)


@app.options("/{rest_of_path:path}", include_in_schema=False)
async def preflight_handler(rest_of_path: str):
    return Response(headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "*",
        "Access-Control-Allow-Headers": "*",
        "Access-Control-Allow-Credentials": "true",
    })


multitasking = Multitasking()


# 创建新的工作线程
def create_process():
    process_id = uuid.uuid4().hex
    multitasking.create_process(process_id)
    return process_id


def comm_stream_generator(process_id):
    """
    通信流生成器
    Args:
        process_id:

    Returns:

    """
    while True:
        try:
            output_new = multitasking.get_process(process_id).console.get_update_output()

            update = False  # 是否有更新
            if output_new != "":
                update = True

            data = str(json.dumps({"update": update, "output": output_new}))
            yield f"data: {data}\n\n"  # 将数据序列化为JSON，然后编码为字节流
            time.sleep(2)
        except (ConnectionResetError, BrokenPipeError):
            # 客户端断开连接，停止生成事件
            print("Client disconnected")


# 获取工作线程的ID
@app.get("/get_process_id")
def get_process_id(phone: str):
    """
    获取工作线程的ID
    Args:
        phone:

    Returns:

    """
    process_id = multitasking.get_process_id(phone)
    if process_id is None:
        print("没找到这个手机号的进程ID", phone)
        process_id = create_process()

    for i in multitasking.tasks:
        print(i.process_id, i.phone)
    print('分配工作线程id', process_id)

    return {"status": "success", "process_id": process_id}


# 获取工作线程的历史输出
@app.get("/get_process_output")
def get_process_output(process_id: str):
    """
    获取进程的输出
    Args:
        process_id: 目标进程的ID

    Returns:

    """
    output = multitasking.get_process(process_id).console.get_output()
    return {"status": "success", "output": output}


# 连接通信流
@app.get("/comm_stream")
def comm_stream(process_id: str):
    """
    连接通信流
    Args:
        process_id:

    Returns:

    """
    return StreamingResponse(comm_stream_generator(process_id), media_type="text/event-stream")


# 更新进程的最后刷新的时间
@app.get("/update_process_refresh_time")
def update_process_refresh_time(process_id: str):
    """
    更新进程的最后刷新的时间
    Args:
        process_id: 目标进程的ID
        refresh_time: 刷新时间

    Returns:

    """
    process = multitasking.get_process(process_id)
    if process is None:
        return {"status": "error", "message": "No process found with id {}".format(process_id)}
    process.last_refresh_time = time.time()
    return {"status": "success"}


@app.get("/send_value")
def send_value(process_id: str, value: str):
    """
    发送值
    Args:
        process_id:
        value:

    Returns:

    """
    # 如果进程ID在输入队列中, 则更新值
    if process_id in list(chaoxing_web_prompt.input_queue.keys()):
        chaoxing_web_prompt.input_queue[process_id] = value
    print("得到输入", process_id, value)
    return {"status": "success"}


# 接口正常访问测试
@app.get("/test")
def test():
    return {"status": "success"}


@app.get("/test2")
def test2():
    namelist = []
    for thread in threading.enumerate():
        namelist.append(thread.name)
        print(thread.name)
    return {"status": "success", "namelist": namelist}


with open("./web/index.html", "r", encoding='utf-8') as f:
    index_html = f.read()


@app.get("/index", response_class=HTMLResponse)
async def index():
    global index_html
    return index_html


# 启动uvicorn服务，默认端口8000，main对应文件名
if __name__ == '__main__':
    uvicorn.run('app:app', port=2333)
