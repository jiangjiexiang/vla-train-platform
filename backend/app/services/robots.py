"""机器人配置存储（SQLite）：支持管理员动态增删改机器人并自动生成训练配置文件。"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import yaml

from .errors import ServiceError
from .sessions import utc_now

ROBOT_CONFIGS_DIR = Path(os.environ.get("PLATFORM_CHECKPOINT_MOUNT_PATH", "/checkpoints")) / "_robot_configs"

DEFAULT_ROBOTS: list[dict[str, Any]] = [
    {
        "id": "turtle",
        "name": "Robam / Turtle",
        "description": "三目 faceImg / leftImg / rightImg",
        "camera_keywords": ["faceImg"],
        "camera_top": "observation.images.faceImg",
        "camera_left": "observation.images.leftImg",
        "camera_right": "observation.images.rightImg",
        "state_dim": 20,
        "action_dim": 20,
        "param_defaults": {
            "openpi_config_name": "pi05_turtle",
            "data_name": "robam_x2robot",
            "modality_config_path": "/opt/GR00T-N1.7-3B/GR00T_N1.7/x2robot_config.py",
        },
        "sort_order": 10,
    },
    {
        "id": "h01",
        "name": "H01 双臂",
        "description": "鱼眼 + 双腕相机",
        "camera_keywords": ["fisheye", "wrist"],
        "camera_top": "observation.images.cam_fisheye_front",
        "camera_left": "observation.images.cam_left_wrist_down",
        "camera_right": "observation.images.cam_right_wrist_down",
        "state_dim": 20,
        "action_dim": 20,
        "param_defaults": {"openpi_config_name": "pi05_h01"},
        "sort_order": 20,
    },
    {
        "id": "x2robot",
        "name": "X2Robot",
        "description": "20 维双臂",
        "camera_keywords": ["faceImg"],
        "camera_top": "observation.images.faceImg",
        "camera_left": "observation.images.leftImg",
        "camera_right": "observation.images.rightImg",
        "state_dim": 20,
        "action_dim": 20,
        "param_defaults": {
            "openpi_config_name": "pi05_x2robot",
            "data_name": "robam_x2robot",
            "modality_config_path": "/opt/GR00T-N1.7-3B/GR00T_N1.7/x2robot_config.py",
        },
        "sort_order": 30,
    },
    {
        "id": "so101",
        "name": "SO-100 / SO-101",
        "description": "单臂桌面机械臂",
        "camera_keywords": [],
        "camera_top": "observation.images.top",
        "camera_left": "observation.images.wrist",
        "camera_right": "",
        "state_dim": 6,
        "action_dim": 6,
        "param_defaults": {
            "openpi_config_name": "pi05_so101_joint",
            "modality_config_path": "/opt/GR00T-N1.7-3B/Isaac-GR00T/examples/mask-guided-background-suppression/so101_config.py",
        },
        "sort_order": 40,
    },
    {
        "id": "sim",
        "name": "仿真环境",
        "description": "LIBERO / ManiSkill / RoboTwin",
        "camera_keywords": [],
        "camera_top": "observation.images.cam_high",
        "camera_left": "observation.images.cam_left_wrist",
        "camera_right": "observation.images.cam_right_wrist",
        "state_dim": 14,
        "action_dim": 14,
        "param_defaults": {
            "openpi_config_name": "pi05_libero",
            "data_name": "robotwin",
        },
        "sort_order": 50,
    },
]


def _write_robot_files(robot: dict[str, Any]) -> None:
    """自动生成 LingBot-VLA 2.0 与 GR00T N1.7 可直接使用的机器人配置文件。"""
    try:
        ROBOT_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
        rid = robot["id"]
        sdim = int(robot.get("state_dim") or 20)
        adim = int(robot.get("action_dim") or 20)
        arm_s = min(14, sdim)
        arm_a = min(14, adim)
        images = []
        if robot.get("camera_top"):
            images.append({"observation.images.camera_top": {"origin_keys": robot["camera_top"]}})
        if robot.get("camera_left"):
            images.append({"observation.images.camera_wrist_left": {"origin_keys": robot["camera_left"]}})
        if robot.get("camera_right"):
            images.append({"observation.images.camera_wrist_right": {"origin_keys": robot["camera_right"]}})
        lingbot_doc = {
            "states": [
                {"observation.state.arm.position": {"origin_keys": [{"observation.state": {"start": 0, "end": arm_s}}]}},
                *(
                    [{"observation.state.effector.position": {"origin_keys": [{"observation.state": {"start": arm_s, "end": sdim}}]}}]
                    if sdim > arm_s
                    else []
                ),
            ],
            "actions": [
                {"action.arm.position": {"origin_keys": [{"action": {"start": 0, "end": arm_a}}], "subtract_state": False}},
                *(
                    [{"action.effector.position": {"origin_keys": [{"action": {"start": arm_a, "end": adim}}], "subtract_state": False}}]
                    if adim > arm_a
                    else []
                ),
            ],
            "images": images,
            "norm_stats": "assets/norm_stats/robotwin.json",
        }
        (ROBOT_CONFIGS_DIR / f"{rid}.yaml").write_text(
            yaml.safe_dump(lingbot_doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
    except OSError:
        pass


class RobotStore:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS robots (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    camera_keywords_json TEXT NOT NULL DEFAULT '[]',
                    param_defaults_json TEXT NOT NULL DEFAULT '{}',
                    camera_top TEXT NOT NULL DEFAULT 'observation.images.faceImg',
                    camera_left TEXT NOT NULL DEFAULT 'observation.images.leftImg',
                    camera_right TEXT NOT NULL DEFAULT 'observation.images.rightImg',
                    state_dim INTEGER NOT NULL DEFAULT 20,
                    action_dim INTEGER NOT NULL DEFAULT 20,
                    sort_order INTEGER NOT NULL DEFAULT 100,
                    updated_at TEXT NOT NULL
                );
                """
            )
            cols = {r[1] for r in connection.execute("PRAGMA table_info(robots)").fetchall()}
            for col, ddl in (
                ("camera_top", "TEXT NOT NULL DEFAULT 'observation.images.faceImg'"),
                ("camera_left", "TEXT NOT NULL DEFAULT 'observation.images.leftImg'"),
                ("camera_right", "TEXT NOT NULL DEFAULT 'observation.images.rightImg'"),
                ("state_dim", "INTEGER NOT NULL DEFAULT 20"),
                ("action_dim", "INTEGER NOT NULL DEFAULT 20"),
            ):
                if col not in cols:
                    connection.execute(f"ALTER TABLE robots ADD COLUMN {col} {ddl}")
            count = connection.execute("SELECT COUNT(*) FROM robots").fetchone()[0]
            if count == 0:
                now = utc_now().isoformat()
                for item in DEFAULT_ROBOTS:
                    connection.execute(
                        """
                        INSERT INTO robots (id, name, description, camera_keywords_json, param_defaults_json, camera_top, camera_left, camera_right, state_dim, action_dim, sort_order, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item["id"],
                            item["name"],
                            item["description"],
                            json.dumps(item["camera_keywords"], ensure_ascii=False),
                            json.dumps(item["param_defaults"], ensure_ascii=False),
                            item["camera_top"],
                            item["camera_left"],
                            item["camera_right"],
                            item["state_dim"],
                            item["action_dim"],
                            item["sort_order"],
                            now,
                        ),
                    )
        for r in self.list_all():
            _write_robot_files(r)

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "camera_keywords": json.loads(row["camera_keywords_json"] or "[]"),
            "param_defaults": json.loads(row["param_defaults_json"] or "{}"),
            "camera_top": row["camera_top"],
            "camera_left": row["camera_left"],
            "camera_right": row["camera_right"],
            "state_dim": row["state_dim"],
            "action_dim": row["action_dim"],
            "sort_order": row["sort_order"],
            "updated_at": row["updated_at"],
        }

    def list_all(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM robots ORDER BY sort_order ASC, id ASC"
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM robots WHERE id = ?", (payload["id"],)
            ).fetchone()
            if existing is not None:
                raise ServiceError(409, "robot_exists", f"机器人 {payload['id']} 已存在")
            connection.execute(
                """
                INSERT INTO robots (id, name, description, camera_keywords_json, param_defaults_json, camera_top, camera_left, camera_right, state_dim, action_dim, sort_order, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload["id"],
                    payload["name"],
                    payload.get("description", ""),
                    json.dumps(payload.get("camera_keywords") or [], ensure_ascii=False),
                    json.dumps(payload.get("param_defaults") or {}, ensure_ascii=False),
                    payload.get("camera_top", "observation.images.faceImg"),
                    payload.get("camera_left", "observation.images.leftImg"),
                    payload.get("camera_right", "observation.images.rightImg"),
                    int(payload.get("state_dim", 20)),
                    int(payload.get("action_dim", 20)),
                    int(payload.get("sort_order", 100)),
                    now,
                ),
            )
            row = connection.execute("SELECT * FROM robots WHERE id = ?", (payload["id"],)).fetchone()
        created = self._row_to_dict(row)
        _write_robot_files(created)
        return created

    def update(self, robot_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = utc_now().isoformat()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM robots WHERE id = ?", (robot_id,)).fetchone()
            if row is None:
                raise ServiceError(404, "robot_not_found", f"机器人 {robot_id} 不存在")
            current = self._row_to_dict(row)
            name = payload.get("name", current["name"])
            description = payload.get("description", current["description"])
            camera_keywords = payload.get("camera_keywords", current["camera_keywords"])
            param_defaults = payload.get("param_defaults", current["param_defaults"])
            camera_top = payload.get("camera_top", current["camera_top"])
            camera_left = payload.get("camera_left", current["camera_left"])
            camera_right = payload.get("camera_right", current["camera_right"])
            state_dim = int(payload.get("state_dim", current["state_dim"]))
            action_dim = int(payload.get("action_dim", current["action_dim"]))
            sort_order = int(payload.get("sort_order", current["sort_order"]))
            connection.execute(
                """
                UPDATE robots
                SET name = ?, description = ?, camera_keywords_json = ?, param_defaults_json = ?, camera_top = ?, camera_left = ?, camera_right = ?, state_dim = ?, action_dim = ?, sort_order = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name,
                    description,
                    json.dumps(camera_keywords, ensure_ascii=False),
                    json.dumps(param_defaults, ensure_ascii=False),
                    camera_top,
                    camera_left,
                    camera_right,
                    state_dim,
                    action_dim,
                    sort_order,
                    now,
                    robot_id,
                ),
            )
            updated_row = connection.execute("SELECT * FROM robots WHERE id = ?", (robot_id,)).fetchone()
        updated = self._row_to_dict(updated_row)
        _write_robot_files(updated)
        return updated

    def delete(self, robot_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM robots WHERE id = ?", (robot_id,))
            if cursor.rowcount == 0:
                raise ServiceError(404, "robot_not_found", f"机器人 {robot_id} 不存在")
        return {"deleted": robot_id}
