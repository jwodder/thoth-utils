from __future__ import annotations
import os
import socket
import subprocess
import tomllib
import click
from eletter import compose
from outgoing import from_config_file
from pydantic import BaseModel
from .util import get_config_path


class Config(BaseModel):
    sender: str
    recipient: str

    model_config = {"extra": "forbid"}


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.argument("unit")
def main(unit: str) -> None:
    configfile = get_config_path()
    with configfile.open("rb") as fp:
        data = tomllib.load(fp)
    cfg = Config.model_validate(data.get("mail-systemd-failure", {}))
    if os.environ.get("SERVICE_RESULT") != "success":
        sender = from_config_file(configfile, fallback=False)
        subject = f"Systemd task {unit} on {socket.gethostname()} failed"
        invocation_id = os.environ["INVOCATION_ID"]
        body = subprocess.run(
            [
                "journalctl",
                "-n100",
                f"_SYSTEMD_INVOCATION_ID={invocation_id}",
                "+",
                f"INVOCATION_ID={invocation_id}",
            ],
            check=True,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        ).stdout
        msg = compose(
            subject=subject,
            from_=cfg.sender,
            to=[cfg.recipient],
            text=body,
        )
        with sender:
            sender.send(msg)


if __name__ == "__main__":
    main()
