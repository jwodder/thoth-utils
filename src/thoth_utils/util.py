from __future__ import annotations
from pathlib import Path
from platformdirs import user_config_path


def get_config_path() -> Path:
    return user_config_path("thoth-utils", "jwodder") / "config.toml"
