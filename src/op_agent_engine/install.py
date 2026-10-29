"""One-shot scaffolding and systemd installation.

``init`` copies the bundled examples into ``~/.config/op-agent-engine/``
(idempotently, never overwriting) and substitutes the ``<home>``/``<user>``
placeholders. ``install-service`` renders the systemd units with the real
paths and the resolved ``op-agent-engine`` binary, stages them under
``DATA_DIR/systemd/``, and with ``--apply`` installs them with sudo and
enables the service plus the retention timer.
"""

from __future__ import annotations

import getpass
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

UNIT_NAMES: tuple[str, ...] = (
    "op-agent-engine.service",
    "op-agent-engine-cleanup.service",
    "op-agent-engine-cleanup.timer",
)

# Examples ship inside the wheel at op_agent_engine/examples/ (see the
# force-include entries in pyproject.toml). Editable installs fall back to the
# repository layout.
_REPO_EXAMPLES: dict[str, Path] = {
    "workflow.example.yaml": Path("config/workflow.example.yaml"),
    "engine.env.example": Path("deploy/engine.env.example"),
    "units/op-agent-engine.service": Path("deploy/op-agent-engine.service"),
    "units/op-agent-engine-cleanup.service": Path("deploy/op-agent-engine-cleanup.service"),
    "units/op-agent-engine-cleanup.timer": Path("deploy/op-agent-engine-cleanup.timer"),
}

_CONFIG_FILES: tuple[tuple[str, str, int], ...] = (
    ("workflow.example.yaml", "workflow.yaml", 0o644),
    ("engine.env.example", "engine.env", 0o600),
)


class InstallError(RuntimeError):
    """A subprocess step failed; the message includes the command output."""


@dataclass
class InitReport:
    created: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class ServiceReport:
    staged: list[str] = field(default_factory=list)
    installed: list[str] = field(default_factory=list)
    enabled: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def default_config_dir() -> Path:
    return Path.home() / ".config" / "op-agent-engine"


def default_stage_dir() -> Path:
    return Path.home() / ".local" / "share" / "op-agent-engine" / "systemd"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _example(rel_name: str) -> Path:
    packaged = Path(__file__).resolve().parent / "examples" / rel_name
    if packaged.is_file():
        return packaged
    return _repo_root() / _REPO_EXAMPLES[rel_name]


def _current_user() -> str:
    try:
        return getpass.getuser()
    except KeyError:
        return os.environ.get("USER", "op-agent-engine")


def _substitute(text: str) -> str:
    return text.replace("<home>", str(Path.home())).replace("<user>", _current_user())


def init_config(config_dir: Path) -> InitReport:
    """Scaffold the config directory; never overwrite existing files."""
    report = InitReport()
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(config_dir, 0o700)
    except OSError as error:
        report.errors.append(f"cannot create {config_dir}: {error}")
        return report
    for example_name, dest_name, mode in _CONFIG_FILES:
        dest = config_dir / dest_name
        if dest.exists():
            report.kept.append(str(dest))
            continue
        try:
            source = _example(example_name)
            dest.write_text(_substitute(source.read_text(encoding="utf-8")), encoding="utf-8")
            os.chmod(dest, mode)
            report.created.append(str(dest))
        except (OSError, KeyError) as error:
            report.errors.append(f"cannot create {dest}: {error}")
    return report


def find_executable() -> Path | None:
    """Locate the ``op-agent-engine`` binary on PATH, if any."""
    path = shutil.which("op-agent-engine")
    return Path(path) if path is not None else None


def render_units(exe: Path) -> dict[str, str]:
    """Render every unit file with real user, home and executable paths."""
    units: dict[str, str] = {}
    for name in UNIT_NAMES:
        text = _substitute(_example(f"units/{name}").read_text(encoding="utf-8"))
        units[name] = text.replace("<exe>", str(exe))
    return units


def _execute(cmd: list[str], *, input_text: str | None = None) -> str:
    try:
        proc = subprocess.run(
            cmd, input=input_text, capture_output=True, text=True, check=False
        )
    except OSError as error:
        raise InstallError(f"cannot run {' '.join(cmd)}: {error}") from error
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise InstallError(f"command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")
    return (proc.stdout or proc.stderr).strip()


def install_service(*, apply: bool, exe: Path, stage_dir: Path) -> ServiceReport:
    """Render and stage the units; with ``apply`` install and enable them."""
    report = ServiceReport()
    try:
        units = render_units(exe)
        stage_dir.mkdir(parents=True, exist_ok=True)
        for name, text in units.items():
            staged = stage_dir / name
            staged.write_text(text, encoding="utf-8")
            report.staged.append(str(staged))
    except (OSError, KeyError) as error:
        report.errors.append(str(error))
        return report

    prefix = "" if os.geteuid() == 0 else "sudo "
    if not apply:
        for name in UNIT_NAMES:
            report.commands.append(f"{prefix}cp {stage_dir / name} /etc/systemd/system/{name}")
        report.commands.append(f"{prefix}systemctl daemon-reload")
        report.commands.append(f"{prefix}systemctl enable --now op-agent-engine")
        report.commands.append(f"{prefix}systemctl enable --now op-agent-engine-cleanup.timer")
        return report

    sudo: list[str] = [] if os.geteuid() == 0 else ["sudo"]
    if sudo and shutil.which("sudo") is None:
        report.errors.append("sudo not found; copy the staged files manually")
        return report

    for name in UNIT_NAMES:
        target = f"/etc/systemd/system/{name}"
        try:
            _execute([*sudo, "tee", target], input_text=units[name])
        except InstallError as error:
            report.errors.append(str(error))
            return report
        report.installed.append(target)

    steps: tuple[tuple[list[str], str], ...] = (
        ([*sudo, "systemctl", "daemon-reload"], ""),
        (
            ["systemd-analyze", "verify", "/etc/systemd/system/op-agent-engine.service"],
            "",
        ),
        ([*sudo, "systemctl", "enable", "--now", "op-agent-engine"], "op-agent-engine.service"),
        (
            [*sudo, "systemctl", "enable", "--now", "op-agent-engine-cleanup.timer"],
            "op-agent-engine-cleanup.timer",
        ),
    )
    for cmd, unit in steps:
        try:
            _execute(cmd)
        except InstallError as error:
            report.errors.append(str(error))
            return report
        if unit:
            report.enabled.append(unit)
    return report
