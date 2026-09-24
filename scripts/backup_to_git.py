"""Back the library up into the private backup repo, in a form git can diff.

- every data table is dumped as SQL text (the FTS index is left out: it's rebuilt on restore),
  so git stores only what changed since last time
- photos are copied one file each; only new ones are added
- commit + push only when something changed

usage: python scripts/backup_to_git.py <data_dir> <backup_repo_dir>
restore: sqlite3 core.db < library/core.sql   (then open it with Store once: FTS rebuilds via
         INSERT INTO records_fts(records_fts) VALUES('rebuild'))
"""

import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

TABLES = ("records", "links", "images", "moods", "revisions", "meta")


def dump(db_path: Path, out: Path) -> None:
    src = sqlite3.connect(db_path)
    lines = []
    for line in src.iterdump():
        # keep schema + rows of the data tables; skip the FTS shadow tables and triggers
        if "records_fts" in line:
            continue
        lines.append(line)
    src.close()
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def git(repo: Path, *args: str) -> str:
    # no console window when run from the scheduler under pythonw
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          encoding="utf-8", creationflags=flags).stdout


def log(data: Path, msg: str) -> None:
    with open(data / "backup.log", "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M}] {msg}\n")


def main() -> None:
    data, repo = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    lib = repo / "library"
    (lib / "images").mkdir(parents=True, exist_ok=True)
    dump(data / "core.db", lib / "core.sql")
    new_images = 0
    for f in (data / "images").iterdir():
        dst = lib / "images" / f.name
        if not dst.exists():
            shutil.copy2(f, dst)
            new_images += 1
    for name in ("secret.txt",):                          # never back up the key
        (lib / name).unlink(missing_ok=True)
    git(repo, "add", "-A", "library")
    if not git(repo, "status", "--porcelain", "library").strip():
        log(data, "nothing changed")
        return
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    git(repo, "-c", "user.name=Lune", "-c", "user.email=gswtcbsf44@privaterelay.appleid.com",
        "commit", "-q", "-m", f"library backup {stamp} (+{new_images} photos)")
    git(repo, "push", "-q")
    log(data, f"backed up: +{new_images} photos")


def run() -> None:
    data = Path(sys.argv[1]).resolve()
    try:
        main()
    except Exception as e:                                # the scheduler has no console: leave a trace
        log(data, f"FAILED: {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    run()
