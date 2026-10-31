"""Answers people gave, in a local SQLite file that is never synced: uv run python -m app.core.answers list"""

import argparse
import json
import sqlite3
import sys
from datetime import UTC, datetime

from app.core import config, glossary

CREATE = """CREATE TABLE IF NOT EXISTS answers (
    rule_type TEXT NOT NULL,
    field TEXT NOT NULL,
    term TEXT NOT NULL,
    value TEXT NOT NULL,
    answered_by TEXT NOT NULL,
    answered_at TEXT NOT NULL,
    sentence TEXT NOT NULL
)"""
COLUMNS = "rowid AS id, rule_type, field, term, value, answered_by, answered_at, sentence"
ORDER_ID_MASK = "[order id]"
BACKUP_NAME = "answers-backup-{}.json"
BACKUP_TIME_FORMAT = "%Y%m%d-%H%M%S"


def _connect():
    config.ANSWERS_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(config.ANSWERS_PATH)
    db.row_factory = sqlite3.Row
    db.execute(CREATE)
    return db


def save(rule_type, field, term, value, sentence):
    """Keeps every different answer, so a disagreement stays visible. Order ids never reach the file."""
    if any(row["value"] == value for row in find(rule_type, field, term)):
        return
    with _connect() as db:
        db.execute(
            "INSERT INTO answers VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                rule_type,
                field,
                term,
                value,
                config.ANSWERED_BY,
                datetime.now(UTC).isoformat(timespec="seconds"),
                glossary.mask_order_ids(sentence, ORDER_ID_MASK),
            ),
        )


def find(rule_type, field, term):
    with _connect() as db:
        rows = db.execute(
            f"SELECT {COLUMNS} FROM answers WHERE rule_type = ? AND field = ? AND term = ? ORDER BY rowid",
            (rule_type, field, term),
        ).fetchall()
    return [dict(row) for row in rows]


def for_rule_type(rule_type):
    with _connect() as db:
        rows = db.execute(f"SELECT {COLUMNS} FROM answers WHERE rule_type = ? ORDER BY rowid", (rule_type,)).fetchall()
    return [dict(row) for row in rows]


def everything():
    with _connect() as db:
        return [dict(row) for row in db.execute(f"SELECT {COLUMNS} FROM answers ORDER BY rowid").fetchall()]


def pick(rule_type, field, term, value):
    """A person settled a conflict: the other answers for this term go."""
    with _connect() as db:
        db.execute(
            "DELETE FROM answers WHERE rule_type = ? AND field = ? AND term = ? AND value != ?",
            (rule_type, field, term, value),
        )


def delete(answer_id):
    with _connect() as db:
        return db.execute("DELETE FROM answers WHERE rowid = ?", (answer_id,)).rowcount


def delete_all():
    with _connect() as db:
        return db.execute("DELETE FROM answers").rowcount


def _as_json(rows):
    return json.dumps(rows, indent=2) + "\n"


def backup_and_clear():
    """Writes the answers beside the answers file, then clears them. Returns (backup path or None if empty, count)."""
    rows = everything()
    if not rows:
        return None, 0
    path = config.ANSWERS_PATH.parent / BACKUP_NAME.format(datetime.now().strftime(BACKUP_TIME_FORMAT))
    # "x" so a second reset in the same second can't overwrite a backup.
    with open(path, "x", encoding="utf-8") as f:
        f.write(_as_json(rows))
    return path, delete_all()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="print every saved answer")
    export = commands.add_parser("export", help="write every saved answer as JSON")
    export.add_argument("--out", help="file to write (default: print)")
    remove = commands.add_parser("delete", help="delete one answer by id, or all of them")
    remove.add_argument("id", nargs="?", type=int)
    remove.add_argument("--all", action="store_true")
    a = ap.parse_args(argv)
    if a.command == "list":
        for row in everything():
            print(
                f"{row['id']}: {row['rule_type']} {row['field']} {row['term']!r} = {row['value']!r} "
                f"({row['answered_by']}, {row['answered_at']}): {row['sentence']}"
            )
    elif a.command == "export":
        text = _as_json(everything())
        if a.out:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(text)
        else:
            sys.stdout.write(text)
    elif a.all:
        print(f"deleted {delete_all()}")
    elif a.id is None:
        ap.error("give an id or --all")
    else:
        print(f"deleted {delete(a.id)}")


if __name__ == "__main__":
    main()
