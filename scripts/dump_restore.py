#!/usr/bin/env python3
"""Restore a verified rootless Gitea ZIP into freshly created volumes."""
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import sys
import zipfile
from manager import parse_ini


def local_path(value, data_root=Path("/var/lib/gitea")):
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or not (
        str(path) == "/var/lib/gitea" or str(path).startswith("/var/lib/gitea/")
    ):
        raise ValueError(f"Restore path is outside the data volume: {value}")
    return Path(data_root) / path.relative_to("/var/lib/gitea")


def restore_dump(archive_path, config_path, data_root=Path("/var/lib/gitea")):
    sections = parse_ini(Path(config_path).read_text())
    data = local_path(sections["server"]["APP_DATA_PATH"], data_root)
    if sections["server"]["APP_DATA_PATH"].rstrip("/") != "/var/lib/gitea":
        raise ValueError("Unsupported rootless data layout")
    repos = local_path(sections["repository"]["ROOT"], data_root)
    if sections["repository"]["ROOT"].rstrip("/") != "/var/lib/gitea/git/repositories":
        raise ValueError("Unsupported rootless repository layout")
    routes = {"repos": repos, "custom": data / "custom",
              "log": local_path(sections.get("log", {}).get("ROOT_PATH", "/var/lib/gitea/log"), data_root)}
    stores = {
        "lfs": local_path(sections.get("lfs", {}).get("PATH", "/var/lib/gitea/lfs"), data_root),
        "attachments": local_path(sections.get("attachment", {}).get("PATH", "/var/lib/gitea/attachments"), data_root),
        "packages": local_path(sections.get("storage.packages", {}).get("PATH", "/var/lib/gitea/packages"), data_root),
    }
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            path = PurePosixPath(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe dump archive member")
            # Reject symbolic links: restore must not follow them outside volumes.
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Symbolic links in dumps require an explicit restore procedure")
            parts = path.parts
            if not parts:
                continue
            if parts[0] == "data":
                if len(parts) > 1 and parts[1] in stores:
                    destination = stores[parts[1]].joinpath(*parts[2:])
                else:
                    destination = data.joinpath(*parts[1:])
            elif parts[0] in routes:
                destination = routes[parts[0]].joinpath(*parts[1:])
            else:
                # app.ini comes from the complete verified config volume.
                continue
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
            mode = (member.external_attr >> 16) & 0o777
            destination.chmod(mode or 0o600)
        database = local_path(sections["database"]["PATH"], data_root)
        database.parent.mkdir(parents=True, exist_ok=True)
        # Official dumps include the native SQLite file when it lies under data.
        # Prefer it; import the SQL export only if no native database was included.
        if not database.exists():
            with sqlite3.connect(database) as connection:
                connection.executescript(archive.read("gitea-db.sql").decode("utf-8"))
    with sqlite3.connect(database) as connection:
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("Restored SQLite database failed integrity check")


if __name__ == "__main__":
    restore_dump(sys.argv[1], "/etc/gitea/app.ini")
