"""Tests for the init and install-service scaffolding commands."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from op_agent_engine import install
from op_agent_engine.cli import CONFIG_ERROR_EXIT, OK_EXIT, main


def _fake_exe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "op-agent-engine"
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return exe


class TestInit:
    def test_creates_files_with_substituted_paths(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"

        report = install.init_config(config_dir)

        assert report.ok
        assert (config_dir / "workflow.yaml").is_file()
        env = config_dir / "engine.env"
        assert env.is_file()
        assert env.stat().st_mode & 0o777 == 0o600
        text = env.read_text(encoding="utf-8")
        assert "<home>" not in text
        assert "<user>" not in text
        assert str(Path.home()) in text

    def test_config_dir_mode(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"

        install.init_config(config_dir)

        assert config_dir.stat().st_mode & 0o777 == 0o700

    def test_never_overwrites_existing(self, tmp_path: Path) -> None:
        config_dir = tmp_path / "config"
        install.init_config(config_dir)
        (config_dir / "workflow.yaml").write_text("custom", encoding="utf-8")

        report = install.init_config(config_dir)

        assert report.ok
        assert (config_dir / "workflow.yaml").read_text(encoding="utf-8") == "custom"
        assert any("workflow.yaml" in path for path in report.kept)


class TestSourceConfig:
    def test_installs_and_backs_up_existing_files(self, tmp_path: Path) -> None:
        source = tmp_path / "checkout"
        source.mkdir()
        (source / "workflow.yaml").write_text("new workflow", encoding="utf-8")
        (source / "engine.env").write_text("OP_BASE_URL=https://op.example\n", encoding="utf-8")
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "workflow.yaml").write_text("old workflow", encoding="utf-8")
        (config_dir / "engine.env").write_text(
            "OP_BASE_URL=https://old.example\n", encoding="utf-8"
        )

        report = install.install_source_config(source, config_dir, stamp="20250101T000000Z")

        assert report.ok
        assert (config_dir / "workflow.yaml").read_text(encoding="utf-8") == "new workflow"
        assert (config_dir / "engine.env").read_text(encoding="utf-8") == (
            "OP_BASE_URL=https://op.example\n"
        )
        assert (config_dir / "workflow.yaml.bak.20250101T000000Z").is_file()
        assert (config_dir / "engine.env.bak.20250101T000000Z").is_file()
        assert (config_dir / "engine.env").stat().st_mode & 0o777 == 0o600
        assert config_dir.stat().st_mode & 0o777 == 0o700

    def test_substitutes_home_and_user_placeholders(self, tmp_path: Path) -> None:
        source = tmp_path / "checkout"
        source.mkdir()
        (source / "workflow.yaml").write_text("workflow", encoding="utf-8")
        (source / "engine.env").write_text(
            "WORKSPACE_ROOT=<home>/op-agent-engine/agents\n"
            "ENGINE_CONFIG=<home>/.config/op-agent-engine/workflow.yaml\n",
            encoding="utf-8",
        )
        config_dir = tmp_path / "config"

        report = install.install_source_config(source, config_dir)

        assert report.ok
        text = (config_dir / "engine.env").read_text(encoding="utf-8")
        assert "<home>" not in text
        assert str(Path.home()) in text

    def test_second_run_keeps_identical_files(self, tmp_path: Path) -> None:
        source = tmp_path / "checkout"
        source.mkdir()
        (source / "workflow.yaml").write_text("workflow", encoding="utf-8")
        (source / "engine.env").write_text("OP_BASE_URL=https://op.example\n", encoding="utf-8")
        config_dir = tmp_path / "config"
        install.install_source_config(source, config_dir, stamp="first")

        report = install.install_source_config(source, config_dir, stamp="second")

        assert report.ok
        assert report.copied == []
        assert len(report.kept) == 2
        assert not (config_dir / "workflow.yaml.bak.second").exists()


class TestEnvFile:
    def test_load_env_file(self, tmp_path: Path) -> None:
        path = tmp_path / "engine.env"
        path.write_text(
            "# comment\n"
            "OP_BASE_URL=https://op.example\n"
            'export QUOTED="two words"\n'
            "SINGLE='one word'\n"
            "not a pair\n",
            encoding="utf-8",
        )

        assert install.load_env_file(path) == {
            "OP_BASE_URL": "https://op.example",
            "QUOTED": "two words",
            "SINGLE": "one word",
        }

    def test_update_env_file_preserves_comments_and_order(self, tmp_path: Path) -> None:
        path = tmp_path / "engine.env"
        path.write_text(
            "# keep this comment\n"
            "OP_BASE_URL=https://op.example\n"
            "OP_ENGINE_TOKEN=old\n",
            encoding="utf-8",
        )

        install.update_env_file(
            path, {"OP_ENGINE_TOKEN": "new", "OP_TOKEN_FRONTEND": "frontend"}
        )

        text = path.read_text(encoding="utf-8")
        assert "# keep this comment" in text
        assert "OP_BASE_URL=https://op.example" in text
        assert "OP_ENGINE_TOKEN=new" in text
        assert "OP_TOKEN_FRONTEND=frontend" in text
        assert text.index("OP_BASE_URL") < text.index("OP_ENGINE_TOKEN")
        assert path.stat().st_mode & 0o777 == 0o600

    def test_update_env_file_creates_missing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "engine.env"

        install.update_env_file(path, {"OP_BASE_URL": "https://op.example"})

        assert install.load_env_file(path) == {"OP_BASE_URL": "https://op.example"}
        assert path.stat().st_mode & 0o777 == 0o600


class TestInstallService:
    def test_dry_run_stages_rendered_units(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exe = _fake_exe(tmp_path, monkeypatch)
        stage_dir = tmp_path / "stage"

        report = install.install_service(apply=False, exe=exe, stage_dir=stage_dir)

        assert report.ok
        for name in install.UNIT_NAMES:
            text = (stage_dir / name).read_text(encoding="utf-8")
            if name.endswith(".service"):
                assert str(exe) in text
            assert "<home>" not in text
            assert "<user>" not in text
            assert "<exe>" not in text
        assert any("daemon-reload" in command for command in report.commands)

    def test_apply_installs_and_enables(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exe = _fake_exe(tmp_path, monkeypatch)
        calls: list[list[str]] = []

        def fake_execute(cmd: list[str], *, input_text: str | None = None) -> str:
            calls.append(cmd)
            return ""

        monkeypatch.setattr(install, "_execute", fake_execute)
        monkeypatch.setattr(
            install.shutil,
            "which",
            lambda name: "/usr/bin/sudo" if name == "sudo" else str(exe),
        )

        report = install.install_service(apply=True, exe=exe, stage_dir=tmp_path / "stage")

        assert report.ok
        assert len(report.installed) == 4
        assert report.enabled == [
            "op-agent-engine.service",
            "op-agent-engine-dashboard.service",
            "op-agent-engine-cleanup.timer",
        ]
        assert calls[0][:3] == ["sudo", "tee", "/etc/systemd/system/op-agent-engine.service"]
        assert ["sudo", "systemctl", "daemon-reload"] in calls
        assert ["sudo", "systemctl", "enable", "--now", "op-agent-engine"] in calls
        assert [
            "sudo",
            "systemctl",
            "enable",
            "--now",
            "op-agent-engine-dashboard",
        ] in calls
        assert ["sudo", "systemctl", "enable", "--now", "op-agent-engine-cleanup.timer"] in calls

    def test_apply_reports_subprocess_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        exe = _fake_exe(tmp_path, monkeypatch)

        def failing(cmd: list[str], *, input_text: str | None = None) -> str:
            raise install.InstallError(f"boom: {cmd[0]}")

        monkeypatch.setattr(install, "_execute", failing)
        monkeypatch.setattr(install.shutil, "which", lambda name: "/usr/bin/sudo")

        report = install.install_service(apply=True, exe=exe, stage_dir=tmp_path / "stage")

        assert not report.ok
        assert report.errors


class TestCli:
    def test_cli_init(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["init", "--config-dir", str(tmp_path / "cfg")]) == OK_EXIT

        out = capsys.readouterr().out
        assert "created:" in out
        assert "install-service --apply" in out

    def test_cli_install_service_dry_run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _fake_exe(tmp_path, monkeypatch)

        assert main(["install-service", "--stage-dir", str(tmp_path / "stage")]) == OK_EXIT

        out = capsys.readouterr().out
        assert "staged:" in out
        assert "install-service --apply" in out

    def test_cli_install_service_missing_exe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setattr(install, "find_executable", lambda: None)

        assert main(["install-service", "--stage-dir", str(tmp_path / "stage")]) == (
            CONFIG_ERROR_EXIT
        )
        assert "not found on PATH" in capsys.readouterr().err

    def test_cli_init_keeps_existing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_dir = tmp_path / "cfg"
        assert main(["init", "--config-dir", str(config_dir)]) == OK_EXIT
        (config_dir / "engine.env").write_text("custom", encoding="utf-8")

        assert main(["init", "--config-dir", str(config_dir)]) == OK_EXIT

        out = capsys.readouterr().out
        assert "kept existing:" in out
        assert (config_dir / "engine.env").read_text(encoding="utf-8") == "custom"


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_install_script_forces_a_package_rebuild() -> None:
    """``uv`` caches local builds by version, so ``--force`` alone is not enough.

    The package version does not change between checkouts; without
    ``--reinstall-package`` the installer silently keeps running the
    previously built code (for example an older ``setup`` that never printed
    the generated user passwords).
    """
    script = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")

    assert "--reinstall-package op-agent-engine" in script


def test_install_script_checks_installed_sources_match_checkout() -> None:
    script = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")

    assert "SOURCE_MISMATCH" in script
    assert "wizard.py" in script
    assert "provision.py" in script
