from __future__ import annotations
from dataclasses import InitVar, dataclass, field
import logging
import tomllib
from types import TracebackType
import click
from click_loglevel import LogLevel
from pydantic import BaseModel
import requests
from .util import get_config_path

log = logging.getLogger("update_dns")


class Config(BaseModel):
    ipv4: bool
    ipv6: bool
    domain: str
    subdomain: str
    digital_ocean_token: str

    model_config = {"extra": "forbid"}


@dataclass
class DigitalOceanClient:
    session: requests.Session = field(init=False)
    token: InitVar[str]

    def __post_init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def __enter__(self) -> DigitalOceanClient:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_val: BaseException | None,
        _exc_tb: TracebackType | None,
    ) -> None:
        self.session.close()

    def set_dns_record(
        self, domain: str, recname: str, rectype: str, recvalue: str
    ) -> None:
        url = f"https://api.digitalocean.com/v2/domains/{domain}/records"
        params = {"name": f"{recname}.{domain}", "type": rectype}
        records = []
        while True:
            r = self.session.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            records.extend(data["domain_records"])
            try:
                url = data["links"]["pages"]["next"]
            except Exception:
                break
            else:
                params = {}
        values = {rec["data"] for rec in records}
        if values == {recvalue}:
            log.info("No change to %s records", rectype)
        elif {recvalue} < values:
            log.info(
                "Deleting %d extra %s records",
                sum(1 for rec in records if rec["data"] != recvalue),
                rectype,
            )
            for rec in records:
                if rec["data"] != recvalue:
                    r = self.session.delete(
                        f"https://api.digitalocean.com/v2/domains/{domain}/records/{rec['id']}"
                    )
                    r.raise_for_status()
        elif len(records) == 1:
            log.info("Updating %s record", rectype)
            rec = records[0]
            r = self.session.patch(
                f"https://api.digitalocean.com/v2/domains/{domain}/records/{rec['id']}",
                json={"type": rectype, "data": recvalue},
            )
            r.raise_for_status()
        else:
            log.info(
                "Deleting %d %s records and creating a new one", len(records), rectype
            )
            for rec in records:
                r = self.session.delete(
                    f"https://api.digitalocean.com/v2/domains/{domain}/records/{rec['id']}"
                )
                r.raise_for_status()
            r = self.session.post(
                f"https://api.digitalocean.com/v2/domains/{domain}/records",
                json={
                    "type": rectype,
                    "name": recname,
                    "data": recvalue,
                },
            )
            r.raise_for_status()


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-l",
    "--log-level",
    type=LogLevel(),
    default="INFO",
    help="Set logging level",
    show_default=True,
)
def main(log_level: int) -> None:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        level=log_level,
    )
    with get_config_path().open("rb") as fp:
        data = tomllib.load(fp)
    cfg = Config.model_validate(data.get("update-dns", {}))
    with DigitalOceanClient(token=cfg.digital_ocean_token) as doclient:
        if cfg.ipv4:
            log.info("Fetching our public IPv4 address ...")
            r = requests.get("https://4.ident.me")
            r.raise_for_status()
            ipaddr = r.text.strip()
            log.info("Got IPv4 address: %s", ipaddr)
            doclient.set_dns_record(cfg.domain, cfg.subdomain, "A", ipaddr)
        else:
            log.info("IPv4 disabled; not setting")
        if cfg.ipv6:
            log.info("Fetching our public IPv6 address ...")
            r = requests.get("https://6.ident.me")
            r.raise_for_status()
            ipaddr = r.text.strip()
            log.info("Got IPv6 address: %s", ipaddr)
            doclient.set_dns_record(cfg.domain, cfg.subdomain, "AAAA", ipaddr)
        else:
            log.info("IPv6 disabled; not setting")


if __name__ == "__main__":
    main()
