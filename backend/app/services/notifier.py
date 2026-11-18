"""任务终态通知：训练成功/失败时推送到钉钉、企业微信或通用 webhook。

为什么单独成模块：
- 训练任务动辄跑几小时，没人会盯着页面。失败时尤其如此——等发现时
  半天已经过去了。
- 但通知只是「锦上添花」：webhook 挂了、超时了、返回非 200，
  都绝不能影响训练任务本身，也不能让轮询线程死掉。

因此这里所有发送都走 `_post()` 一个入口，它自己吞掉所有异常并返回
``(ok, detail)``，调用方不需要 try/except。

只用标准库 urllib：requirements.txt 里没有 requests，为一个可选的
通知功能引入新依赖不值得（也会让离线构建镜像变复杂）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote_plus, urlparse

logger = logging.getLogger("vla-platform")

# 只发这两种终态。CANCELLED 是用户主动取消的，发通知纯属噪音。
NOTIFIABLE_EVENTS = ("RUNNING", "SUCCEEDED", "FAILED")

_EVENT_TITLES = {
    "RUNNING": "训练任务开始",
    "SUCCEEDED": "训练任务成功",
    "FAILED": "训练任务失败",
}


class Notifier:
    """按配置向一个或多个 webhook 发送任务终态通知。

    构造时从 settings 读 webhook；三个都为空时 ``enabled`` 为 False，
    此时 ``notify_job()`` 直接返回空列表，不会发起任何网络请求——
    这正是「没配 webhook 就完全无副作用」的保证。
    """

    def __init__(self, settings: Any, *, opener: Any = None) -> None:
        self.dingtalk_webhook = (getattr(settings, "dingtalk_webhook", "") or "").strip()
        self.dingtalk_secret = (getattr(settings, "dingtalk_secret", "") or "").strip()
        self.wecom_webhook = (getattr(settings, "wecom_webhook", "") or "").strip()
        self.generic_webhook = (getattr(settings, "generic_webhook", "") or "").strip()
        self.timeout = float(getattr(settings, "notify_timeout_seconds", 5.0) or 5.0)
        # 允许注入 opener 方便测试；生产环境用默认的 urlopen。
        self._opener = opener or urllib.request.urlopen

    @property
    def enabled(self) -> bool:
        return bool(self.dingtalk_webhook or self.wecom_webhook or self.generic_webhook)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def notify_job(self, job: dict[str, Any], event: str) -> list[dict[str, Any]]:
        """发送一条任务通知，返回每个渠道的结果。

        永远不会抛异常：任何渠道失败都只是结果里 ``ok=False``。
        返回值方便测试断言，生产调用方可以忽略。
        """
        if not self.enabled:
            return []
        normalized = str(event or "").upper()
        if normalized not in NOTIFIABLE_EVENTS:
            # 取消、运行中状态一律不发。
            return []
        results: list[dict[str, Any]] = []
        channels = (
            ("dingtalk", self.dingtalk_webhook, self._send_dingtalk),
            ("wecom", self.wecom_webhook, self._send_wecom),
            ("generic", self.generic_webhook, self._send_generic),
        )
        for name, url, sender in channels:
            if not url:
                continue
            try:
                result = sender(url, job, normalized)
            except Exception as exc:  # noqa: BLE001 - 通知失败绝不能往上抛
                result = {"channel": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}
            result.setdefault("channel", name)
            results.append(result)
            if not result.get("ok"):
                logger.warning("notify via %s failed for %s: %s", name, job.get("name"), result.get("detail"))
        return results

    # ------------------------------------------------------------------
    # 三种渠道
    # ------------------------------------------------------------------
    @staticmethod
    def _sign_dingtalk_url(url: str, secret: str, *, timestamp_ms: int | None = None) -> str:
        ts = str(timestamp_ms if timestamp_ms is not None else round(time.time() * 1000))
        string_to_sign = f"{ts}\n{secret}"
        hmac_code = hmac.new(
            secret.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        sign = quote_plus(base64.b64encode(hmac_code))
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}timestamp={ts}&sign={sign}"

    def _send_dingtalk(self, url: str, job: dict[str, Any], event: str) -> dict[str, Any]:
        """钉钉群机器人 markdown 消息，支持可选 SEC 加签。"""
        title = self._title(job, event)
        target_url = self._sign_dingtalk_url(url, self.dingtalk_secret) if self.dingtalk_secret else url
        return self._post(
            "dingtalk",
            target_url,
            {"msgtype": "markdown", "markdown": {"title": title, "text": self._markdown(job, event)}},
        )

    def _send_wecom(self, url: str, job: dict[str, Any], event: str) -> dict[str, Any]:
        """企业微信群机器人 markdown 消息。

        注意与钉钉的差别：企微的 markdown 对象只有 ``content``，
        没有 ``title``；多塞一个 title 字段企微会直接报错。
        """
        return self._post(
            "wecom",
            url,
            {"msgtype": "markdown", "markdown": {"content": self._markdown(job, event)}},
        )

    def _send_generic(self, url: str, job: dict[str, Any], event: str) -> dict[str, Any]:
        """通用 webhook：结构化 JSON，交给接收端自己排版。

        字段固定为 event/job 两块，接收端（自建通知服务、Slack 代理、
        自动化脚本）按需取用。``text`` 是纯文本摘要，方便直接人读。
        """
        payload = {
            "event": event,
            "title": self._title(job, event),
            "text": self._plain_text(job, event),
            "job": {
                "name": job.get("name"),
                "status": event,
                "owner": job.get("owner") or "",
                "image": job.get("image") or "",
                "queue": job.get("queue") or "",
                "gpus": job.get("gpus"),
                "created_at": job.get("created_at"),
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
                "duration_seconds": job.get("duration_seconds"),
                "message": job.get("message") or "",
                "checkpoint_dir": job.get("checkpoint_dir") or "",
            },
        }
        return self._post("generic", url, payload)

    # ------------------------------------------------------------------
    # 底层发送
    # ------------------------------------------------------------------
    def _post(self, channel: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST 一个 JSON，返回 ``{"channel", "ok", "detail"}``，绝不抛异常。

        成功的判断不只看 HTTP 200：钉钉和企微在业务错误（token 失效、
        关键词不匹配、限流）时也返回 200，真正的结果在
        ``{"errcode": 0}`` 里。只检查状态码会把「其实没发出去」当成成功，
        然后把这个任务标记为已通知，用户永远收不到。
        """
        host = urlparse(url).hostname or ""
        if not host:
            return {"channel": channel, "ok": False, "detail": "invalid webhook url"}
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                status = getattr(response, "status", None) or getattr(response, "code", None) or 0
                body = response.read()
        except urllib.error.HTTPError as exc:
            # 非 2xx：读出响应体便于排查（钉钉限流会在这里返回 4xx）。
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                detail = ""
            return {"channel": channel, "ok": False, "detail": f"HTTP {exc.code} {detail}".strip()}
        except Exception as exc:  # noqa: BLE001 - 超时/断网/DNS 失败都在这里
            return {"channel": channel, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}

        if not (200 <= int(status) < 300):
            return {"channel": channel, "ok": False, "detail": f"HTTP {status}"}
        error = _business_error(body)
        if error is not None:
            return {"channel": channel, "ok": False, "detail": error}
        return {"channel": channel, "ok": True, "detail": "sent"}

    # ------------------------------------------------------------------
    # 文案
    # ------------------------------------------------------------------
    @staticmethod
    def _title(job: dict[str, Any], event: str) -> str:
        return f"{_EVENT_TITLES.get(event, event)}：{job.get('name', '')}"

    def _markdown(self, job: dict[str, Any], event: str) -> str:
        """钉钉/企微共用的 markdown 正文。

        两家都支持 `### 标题` 和列表，正文格式可以完全一致。
        任务名等字段直接来自提交参数，这里不做 markdown 转义：
        通知是内部群消息，可读性优先。
        """
        lines = [
            f"### {self._title(job, event)}",
            f"- **任务名**：{job.get('name', '')}",
            f"- **状态**：{event}",
        ]
        if job.get("owner"):
            lines.append(f"- **提交者**：{job['owner']}")
        if job.get("image"):
            lines.append(f"- **镜像**：{job['image']}")
        if job.get("gpus") is not None:
            queue = f"（队列 {job['queue']}）" if job.get("queue") else ""
            lines.append(f"- **GPU**：{job['gpus']}{queue}")
        if job.get("started_at"):
            lines.append(f"- **开始时间**：{_localize(job['started_at'])}")
        if job.get("finished_at"):
            lines.append(f"- **结束时间**：{_localize(job['finished_at'])}")
        if job.get("duration_seconds") is not None:
            lines.append(f"- **耗时**：{_format_duration(job['duration_seconds'])}")
        if job.get("message"):
            lines.append(f"- **信息**：{job['message']}")
        if job.get("checkpoint_dir"):
            lines.append(f"- **产物目录**：{job['checkpoint_dir']}")
        return "\n".join(lines)

    def _plain_text(self, job: dict[str, Any], event: str) -> str:
        """去掉 markdown 标记的纯文本，给通用 webhook 的接收端直接用。"""
        text = self._markdown(job, event)
        return "\n".join(line.lstrip("#- ").replace("**", "") for line in text.splitlines())


def _business_error(body: bytes) -> str | None:
    """解析钉钉/企微的业务错误码；通用接收端返回非 JSON 时视为成功。

    钉钉、企微都返回 {"errcode": 0, "errmsg": "ok"}。
    取不到 errcode 就当成功（通用 webhook 可能返回任意内容）。
    """
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict) or "errcode" not in parsed:
        return None
    try:
        code = int(parsed.get("errcode") or 0)
    except (TypeError, ValueError):
        return None
    if code == 0:
        return None
    return f"errcode={code} errmsg={parsed.get('errmsg', '')}"


def _localize(value: Any) -> str:
    """把 ISO 时间转成本地时区展示；解析失败就原样返回。"""
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _format_duration(seconds: Any) -> str:
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return str(seconds)
    if total < 0:
        return str(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分 {secs} 秒"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"