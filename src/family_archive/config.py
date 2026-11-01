"""Small TOML configuration with explicit CLI overrides; no stored credentials."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    bucket: str
    prefix: str = ""
    profile: str | None = None
    database: Path = Path("~/.family-archive/archive.db")
    work_dir: Path = Path("~/.family-archive/work")
    log_dir: Path = Path("~/.family-archive/logs")

    def __post_init__(self) -> None:
        for key in ("database", "work_dir", "log_dir"):
            object.__setattr__(self, key, Path(getattr(self, key)).expanduser().resolve())

    def key(self, suffix: str) -> str:
        return f"{self.prefix}/{suffix}" if self.prefix else suffix

    def validate_paths(self, mount: Path) -> None:
        mount = mount.resolve()
        for path in (self.database, self.work_dir, self.log_dir):
            if path.expanduser().resolve().is_relative_to(mount):
                raise ValueError(f"Local archive path must not be on source media: {path}")


def load_config(path: Path | None = None, **overrides) -> Config:
    config_path = path or Path("~/.family-archive/config.toml").expanduser()
    values = {}
    if config_path.exists():
        with config_path.open("rb") as stream:
            values = tomllib.load(stream)
    elif path is not None:
        raise ValueError(f"Configuration file does not exist: {path}")
    allowed = {"bucket", "prefix", "profile", "database", "work_dir", "log_dir"}
    if set(values) - allowed:
        raise ValueError("Unknown configuration keys: " + ", ".join(sorted(set(values) - allowed)))
    values.update({key: value for key, value in overrides.items() if value is not None})
    if not isinstance(values.get("bucket"), str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", values["bucket"]
    ):
        raise ValueError("Set an S3 bucket name with --bucket or config.toml (not an s3:// URL).")
    if not isinstance(values.get("prefix", ""), str):
        raise ValueError("S3 prefix must be a string")
    prefix = values.get("prefix", "").strip("/")
    if any(part in {".", "..", ""} for part in prefix.split("/")) and prefix:
        raise ValueError("S3 prefix must have nonempty components without . or ..")
    values["prefix"] = prefix
    for key in ("database", "work_dir", "log_dir"):
        values[key] = Path(values.get(key, getattr(Config, key))).expanduser().resolve()
    return Config(**values)
