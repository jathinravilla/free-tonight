"""SQLite storage for profiles. One row per person; rows are never joined or shown to anyone else."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .profile import Profile


class ProfileStore:
    def __init__(self, path: str | Path = "freetonight.db"):
        path = Path(path)
        if str(path) != ":memory:":
            path.touch(mode=0o600, exist_ok=True)  # private profiles: owner-only file
            os.chmod(path, 0o600)
        self.db = sqlite3.connect(str(path))
        self.db.execute("CREATE TABLE IF NOT EXISTS profiles (user_id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        self.db.commit()

    def get(self, user_id: str) -> Profile | None:
        row = self.db.execute("SELECT data FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        return Profile.from_dict(json.loads(row[0])) if row else None

    def put(self, profile: Profile) -> None:
        if not profile.user_id:
            raise ValueError("Profile needs a user_id")
        self.db.execute("INSERT INTO profiles (user_id, data) VALUES (?, ?) "
                        "ON CONFLICT(user_id) DO UPDATE SET data = excluded.data",
                        (profile.user_id, profile.to_json()))
        self.db.commit()

    def delete(self, user_id: str) -> bool:
        cur = self.db.execute("DELETE FROM profiles WHERE user_id = ?", (user_id,))
        self.db.commit()
        return cur.rowcount > 0

    def close(self) -> None:
        self.db.close()
