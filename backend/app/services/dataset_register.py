"""数据集目录的安全解析与注册。

平台原本只能看到「启动时扫出来的 /datasets 子目录」，用户加新数据必须找管理员，
企业里不可行。这里开放「注册已有目录」，因此**路径校验是本模块最重要的部分**：

* 允许的根目录默认只有数据集根（``datasets_mount_path``）。配置
  ``PLATFORM_DATASET_ALLOWED_ROOTS`` 可以追加（例如宿主机原始数据目录 ``/data/raw``）。
* **配置为空时只接受数据集根的直接子目录**——这正是目录扫描能发现的数据集，
  所以「默认配置」不会比「扫描」多放出任何一个文件系统位置。
* 所有比较都在 ``Path.resolve()`` 之后做，``..`` 与软链接逃逸都过不去。
  写法与 ``services/checkpoint_retention.py::safe_job_dir`` 一致：
  宁可误拒，也不做「尽力而为」。
* 显式拒绝训练产物目录与中间态目录（``checkpoints`` / ``backups`` / ``.tmp``），
  这类目录里也可能残留 ``meta/info.json``，注册进来只会让人误以为能训练。
* 数据集 id 必须是单段、无空白、无控制字符的名字：它会出现在 URL 与 SQL 里，
  也会被拼到扫描根目录下。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..config import Settings
from ..datasets.scanner import detect_format

# 数据集 id 的最大长度。与 models.DatasetRegisterRequest 保持一致。
MAX_DATASET_ID_LENGTH = 100
# 与 datasets.scanner.list_dataset_dirs 跳过的后缀保持一致：临时目录不是数据集
TEMP_SUFFIXES = (".tmp", ".partial")
# 绝对不允许出现在路径里的元素：训练产物、数据库备份。
# 它们和真实数据集在同一个 /data/vla-platform 下，误注册代价很高。
FORBIDDEN_PARTS = frozenset({"checkpoints", "backups"})


class UnsafeDatasetPath(ValueError):
    """路径或 id 不安全，拒绝注册。"""


@dataclass(frozen=True)
class ResolvedDatasetDir:
    dataset_id: str
    path: Path


def validate_dataset_id(dataset_id: str) -> str:
    """数据集 id 必须是单段目录名。"""
    if not dataset_id or not dataset_id.strip():
        raise UnsafeDatasetPath("数据集 id 不能为空")
    if dataset_id != dataset_id.strip():
        raise UnsafeDatasetPath(f"数据集 id 两端有空白：{dataset_id!r}")
    if len(dataset_id) > MAX_DATASET_ID_LENGTH:
        raise UnsafeDatasetPath(f"数据集 id 不能超过 {MAX_DATASET_ID_LENGTH} 个字符")
    for bad in ("/", "\\", "*", "?", "[", "]", "\0"):
        if bad in dataset_id:
            raise UnsafeDatasetPath(f"数据集 id 不能包含 {bad!r}：{dataset_id!r}")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in dataset_id):
        raise UnsafeDatasetPath(f"数据集 id 不能包含控制字符：{dataset_id!r}")
    if dataset_id in (".", ".."):
        raise UnsafeDatasetPath(f"数据集 id 不能是 {dataset_id!r}")
    if dataset_id.startswith("."):
        # 扫描器跳过隐藏目录，注册进来也永远扫不到，不如直接说清楚。
        raise UnsafeDatasetPath(f"数据集 id 不能以 . 开头：{dataset_id!r}")
    if dataset_id.endswith(TEMP_SUFFIXES):
        raise UnsafeDatasetPath(f"数据集 id 不能以 {TEMP_SUFFIXES} 结尾：{dataset_id!r}")
    return dataset_id


def datasets_root(settings: Settings) -> Path:
    return Path(settings.datasets_mount_path).resolve()


def allowed_roots(settings: Settings) -> list[Path]:
    """允许注册的根目录：数据集根 + ``PLATFORM_DATASET_ALLOWED_ROOTS`` 里合法的项。"""
    roots: list[Path] = [datasets_root(settings)]
    for raw in settings.dataset_allowed_roots:
        if not raw.strip():
            continue
        resolved = Path(raw.strip()).resolve()
        if resolved not in roots:
            roots.append(resolved)
    return roots


def to_container_path(settings: Settings, raw: str) -> str:
    """把宿主机路径翻译成容器内路径。

    为什么需要：用户在宿主机上看到的路径是 /data/vla-platform/datasets/xxx，
    而容器把它挂在 /datasets/xxx。两者都对，但只有容器路径能被校验。
    让人去换算挂载点是没道理的，这里自动识别宿主机前缀并做替换。
    """
    host_root = Path(settings.dataset_host_path or "").as_posix()
    mount_root = Path(settings.datasets_mount_path or "").as_posix()
    if not host_root or not mount_root or host_root == mount_root:
        return raw

    text = raw.strip()
    # 只处理绝对路径；相对路径交回原有校验流程报错
    if not text.startswith("/"):
        return raw
    normalized = Path(text).as_posix()
    if normalized == host_root:
        return mount_root
    prefix = host_root.rstrip("/") + "/"
    if normalized.startswith(prefix):
        return mount_root.rstrip("/") + "/" + normalized[len(prefix):]
    return raw


def resolve_dataset_dir(
    settings: Settings,
    path: str,
    dataset_id: str | None = None,
) -> ResolvedDatasetDir:
    """把用户给的路径解析成允许注册的数据集目录。任何可疑输入都抛 UnsafeDatasetPath。

    校验顺序刻意「先便宜后昂贵」：格式 → 越界 → 存在性 → 目录 → 禁区分支，
    这样路径穿越不会因为先 stat 而暴露目标是否存在。
    """
    if not path or not path.strip():
        raise UnsafeDatasetPath("path 不能为空")
    raw = path.strip()
    if "\0" in raw:
        raise UnsafeDatasetPath("path 不能包含 NUL 字符")

    path = to_container_path(settings, path)
    root = datasets_root(settings)
    if not root.is_dir():
        # 根目录没挂上时，任何路径都不该被当成合法数据集。
        raise UnsafeDatasetPath(f"数据集根目录不存在：{root}")
    # resolve() 会消掉 .. 与软链接；不存在也没关系，后面单独判断存在性。
    target = Path(raw).resolve()

    # --- 越界检查（防 ../../etc、防软链接跳出去） -------------------------
    granted = [item for item in allowed_roots(settings) if item != root]
    inside_root = target.is_relative_to(root)
    inside_granted = any(target.is_relative_to(item) for item in granted)
    if not inside_root and not inside_granted:
        raise UnsafeDatasetPath(f"路径不在允许的数据集根目录之下：{target}")

    # --- 禁区 -------------------------------------------------------------
    if any(part in FORBIDDEN_PARTS for part in target.parts):
        raise UnsafeDatasetPath(f"不允许注册训练产物或备份目录：{target}")

    # --- 必须是子目录，不能是根本身 ----------------------------------------
    if target == root or target in granted:
        raise UnsafeDatasetPath(f"不能把根目录本身注册成数据集：{target}")
    if target.name.startswith(".") or target.name.endswith(TEMP_SUFFIXES):
        raise UnsafeDatasetPath(f"不能注册隐藏目录或临时目录：{target}")

    # 只有数据集根权限时，只接受直接子目录——与 list_dataset_dirs 的发现范围一致。
    # 显式授权的根（例如宿主机原始数据目录）允许更深，那是用户自己的布局。
    if inside_root and not inside_granted and target.parent != root:
        raise UnsafeDatasetPath(
            f"只允许注册 {root} 的直接子目录（或 PLATFORM_DATASET_ALLOWED_ROOTS 之下）：{target}"
        )

    # --- 存在性与类型 ------------------------------------------------------
    if not target.exists():
        raise UnsafeDatasetPath(f"路径不存在：{target}")
    if not target.is_dir():
        raise UnsafeDatasetPath(f"路径不是目录：{target}")

    final_id = validate_dataset_id(dataset_id if dataset_id is not None else target.name)
    return ResolvedDatasetDir(dataset_id=final_id, path=target)


def check_lerobot_format(path: Path) -> str:
    """返回识别出的 LeRobot 版本；不是 LeRobot 数据集时抛 UnsafeDatasetPath。

    判据与扫描器一致（见 ``scanner.detect_format``）：``meta/info.json`` 存在且
    带 CodeBase Version。不在这里重复实现解析，避免两处判定标准漂移。
    """
    if not (path / "meta" / "info.json").is_file():
        raise UnsafeDatasetPath(f"目录里没有 meta/info.json，不是有效的 LeRobot 数据集：{path}")
    version = detect_format(path)
    if version == "unknown":
        raise UnsafeDatasetPath(
            f"meta/info.json 无法识别（缺少或非法的 codebase_version），不是有效的 LeRobot 数据集：{path}"
        )
    return version