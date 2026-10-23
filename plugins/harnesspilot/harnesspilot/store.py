from __future__ import annotations

import json
import hashlib
import hmac
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from .config import canonical, state_root


class Store:
    def __init__(self, root: Path | None = None):
        self.root = root or state_root()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS records (
                    kind TEXT, id TEXT, data TEXT NOT NULL,
                    PRIMARY KEY(kind,id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    time REAL NOT NULL, object_id TEXT NOT NULL,
                    action TEXT NOT NULL, data TEXT NOT NULL
                );
            """)

    def connect(self):
        conn = sqlite3.connect(self.root / "index.sqlite", timeout=30)
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def get(self, kind: str, ident: str) -> dict:
        if not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise ValueError("标识格式无效")
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM records WHERE kind=? AND id=?",
                               (kind, ident)).fetchone()
        if row is None:
            raise ValueError("记录不存在")
        return json.loads(row[0])

    def put(self, kind: str, ident: str, data: dict, *, action: str | None = None):
        with self.connect() as conn:
            conn.execute("INSERT OR REPLACE INTO records VALUES (?,?,?)",
                         (kind, ident, canonical(data)))
            if action:
                conn.execute("INSERT INTO events(time,object_id,action,data) VALUES (?,?,?,?)",
                             (time.time(), ident, action, canonical({"status": data.get("status")})))

    def create(self, kind: str, data: dict, *, ident: str | None = None) -> str:
        ident = ident or uuid.uuid4().hex
        self.put(kind, ident, data, action=f"{kind}.created")
        return ident

    def claim(self, ident: str, plan_sha: str, approval_token: str):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM records WHERE kind='plan' AND id=?",
                               (ident,)).fetchone()
            if row is None:
                raise ValueError("计划不存在")
            plan = json.loads(row[0])
            if plan["status"] != "review" or plan["expires"] < time.time():
                raise ValueError("计划已使用或过期")
            if plan["sha256"] != plan_sha:
                raise ValueError("批准的计划哈希不匹配")
            token_hash = hashlib.sha256(approval_token.encode()).hexdigest()
            if not hmac.compare_digest(plan.get("approval_hash", ""), token_hash):
                raise ValueError("审批令牌无效")
            plan["status"] = "claimed"
            plan["approval_hash"] = ""
            conn.execute("UPDATE records SET data=? WHERE kind='plan' AND id=?",
                         (canonical(plan), ident))
            conn.execute("INSERT INTO events(time,object_id,action,data) VALUES (?,?,?,?)",
                         (time.time(), ident, "plan.claimed", canonical({"sha256": plan_sha})))

    def events(self, ident: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT seq,time,action,data FROM events WHERE object_id=? "
                                "ORDER BY seq", (ident,)).fetchall()
        return [{"seq": n, "time": t, "action": a, "data": json.loads(d)} for n, t, a, d in rows]

    def artifact(self, category: str, ident: str) -> Path:
        if category not in {"jobs", "snapshots", "checkpoints"}:
            raise ValueError("工件类别无效")
        if not re.fullmatch(r"[a-f0-9]{32}", ident):
            raise ValueError("标识格式无效")
        path = self.root / category / ident
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.chmod(0o700)
        except OSError:
            pass
        return path

    def list(self, kind: str) -> list[tuple[str, dict]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT id,data FROM records WHERE kind=? ORDER BY id", (kind,))
            return [(ident, json.loads(data)) for ident, data in rows]

    def export(self, name: str, value: dict, destination: Path):
        if not name or "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("工件名称无效")
        temporary = destination / f".{name}.tmp"
        with temporary.open("w", encoding="utf-8") as target:
            target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, destination / name)
