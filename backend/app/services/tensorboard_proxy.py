"""TensorBoard 原生界面集成。

平台原本只有一个从 events 文件解析出 JSON 的简版曲线接口
（``services/tensorboard.py``）。功能上够用，但用户想要的是
TensorBoard 官方的界面——平滑滑块、多曲线对比、直方图、导出，
这些自己实现代价太大，也不该重新造。

于是改成在 Web 容器里起一个 tensorboard 子进程，用反向代理暴露出去，
前端用 iframe 嵌进任务详情页。

这里有三个设计决定，都是踩过坑之后定的：

1. **一个进程服务所有任务**，而不是一个任务一个进程。
   TensorBoard 的 --logdir 指向 checkpoints 根目录时，每个子目录会
   自动变成一个 run，界面上就是一组 tab。这样进程数恒定，不会因为
   任务多而把内存吃光（单进程常驻约 100~200MB）。

2. **鉴权走 Cookie 而不是 Bearer**。
   iframe 发起的请求不会带 Authorization 头（那是 XHR/fetch 才管的事），
   所以 Bearer token 在这里行不通。登录时额外种一个 HttpOnly Cookie，
   值就是现有的会话令牌本身——反代只做 sessions.lookup()，
   不需要引入新的签名密钥，会话被 revoke 后 Cookie 立刻失效。

3. **反代必须先剥掉客户端的 Authorization**。
   TensorBoard 自己不校验任何东西，靠外层挡。如果反代把 Authorization
   透传下去，将来 TensorBoard 加了什么认证逻辑就会踩出奇怪的坑。

失败降级：tensorboard 起不来（端口占用、崩溃、依赖缺失）时，
整个平台照常工作，只是 iframe 里显示错误提示。训练和排队绝不受影响。
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# TensorBoard 的资源路径是绝对路径，必须告诉它挂在子路径下，
# 否则 CSS/JS 全部 404（页面白屏）。
PATH_PREFIX = "/tensorboard"

# 只在回环地址上监听：外部无法直连，必须经过平台反代，鉴权才不会被绕过。
LISTEN_HOST = "127.0.0.1"
DEFAULT_PORT = 6106

# 启动等待时间。tensorboard 加载 events 需要一点时间，
# 但也不能无限等——起不来就是起不来，不能拖住整个应用启动。
STARTUP_TIMEOUT_SECONDS = 20.0


@dataclass
class TensorboardProcess:
    """管理 tensorboard 子进程的生命周期。"""

    logdir: str
    port: int = DEFAULT_PORT
    process: subprocess.Popen[bytes] | None = None
    _reader: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        return f"http://{LISTEN_HOST}:{self.port}"

    def start(self) -> bool:
        """启动 tensorboard。返回是否成功。

        失败不抛异常：这是一个附加功能，它挂了不该让平台起不来。
        """
        if self._port_in_use():
            logger.warning(
                "TensorBoard 端口 %s 已被占用，跳过启动。"
                "如果占用者是上次残留的 tensorboard，界面仍可能可用。",
                self.port,
            )
            return self._healthy()

        command = [
            sys.executable,
            "-m",
            "tensorboard.main",
            "--logdir",
            self.logdir,
            "--host",
            LISTEN_HOST,
            "--port",
            str(self.port),
            "--path_prefix",
            PATH_PREFIX,
            # 训练进程边写边读时，TensorBoard 默认只认单个文件，
            # 开了这个才会扫描新出现的 events 文件。
            "--reload_multifile",
            "true",
            # 默认只采样 1000 个点，长训练看不到全貌。
            "--samples_per_plugin",
            "scalars=100000",
        ]
        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            logger.warning("TensorBoard 启动失败：%s", exc)
            return False

        # 把子进程输出转进平台日志，出问题时才有线索。
        def pump() -> None:
            stream = self.process.stdout if self.process else None
            if stream is None:
                return
            for raw in iter(stream.readline, b""):
                logger.info("[tensorboard] %s", raw.decode("utf-8", "replace").rstrip())

        self._reader = threading.Thread(target=pump, name="tensorboard-log", daemon=True)
        self._reader.start()

        deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                logger.warning("TensorBoard 进程提前退出，返回码 %s", self.process.returncode)
                return False
            if self._healthy():
                logger.info("TensorBoard 已启动：%s%s", self.base_url, PATH_PREFIX)
                return True
            time.sleep(0.5)

        logger.warning("TensorBoard 在 %.0f 秒内没有就绪，放弃等待", STARTUP_TIMEOUT_SECONDS)
        return False

    def stop(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # 优雅停止超时就强杀，不能让它拖住进程退出。
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def _port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(1)
            return probe.connect_ex((LISTEN_HOST, self.port)) == 0

    def _healthy(self) -> bool:
        """探一下 TensorBoard 是否真的能响应，而不只是端口开了。"""
        url = f"{self.base_url}{PATH_PREFIX}/data/plugins_listing"
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, OSError):
            return False


def proxy_request(
    base_url: str,
    path: str,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float = 30.0,
) -> tuple[int, list[tuple[str, str]], bytes]:
    """把请求转发给 TensorBoard，返回 (状态码, 响应头, 响应体)。

    用标准库 urllib 而不是 httpx/requests：这两个都要往镜像里多装依赖，
    而这个功能只在内网回环上跑，够用就行。
    """
    url = f"{base_url}{PATH_PREFIX}/{path.lstrip('/')}"
    request = urllib.request.Request(url, data=body, method=method)

    # 只透传和内容协商相关的头。Authorization 刻意不传——
    # TensorBoard 不做鉴权，透传下去只会带来后续维护的困惑。
    for name in ("content-type", "accept", "accept-encoding", "range", "if-none-match"):
        if name in headers:
            request.add_header(name, headers[name])

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, list(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, list(exc.headers.items()) if exc.headers else [], exc.read()
    except (urllib.error.URLError, OSError) as exc:
        logger.warning("TensorBoard 反代失败：%s", exc)
        return 502, [], b""


def read_text(payload: bytes | None) -> str:
    if not payload:
        return ""
    if isinstance(payload, bytes):
        return payload.decode("utf-8", "replace")
    return str(payload)
