from __future__ import annotations
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
import sys
import traceback
from types import TracebackType
import click
from platformdirs import user_log_path, user_state_path
from txtble import Txtble

SCHEMA = """
CREATE TABLE IF NOT EXISTS authfail (
    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    timestamp timestamp NOT NULL,
    username VARCHAR(255) NOT NULL,
    src_addr VARCHAR(255) NOT NULL
);
"""

MSG_REGEXEN = [
    re.compile(
        r"(?P<timestamp>\S+) \S+ sshd\[\d+\]:"
        r"(?: message repeated \d+ times: \[)?"
        r" Failed (?:password|keyboard-interactive/pam|none)"
        r" for (?:invalid user )?(?P<username>.+?)"
        r" from (?P<src_addr>\S+) port \d+ ssh2\]?\s*"
    ),
    re.compile(
        r"(?P<timestamp>\S+) \S+ sshd\[\d+\]:"
        r"(?: message repeated \d+ times: \[)?"
        r" Invalid user (?P<username>.*?)"
        r" from (?P<src_addr>\S+) port \d+\s*",
    ),
]

VACUUM_DAYS_OLD = 60


@dataclass
class AuthfailDB:
    db: sqlite3.Connection

    @classmethod
    def connect(cls) -> AuthfailDB:
        dbpath = user_state_path("thoth-utils", "jwodder") / "authfail.db"
        dbpath.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(dbpath, autocommit=True)
        db.execute(SCHEMA)
        return cls(db=db)

    def __enter__(self) -> AuthfailDB:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            self.db.commit()
        else:
            self.db.rollback()
        self.db.close()

    def add_record(self, timestamp: datetime, username: str, src_addr: str) -> None:
        self.db.execute(
            "INSERT INTO authfail (timestamp, username, src_addr) VALUES (?, ?, ?)",
            (timestamp, username, src_addr),
        )

    def vacuum(self, age: timedelta) -> None:
        cutoff = (datetime.now(tz=UTC) - age).isoformat(timespec="seconds")
        self.db.execute("DELETE FROM authfail WHERE timestamp <= ?", (cutoff,))

    def dailyreport(self) -> str:
        cutoff = (datetime.now(tz=UTC) - timedelta(days=1)).isoformat(
            timespec="seconds"
        )
        tbl = Txtble(headers=["Attempts", "IP Address"], align=["r", "l"], padding=1)
        for row in self.db.execute(
            (
                "SELECT COUNT(*) AS qty, src_addr FROM authfail WHERE timestamp >= ?"
                " GROUP BY src_addr ORDER BY qty DESC, src_addr ASC"
            ),
            (cutoff,),
        ):
            tbl.append(list(row))
        if tbl.data:
            return f"Failed SSH login attempts in the past 24 hours:\n{tbl}"
        else:
            return "No failed SSH login attempts in the past 24 hours"


def adapt_datetime(val: datetime) -> str:
    return val.astimezone(UTC).isoformat(timespec="seconds")


def convert_datetime(val: bytes) -> datetime:
    return datetime.fromisoformat(val.decode("utf-8")).astimezone(UTC)


sqlite3.register_adapter(datetime, adapt_datetime)
sqlite3.register_converter("timestamp", convert_datetime)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--vacuum", is_flag=True)
def main(vacuum: bool) -> None:
    if vacuum:
        with AuthfailDB.connect() as db:
            db.vacuum(timedelta(days=VACUUM_DAYS_OLD))
    else:
        line = None
        p = authfail_log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fp:
            try:
                with AuthfailDB.connect() as db:
                    # `for line in sys.stdin` cannot be used here because Python buffers
                    # stdin when iterating over it, causing the script to wait for some
                    # too-large number of lines to be passed to it until it'll
                    # do anything.
                    for line in iter(sys.stdin.readline, ""):
                        for rgx in MSG_REGEXEN:
                            if m := rgx.fullmatch(line):
                                db.add_record(
                                    timestamp=datetime.fromisoformat(m["timestamp"]),
                                    username=m["username"],
                                    src_addr=m["src_addr"],
                                )
                                break
                        else:
                            print(
                                json.dumps(
                                    {
                                        "time": datetime.now().astimezone().isoformat(),
                                        "line": line,
                                        "error_type": "ParseError",
                                        "error": "Could not parse logfile entry",
                                    }
                                ),
                                file=fp,
                            )
            except Exception as e:
                print(
                    json.dumps(
                        {
                            "time": datetime.now().astimezone().isoformat(),
                            "line": line,
                            "traceback": traceback.format_exc(),
                            "error_type": type(e).__name__,
                            "error": str(e),
                        }
                    ),
                    file=fp,
                )
                sys.exit(1)


def authfail_log_path() -> Path:
    return user_log_path("thoth-utils", "jwodder") / "authfail.log"


if __name__ == "__main__":
    main()
