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
        assert len(report.installed) == 3
        assert report.enabled == [
            "op-agent-engine.service",
            "op-agent-engine-cleanup.timer",
        ]
        assert calls[0][:3] == ["sudo", "tee", "/etc/systemd/system/op-agent-engine.service"]
        assert ["sudo", "systemctl", "daemon-reload"] in calls
        assert ["sudo", "systemctl", "enable", "--now", "op-agent-engine"] in calls
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
