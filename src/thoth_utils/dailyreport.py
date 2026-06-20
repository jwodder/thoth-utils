from __future__ import annotations
from mailbox import mbox
from pathlib import Path
import re
from shutil import disk_usage
import socket
import subprocess
import time
import tomllib
import click
from eletter import compose, reply_quote
from outgoing import from_config_file
from pydantic import BaseModel
from .authfail import AuthfailDB
from .util import get_config_path

DISK_THRESHOLD = 75  # measured in percentage points

TAGSEQ = "DISK LOGERR REBOOT MAIL".split()


def mkalias(s: str) -> str:
    return s.replace("_", "-")


class Config(BaseModel):
    sender: str
    recipient: str
    mbox_dir: Path
    disks: list[str]

    model_config = {
        "alias_generator": mkalias,
        "extra": "forbid",
    }


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--send", is_flag=True)
def main(send: bool) -> None:
    configfile = get_config_path()
    with configfile.open("rb") as fp:
        data = tomllib.load(fp)
    cfg = Config.model_validate(data.get("dailyreport", {}))
    sender = from_config_file(configfile, fallback=False)
    tags: set[str] = set()
    reports = []
    reports.append(check_mail(cfg.mbox_dir, tags))
    reports.append(check_reboot(tags))
    reports.append(check_load())
    reports.append(check_throttled())
    for d in cfg.disks:
        reports.append(check_disk(tags, d))
    reports.append(check_authfail())
    reports.append(check_authfail_stderr())
    body = "\n\n".join(r for r in reports if r is not None and r != "")
    if not body:
        body = "Nothing to report"
    body += "\n"
    subject = ""
    for t in TAGSEQ:
        if t in tags:
            subject += "[" + t + "] "
            tags.remove(t)
    for t in sorted(tags):
        subject += "[" + t + "] "
    subject += f"Status Report: {socket.gethostname()}, {iso8601_Z()}"
    if send:
        msg = compose(subject=subject, from_=cfg.sender, to=[cfg.recipient], text=body)
        with sender:
            sender.send(msg)
    else:
        # Something about typical dailyreport contents (the size? long lines?)
        # invariably causes serialized EmailMessage's to use quoted-printable
        # transfer encoding no matter what I do.  Thus, in order to actually be
        # able to view non-ASCII characters in subjects of recently-received
        # e-mails in `less`, we need to basically output a pseudo-e-mail.
        click.echo_via_pager(f"Subject: {subject}\n\n{body}".rstrip("\n"))


def check_mail(mbox_dir: Path, tags: set[str]) -> str | None:
    nonempty_boxes = []
    for p in mbox_dir.iterdir():
        if p.is_file() and p.stat().st_size > 0:
            tags.add("MAIL")
            qty: int | None
            try:
                mb = mbox(p, create=False)
                mb.lock()
                try:
                    qty = len(mb)
                finally:
                    try:
                        mb.unlock()
                    except Exception:
                        pass
            except Exception:
                qty = None
            if qty is not None:
                s = f"- {p} - {qty} message"
                if qty != 1:
                    s += "s"
                nonempty_boxes.append(s)
            else:
                nonempty_boxes.append(f"- {p} - failed to count messages")
    if nonempty_boxes:
        return "There is mail in the following mailboxes:\n" + "\n".join(nonempty_boxes)
    else:
        return None


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
            report += "\n" + "\n".join(f"    {s}" for s in pkgs)
        else:
            report += " UNKNOWN"
        return report
    else:
        return None


def check_load() -> str:
    with open("/proc/loadavg") as fp:
        return "Load: " + ", ".join(fp.read().split()[:3])


def check_throttled() -> str | None:
    # <https://www.raspberrypi.com/documentation/computers/os.html#get_throttled>
    # <https://gist.github.com/Paraphraser/17fb6320d0e896c6446fb886e1207c7e#the-vcgencmd_power_reportsh-script>
    r = subprocess.run(
        ["vcgencmd", "get_throttled"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    if (m := re.fullmatch(r"throttled=0x([0-9A-Fa-f]+)", r.stdout.strip())) is not None:
        throttled = int(m[1], base=16)
        events = []
        if throttled & (1 << 16):
            events.append("Undervoltage")
        if throttled & (1 << 17):
            events.append("Arm frequency capping")
        if throttled & (1 << 18):
            events.append("Throttling")
        if throttled & (1 << 19):
            events.append("Soft temperature limit")
        if events:
            return (
                "The following throttling events have occurred since last reboot:\n"
                + "\n".join(f"- {ev}" for ev in events)
            )
        else:
            return None
    else:
        raise ValueError(
            "Could not parse `vcgencmd get_throttled` output: {r.stdout!r}"
        )


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
        f"   ({pctused:.2f}%)"
    )


def check_authfail() -> str:
    with AuthfailDB.connect() as db:
        return db.dailyreport()


def check_authfail_stderr() -> str | None:
    r = subprocess.run(
        ["journalctl", "--quiet", "--since=-24h", "_SYSTEMD_UNIT=authfail.service"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    if r.stdout:
        return "Stderr from authfail in the past 24 hours:\n\n" + reply_quote(
            r.stdout
        ).rstrip("\n")
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
