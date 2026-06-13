from __future__ import annotations
from pathlib import Path
from shutil import disk_usage
import socket
import sys
import time
import tomllib
import click
from eletter import compose
from outgoing import from_config_file
from pydantic import BaseModel

DISK_THRESHOLD = 75  # measured in percentage points

TAGSEQ = "DISK REBOOT MAIL".split()


class Config(BaseModel):
    sender: str
    recipient: str
    mailbox: Path
    disks: list[str]

    model_config = {"extra": "forbid"}


@click.command()
@click.argument(
    "configfile",
    type=click.Path(exists=True, readable=True, dir_okay=False, path_type=Path),
)
def main(configfile: Path) -> None:
    with configfile.open("rb") as fp:
        data = tomllib.load(fp)
    cfg = Config.model_validate(data.get("dailyreport", {}))
    sender = from_config_file(configfile, fallback=False)
    tags: set[str] = set()
    reports = []
    check_mailbox(cfg.mailbox, tags)
    reports.append(check_reboot(tags))
    reports.append(check_load())
    for d in cfg.disks:
        reports.append(check_disk(tags, d))
    body = "\n".join(r for r in reports if r is not None and r != "")
    if not body:
        body = "Nothing to report\n"
    subject = ""
    for t in TAGSEQ:
        if t in tags:
            subject += "[" + t + "] "
            tags.remove(t)
    for t in sorted(tags):
        subject += "[" + t + "] "
    subject += f"Status Report: {socket.gethostname()}, {iso8601_Z()}"
    if sys.stdout.isatty():
        # Something about typical dailyreport contents (the size? long lines?)
        # invariably causes serialized EmailMessage's to use quoted-printable
        # transfer encoding no matter what I do.  Thus, in order to actually be
        # able to view non-ASCII characters in subjects of recently-received
        # e-mails in `less`, we need to basically output a pseudo-e-email.
        click.echo_via_pager(f"Subject: {subject}\n\n{body}".rstrip("\n"))
    else:
        msg = compose(
            subject=subject,
            from_=cfg.sender,
            to=[cfg.recipient],
            text=body,
        )
        with sender:
            sender.send(msg)


def check_load() -> str:
    with open("/proc/loadavg") as fp:
        return "Load: " + ", ".join(fp.read().split()[:3]) + "\n"


def check_disk(tags: set[str], path: str) -> str:
    fssize, fsused, _ = disk_usage(path)
    sused = longint(fsused)
    ssize = longint(fssize)
    width = max(len(sused), len(ssize))
    pctused = 100 * fsused / fssize
    if pctused >= DISK_THRESHOLD:
        tags.add("DISK")
    return (
        f"Space used on {path}:\n"
        f"    {sused:>{width}}\n"
        f"  / {ssize:>{width}}\n"
        f"   ({pctused}%)\n"
    )


def check_mailbox(mailbox: Path, tags: set[str]) -> None:
    if mailbox.exists() and mailbox.stat().st_size > 0:
        tags.add("MAIL")


def check_reboot(tags: set[str]) -> str | None:
    if Path("/var/run/reboot-required").exists():
        tags.add("REBOOT")
        try:
            with open("/var/run/reboot-required.pkgs") as fp:
                pkgs = fp.read().splitlines()
        except OSError:
            pkgs = []
        report = "Reboot required by the following packages:"
        if pkgs:
            report += "\n" + "".join("    " + s + "\n" for s in pkgs)
        else:
            report += " UNKNOWN\n"
        return report
    else:
        return None


def longint(n: int) -> str:
    ns = str(n)
    nl = len(ns)
    triples = [ns[i : i + 3] for i in range(nl % 3, nl, 3)]
    if nl % 3:
        triples = [ns[: nl % 3]] + triples
    return " ".join(triples)


def iso8601_Z() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    main()
