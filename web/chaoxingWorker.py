from __future__ import annotations

import json
import sys
import threading
import time
import re
from collections import deque
from enum import Enum
from os import PathLike
from typing import Literal, Optional, Union, List, Any, TextIO

from bs4 import BeautifulSoup
from rich.align import Align
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt
from rich.style import Style
from rich.text import TextType
from rich.traceback import install

import config
import dialog
from cxapi import (
    ChaoXingAPI,
    ChapterContainer,
    ClassSelector,
    ExamDto,
    PointDocumentDto,
    PointVideoDto,
    PointWorkDto,
)
from cxapi.exception import ChapterNotOpened, TaskPointError
from logger import Logger
from resolver import DocumetResolver, MediaPlayResolver, QuestionResolver
from utils import __version__, ck2dict, sessions_load
from web.utils import ChaoxingProcessState, check_timeout


class ChaoxingWebConsole(Console):
    last_output = ""  # 记录上次的输出，用于判断是否有更新

    def __init__(self, process, web_mode=False, height: int = 30):
        self.process = process
        self.mode = web_mode

        super().__init__(height=height, record=True)
        self.output_collector = deque(maxlen=50)

    def print(self, *args, **kwargs):
        # 如果是web模式，记录输出
        if self.mode:
            # 捕获输出不在控制台打印
            with self.capture() as capture:
                super().print(*args, **kwargs)
            self.collect_output()  # 记录输出
        else:
            super().print(*args, **kwargs)

    @staticmethod
    def parse_css(css):
        css_dict = {}
        for line in css.split('\n'):
            if '{' in line and '}' in line:
                selector, styles = line.split('{')
                selector = selector.strip()
                styles = styles.split('}')[0].strip()
                style_dict = {}
                for style in styles.split(';'):
                    if ':' in style:
                        property, value = style.split(':')
                        style_dict[property.strip()] = value.strip()
                css_dict[selector] = style_dict

        return css_dict

    @staticmethod
    def styles_to_string(styles):
        return '; '.join(f'{k}: {v}' for k, v in styles.items())

    def collect_output(self):
        html = self.export_html()

        # print(html)

        soup = BeautifulSoup(html, 'html.parser')

        styleMatch = re.search(r'<style>(.*?)</style>', html, re.DOTALL)

        if styleMatch:
            style_dict = self.parse_css(styleMatch.group(1))
            for selector, styles in style_dict.items():
                for item in soup.select(selector):
                    if 'style' in item.attrs:
                        item.attrs['style'] += self.styles_to_string(styles)
                    else:
                        item.attrs['style'] = self.styles_to_string(styles)

        # bodyMatch = re.search(r'<body>(.*?)</body>', html, re.DOTALL)
        self.output_collector.append(str(soup.body.pre.code))

    def get_output(self):
        if not self.mode:  # 如果不是web模式，直接返回
            return

        return "".join(self.output_collector)

    def get_update_output(self):
        if not self.mode:  # 如果不是web模式，直接返回
            return

        output = self.get_output()
        # 计算更新的部分
        if self.last_output == output:
            output = ""
        self.last_output = output

        return output


class ChaoxingProcess:
    """
    超星进程

    使用方法：
        控制台模式：
            process = ChaoxingProcess("process_id")
            process.run()
        Web模式：不在控制台打印，而是可以调用get_update_output获取更新的style和body
            process = ChaoxingProcess("process_id", web_mode=True)
            process.run()
    """

    alive = True  # 进程是否存活

    def __init__(self, process_id: str, phone: str | None = None, web_mode: bool = False):
        self.process_id = process_id
        self.phone = phone  # 手机号
        self.state = ChaoxingProcessState.INIT  # 进程状态
        self.begian_time = time.time()  # 开始时间
        self.last_refresh_time = time.time()  # 上次刷新时间

        self.api = ChaoXingAPI()
        self.console = ChaoxingWebConsole(process=self, height=config.TUI_MAX_HEIGHT, web_mode=web_mode)
        self.logger = Logger("Main")

        install(console=self.console, show_locals=False)

        self.layout = Layout()
        self.lay_left = Layout(name="Left")
        self.lay_right = Layout(name="Right", size=60)
        self.lay_right_content = Layout(name="RightContent")
        self.lay_session_notice = Layout(name="session_notice", size=6)
        self.lay_right.update(self.lay_right_content)

    def to_running(self):
        """
        进入运行状态
        Returns:

        """
        self.state = ChaoxingProcessState.RUNNING

    def to_success(self):
        """
        进入成功状态
        Returns:

        """
        self.state = ChaoxingProcessState.SUCCESS

    def to_failed(self):
        """
        进入失败状态
        Returns:

        """
        self.state = ChaoxingProcessState.Failed

    def to_init(self):
        self.state = ChaoxingProcessState.INIT

    @staticmethod
    def task_wait(tui_ctx: Layout, wait_sec: int, text: str):
        """课间等待, 防止风控"""
        tui_ctx.unsplit()
        for i in range(wait_sec):
            tui_ctx.update(
                Panel(
                    Align.center(
                        f"[green]{text}, 课间等待{i}/{wait_sec}s",
                        vertical="middle",
                    )
                )
            )
            time.sleep(1.0)

    def on_captcha_after(self, times: int):
        """识别验证码开始 回调"""
        if self.layout.get("session_notice") is None:
            self.lay_right.split_column(self.lay_right_content, self.lay_session_notice)
        self.lay_session_notice.update(
            Panel(
                f"[yellow]正在识别验证码，第 {times} 次...",
                title="[red]接口风控",
                border_style="yellow",
            )
        )

    def on_captcha_before(self, status: bool, code: str):
        """验证码识别成功 回调"""
        if status is True:
            self.lay_session_notice.update(
                Panel(
                    f"[green]验证码识别成功：[yellow]{code}[green]，提交正确",
                    title="[red]接口风控",
                    border_style="green",
                )
            )
            time.sleep(5.0)
            self.lay_right.unsplit()
        else:
            self.lay_session_notice.update(
                Panel(
                    f"[red]验证码识别成功：[yellow]{code}[red]，提交错误，10S 后重试",
                    title="[red]接口风控",
                    border_style="red",
                )
            )
            time.sleep(1.0)

    def on_face_detection_after(self, orig_url):
        """人脸识别开始 回调"""
        if self.layout.get("captcha") is None:
            self.lay_right.split_column(self.lay_right_content, self.lay_session_notice)
        self.lay_session_notice.update(
            Panel(
                f"[yellow]正在准备人脸识别...\nURL:{orig_url}",
                title="[red]人脸识别",
                border_style="yellow",
            )
        )

    def on_face_detection_before(self, object_id: str, image_path: PathLike):
        """人脸识别成功 回调"""
        self.lay_session_notice.update(
            Panel(
                f"[green]人脸识别提交成功：\nobjectId={object_id}\npath={image_path}",
                title="[red]人脸识别",
                border_style="green",
            )
        )
        time.sleep(5.0)
        self.lay_right.unsplit()

    def fuck_task_worker(self, chap: ChapterContainer):
        """章节任务点处理实现
        Args:
            chap: 章节容器对象
        """

        def _show_chapter(index: int):
            chap.set_tui_index(index)
            self.lay_right_content.update(
                Panel(
                    chap,
                    title=f"《{chap.name}》章节列表",
                    border_style="blue",
                )
            )

        self.layout.split_row(self.lay_left, self.lay_right)
        self.lay_left.update(
            Panel(
                Align.center(
                    "[yellow]正在扫描章节，请稍等...",
                    vertical="middle",
                )
            )
        )

        chap.fetch_point_status()
        with Live(self.layout, console=self.console) as live:
            # 遍历章节列表
            for index in range(len(chap)):
                _show_chapter(index)
                if chap.is_finished(index) and config.WORK["export"] is False:  # 如果该章节所有任务点做完, 那么就跳过
                    self.logger.info(
                        f"忽略完成任务点 "
                        f"[{chap.chapters[index].label}:{chap.chapters[index].name}(Id.{chap.chapters[index].chapter_id})]"
                    )
                    time.sleep(0.1)  # 解决强迫症, 故意添加延时, 为展示滚屏效果
                    continue
                refresh_flag = True
                # 获取当前章节的所有任务点, 并遍历
                for task_point in chap[index]:
                    # 拉取任务卡片 Attachment
                    try:
                        task_point.fetch_attachment()
                    except ChapterNotOpened:
                        if refresh_flag:
                            chap.refresh_chapter(index - 1)
                            refresh_flag = False
                            continue
                        else:
                            self.lay_left.unsplit()
                            self.lay_left.update(
                                Panel(
                                    Align.center(
                                        f"[red]章节【{chap.chapters[index].label}】《{chap.chapters[index].name}》未开放\n程序无法继续执行！",
                                        vertical="middle",
                                    ),
                                    border_style="red",
                                )
                            )
                            self.logger.error("\n-----*未开放章节, 程序异常退出*-----")
                            sys.exit()
                    refresh_flag = True
                    try:
                        # 开始分类讨论任务点类型
                        # 章节测验类型
                        if isinstance(task_point, PointWorkDto) and (
                                config.WORK_EN or config.WORK["export"] is True
                        ):
                            # 导出作业试题
                            if config.WORK["export"] is True:
                                task_point.parse_attachment()
                                # 保存 json 文件
                                task_point.export(
                                    config.EXPORT_PATH / f"work_{task_point.work_id}.json"
                                )

                            # 完成章节测验
                            if config.WORK_EN:
                                if not task_point.parse_attachment():
                                    continue
                                task_point.fetch_all()
                                # 实例化解决器
                                resolver = QuestionResolver(
                                    exam_dto=task_point,
                                    fallback_save=config.WORK["fallback_save"],
                                    fallback_fuzzer=config.WORK["fallback_fuzzer"],
                                )
                                # 传递 TUI ctx
                                self.lay_left.update(resolver)
                                # 开始执行自动接管
                                resolver.execute()
                                # 开始等待
                                self.task_wait(self.lay_left, config.WORK_WAIT, f"试题《{task_point.title}》已结束")

                        # 视频类型
                        elif isinstance(task_point, PointVideoDto) and config.VIDEO_EN:
                            if not task_point.parse_attachment():
                                continue
                            # 拉取取任务点数据
                            if not task_point.fetch():
                                continue
                            # 实例化解决器
                            resolver = MediaPlayResolver(
                                media_dto=task_point,
                                speed=config.VIDEO["speed"],
                                report_rate=config.VIDEO["report_rate"],
                            )
                            # 传递 TUI ctx
                            self.lay_left.update(resolver)
                            # 开始执行自动接管
                            resolver.execute()
                            # 开始等待
                            self.task_wait(self.lay_left, config.VIDEO_WAIT, f"视频《{task_point.title}》已结束")

                        # 文档类型
                        elif isinstance(task_point, PointDocumentDto) and config.DOCUMENT_EN:
                            if not task_point.parse_attachment():
                                continue
                            # 实例化解决器
                            resolver = DocumetResolver(document_dto=task_point)
                            # 传递 TUI ctx
                            self.lay_left.update(resolver)
                            # 开始执行自动接管
                            resolver.execute()

                            # 开始等待
                            self.task_wait(self.lay_left, config.DOCUMENT_WAIT, f"文档《{task_point.title}》已结束")

                    except (TaskPointError, NotImplementedError) as e:
                        self.logger.error(f"任务点自动接管执行异常 -> {e.__class__.__name__} {e.__str__()}")

                    # 刷新章节任务点状态
                    chap.fetch_point_status()
                    _show_chapter(index)

            self.lay_left.unsplit()
            self.lay_left.update(
                Panel(
                    Align.center("[green]该课程已通过", vertical="middle"),
                    border_style="green",
                )
            )
            time.sleep(5.0)

    def fuck_exam_worker(self, exam: ExamDto, export=False):
        """考试处理实现
        Args:
            exam: 考试接口对象
            export: 是否开启导出模式, 默认关闭
        """
        self.layout.split_row(self.lay_left, self.lay_right)
        with Live(self.layout, console=self.console) as live:
            # 拉取元数据
            exam.get_meta()
            # 开始考试
            exam.start()
            # 显示考试信息
            self.lay_right_content.update(Panel(exam, title="考试会话", border_style="blue"))

            # 若开启导出模式, 则不执行自动接管逻辑
            if export is True:
                export_path = config.EXPORT_PATH / f"exam_{exam.exam_id}.json"
                exam.export(export_path)
                live.stop()
                self.console.print(
                    f"[red]请注意，导出后考试已开始计时，时间仅剩 {exam.remain_time_str}！！[/]\n"
                    f"[yellow]应尽快使用 本程序 / Web端 / 客户端 作答[/]\n"
                    f"[green]试卷导出路径为：{export_path}"
                )
                return

            # 实例化解决器
            resolver = QuestionResolver(
                exam_dto=exam,
                fallback_save=False,  # 考试不存在临时保存特性
                fallback_fuzzer=config.EXAM["fallback_fuzzer"],
                persubmit_delay=config.EXAM["persubmit_delay"],
            )
            # 传递 TUI ctx
            self.lay_left.update(resolver)

            # 若开启交卷确认功能, 则注册提交回调
            if config.EXAM["confirm_submit"] is True:

                @resolver.reg_confirm_submit_cb
                def confirm(completed_cnt, incompleted_cnt, mistakes, exam_dto):
                    live.stop()
                    if (
                            Prompt.ask(
                                f"答题完毕，完成 [bold green]{completed_cnt}[/] 题，"
                                f"未完成 [bold red]{incompleted_cnt}[/] 题，"
                                f"请确认是否立即交卷",
                                console=self.console,
                                choices=["y", "n"],
                                default="y",
                            )
                            != "y"
                    ):
                        return False
                    live.start()
                    return True

            # 开始执行自动接管
            resolver.execute()

    def run(self):
        dialog.logo(self.console)
        acc_sessions = sessions_load()
        # 存在至少一个会话存档
        if acc_sessions:
            # 多用户, 允许进行选择
            if config.MULTI_SESS:
                dialog.select_session(self, self.console, acc_sessions, self.api)
            # 单用户, 默认加载第一个会话档
            else:
                ck = ck2dict(acc_sessions[0].ck)
                self.api.session.ck_load(ck)
                if not self.api.accinfo():
                    self.console.print("[red]会话失效, 尝试重新登录")
                    if not dialog.relogin(self.console, acc_sessions[0], self.api):
                        self.console.print("[red]重登失败，账号或密码错误")
                        sys.exit()
        # 会话存档为空
        else:
            self.console.print("[yellow]会话存档为空, 请登录账号")
            dialog.login(self, self.console, self.api)
        self.logger.info("\n-----*任务开始执行*-----")
        self.logger.info(f"Ver. {__version__}")
        dialog.accinfo(self.console, self.api)
        try:
            # 拉取预先上传的人脸图片
            if config.FETCH_UPLOADED_FACE is True:
                if face_url := self.api.fetch_face():
                    self.api.save_face(face_url, config.FACE_PATH)

            # 拉取该账号下所学的课程
            classes = self.api.fetch_classes()
            # 课程选择交互
            command = dialog.select_class(self, self.console, classes)
            # 注册验证码 人脸 回调
            self.api.session.reg_captcha_after(self.on_captcha_after)
            self.api.session.reg_captcha_before(self.on_captcha_before)
            self.api.session.reg_face_after(self.on_face_detection_after)
            self.api.session.reg_face_before(self.on_face_detection_before)
            # 执行课程任务
            for task_obj in ClassSelector(command, classes):
                # 章节容器 执行章节任务
                if isinstance(task_obj, ChapterContainer):
                    self.fuck_task_worker(task_obj)

                # 考试对象 执行考试任务
                elif isinstance(task_obj, ExamDto):
                    self.fuck_exam_worker(task_obj)

                # 考试列表 进入二级选择交互
                elif isinstance(task_obj, list):
                    exam, export = dialog.select_exam(self.console, task_obj, self.api)
                    self.fuck_exam_worker(exam, export)

        except Exception as err:
            # 任务异常
            self.console.print_exception(show_locals=False)
            self.logger.error("\n-----*程序运行异常退出*-----", exc_info=True)
            if isinstance(err, json.JSONDecodeError):
                self.console.print("[red]JSON 解析失败, 可能为账号 ck 失效, 请重新登录该账号 (序号+r)")
            else:
                self.console.print("[bold red]程序运行出现错误, 请截图保存并附上 log 文件在 issue 提交")
        except KeyboardInterrupt:
            # 手动中断程序运行
            self.console.print("[yellow]手动中断程序运行")
        else:
            # 任务执行完毕
            self.logger.info("\n-----*任务执行完毕, 程序退出*-----")
            self.console.print("[green]任务已完成, 程序退出")

    def exit(self):
        """
        退出
        Returns:

        """
        sys.exit()


class GarbageCollector(threading.Thread):
    def __init__(self, multitasking, check_interval=5):
        super().__init__(name="GarbageCollector")
        self.multitasking = multitasking
        self.check_interval = check_interval
        self.RUNFlAG = True

    def run(self):
        while self.RUNFlAG:
            for task in self.multitasking.tasks:
                if check_timeout(task):
                    print(f"进程 {task.process_id} 已超时，已标记为死亡")
                    task.alive = False
                    self.multitasking.tasks.remove(task)  # 从任务列表中移除
                    # task.exit()
            time.sleep(self.check_interval)


class Multitasking:
    def __init__(self):
        self.tasks = []

        # 启动垃圾回收线程
        self.gc = GarbageCollector(self)
        self.gc.start()

    @staticmethod
    def threading_fun(process):
        process.run()

    def create_process(self, process_id):
        process = ChaoxingProcess(process_id=process_id, web_mode=True)
        self.tasks.append(process)
        thread = threading.Thread(target=self.threading_fun, args=(process,), name='Thread-' + process_id)
        thread.start()

    def get_process(self, process_id):
        for task in self.tasks:
            # print(task.process_id, process_id)
            if task.process_id == process_id:
                return task
        return None

    def get_process_id(self, phone):
        for task in self.tasks:
            if task.phone == phone:
                return task.process_id
        return None
