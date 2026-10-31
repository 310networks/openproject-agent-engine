"""Container packaging invariants.

The container design relies on a few things lining up:

* ``./config`` is the only host bind mount, and it is read-only;
* the container paths ``docker/config-sync.sh`` writes into ``docker/config/engine.env``
  are exactly the mount points declared in ``docker-compose.yml``;
* the image never receives operator state or secrets (``.dockerignore``);
* ``docker/config/`` ships as templates edited in place -- nothing that can
  hold a real secret or the operator's own content may be committed in it.

These tests fail when one of them drifts, which is otherwise only visible after
a full image build.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKER_DIR = REPO_ROOT / "docker"
COMPOSE = DOCKER_DIR / "docker-compose.yml"
DOCKERFILE = DOCKER_DIR / "Dockerfile"
BUILD_OVERLAY = DOCKER_DIR / "docker-compose.build.yml"
DOCKERIGNORE = REPO_ROOT / ".dockerignore"
SYNC = REPO_ROOT / "docker" / "config-sync.sh"
VERSION_FILE = REPO_ROOT / "docker" / "version.json"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "docker.yml"
IMAGE_REPO = "kookster310/op-agent-engine"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text())


def _service(name: str) -> dict:
    return _compose()["services"][name]


def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()
    return values


def _populate(home: Path) -> None:
    """Create the operator's own files, leaving existing edits alone."""
    pi = home / ".pi" / "agent"
    (pi / "skills" / "demo").mkdir(parents=True, exist_ok=True)
    files = {
        pi / "skills" / "demo" / "SKILL.md": "demo\n",
        pi / "settings.json": '{"defaultProvider": "openrouter"}\n',
        pi / "auth.json": '{"openrouter": {"key": "x"}}\n',
        home / ".gitconfig": "[user]\n\tname = Operator\n",
    }
    for path, content in files.items():
        if not path.exists():
            path.write_text(content)


def _sync(
    tmp_path: Path, *args: str, populate_home: bool = False
) -> tuple[Path, subprocess.CompletedProcess[str]]:
    """Run config-sync.sh against a throwaway HOME and CONFIG_DIR."""
    home = tmp_path / "home"
    config_dir = tmp_path / "config"
    home.mkdir(exist_ok=True)
    if populate_home:
        _populate(home)

    result = subprocess.run(
        [str(SYNC), *args],
        env={
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "CONFIG_DIR": str(config_dir),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return config_dir, result


def _generated_env(tmp_path: Path) -> dict[str, str]:
    config_dir, result = _sync(tmp_path)
    assert result.returncode == 0, result.stderr
    return _env_file(config_dir / "engine.env")


# -- repository contents -----------------------------------------------------


def test_committed_config_is_templates_only() -> None:
    # docker/config/ ships as templates that are edited in place, so the only
    # committed files there are the templates and the empty skeleton.
    templates = {
        "docker/config/gitconfig",
        "docker/config/pi/auth.json",
        "docker/config/pi/settings.json",
        "docker/config/opencode/opencode.json",
    }
    expected = templates | {
        "docker/config/.gitkeep",
        "docker/config/pi/.gitkeep",
        "docker/config/pi/skills/.gitkeep",
        "docker/config/opencode/.gitkeep",
    }
    tracked = {
        path
        for path in _git("ls-files", "-z", "docker/config").split("\0")
        if path
    }
    assert tracked == expected, tracked ^ expected
    # Nothing ignored may sneak in as an untracked addition either.
    untracked = {
        path
        for path in _git(
            "ls-files", "--others", "--exclude-standard", "-z", "docker/config"
        ).split("\0")
        if path
    }
    assert untracked == set(), untracked

    # Everything that can hold a real secret or the operator's own content
    # stays out of git, while the templates next to it do not.
    for operator_file in (
        "docker/config/engine.env",
        "docker/config/workflow.yaml",
        "docker/config/pi/skills/demo/SKILL.md",
        "docker/config/opencode/auth.json",
    ):
        assert _is_ignored(operator_file), f"{operator_file} must be git-ignored"
    for template in templates:
        assert not _is_ignored(template), f"{template} must be committed"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", path],
        cwd=REPO_ROOT,
        check=False,
    )
    return result.returncode == 0


def test_dockerignore_keeps_secrets_and_state_out_of_the_image() -> None:
    patterns = {
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    for required in (
        "docker/config",
        ".git",
        "workflow.yaml",
        "engine.env",
        ".venv",
    ):
        assert required in patterns


def test_committed_config_templates_are_valid_and_free_of_operator_data() -> None:
    config = REPO_ROOT / "docker" / "config"

    for name in ("pi/settings.json", "pi/auth.json", "opencode/opencode.json"):
        payload = json.loads((config / name).read_text())
        assert isinstance(payload, dict), name

    # Placeholders only: this is the file a real key gets pasted into, so the
    # committed version must never contain one.
    for path in (
        config / "gitconfig",
        config / "pi" / "settings.json",
        config / "pi" / "auth.json",
        config / "opencode" / "opencode.json",
    ):
        text = path.read_text()
        assert "/home/" not in text, f"{path} leaks a personal path"
        assert "310networks" not in text, f"{path} leaks an operator host"
        assert "@example.com" not in text or path.name == "gitconfig", path
        for line in text.splitlines():
            if '"key"' in line or '"apiKey"' in line:
                assert "replace-with" in line, f"{path} looks like a real key"
        if path.name == "gitconfig":
            assert "agent@example.com" in text, "gitconfig must not name a real person"

    # opencode json is only read when the image is built with it, but the
    # schema key must point at the real config URL.
    assert "opencode.ai/config.json" in (config / "opencode" / "opencode.json").read_text()


def test_agent_env_var_selects_the_runner() -> None:
    """`AGENT` is the single knob for pi versus opencode."""
    example_workflow = (REPO_ROOT / "workflow.example.yaml").read_text()
    example_env = (REPO_ROOT / "engine.env.example").read_text()
    entrypoint = (DOCKER_DIR / "entrypoint.sh").read_text()

    # Every agent in the example takes its runner from the environment, so
    # switching agent CLIs does not mean editing four agents.
    assert example_workflow.count('runner: "${AGENT}"') == 4
    assert "\nAGENT=pi\n" in example_env
    # The container refuses to start rather than failing inside a workflow load.
    assert ': "${AGENT:?AGENT is not set' in entrypoint


def test_entrypoint_seeds_every_documented_config_file() -> None:
    entrypoint = (REPO_ROOT / "docker" / "entrypoint.sh").read_text()
    config = REPO_ROOT / "docker" / "config"

    assert 'seed_file "${CONFIG_DIR}/pi/settings.json"' in entrypoint
    # auth files hold keys and must not be world-readable in the volume.
    assert 'seed_file "${CONFIG_DIR}/pi/auth.json" "${AGENT_DIR}/auth.json" 0600' in entrypoint
    assert 'seed_file "${CONFIG_DIR}/gitconfig"' in entrypoint
    # opencode splits config ($HOME/.config) from credentials ($HOME/.local/share).
    assert 'for src in "${CONFIG_DIR}"/opencode/*.json' in entrypoint
    assert 'auth.json) seed_file "${src}" "${OPENCODE_DATA_DIR}/auth.json" 0600 ;;' in entrypoint
    assert '"${OPENCODE_CONFIG_DIR}/${src##*/}" 0644' in entrypoint
    assert (config / "gitconfig").is_file()


def test_container_scripts_are_executable() -> None:
    for script in ("entrypoint.sh", "config-sync.sh", "provision-users.sh"):
        path = REPO_ROOT / "docker" / script
        assert path.is_file()
        assert os.access(path, os.X_OK), f"{path} must be executable"


# -- config staging ----------------------------------------------------------


def test_sync_creates_only_the_skeleton(tmp_path: Path) -> None:
    config_dir, result = _sync(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (config_dir / "workflow.yaml").is_file()
    assert (config_dir / "engine.env").is_file()
    assert (config_dir / "pi" / "skills").is_dir()
    # Nothing of the operator's own is invented.
    assert not (config_dir / "pi" / "auth.json").exists()
    assert not (config_dir / "pi" / "settings.json").exists()
    assert not (config_dir / "gitconfig").exists()
    assert list((config_dir / "pi" / "skills").iterdir()) == []


def test_sync_copies_operator_files_with_container_modes(tmp_path: Path) -> None:
    config_dir, result = _sync(tmp_path, populate_home=True)

    assert result.returncode == 0, result.stderr
    for relative in ("pi/settings.json", "pi/auth.json", "gitconfig"):
        path = config_dir / relative
        assert path.is_file(), relative
        # World-readable: the container reads these as its own user.
        assert path.stat().st_mode & 0o444 == 0o444
    assert (config_dir / "pi" / "skills" / "demo" / "SKILL.md").is_file()
    # engine.env holds tokens: it is read by compose, never mounted.
    assert (config_dir / "engine.env").stat().st_mode & 0o077 == 0


def test_sync_dereferences_symlinked_skills(tmp_path: Path) -> None:
    home = tmp_path / "home"
    shared = tmp_path / "shared-skill"
    (shared).mkdir(parents=True)
    (shared / "SKILL.md").write_text("linked\n")
    pi = home / ".pi" / "agent"
    (pi / "skills").mkdir(parents=True)
    (pi / "skills" / "linked").symlink_to(shared)

    config_dir, result = _sync(tmp_path)

    assert result.returncode == 0, result.stderr
    copied = config_dir / "pi" / "skills" / "linked" / "SKILL.md"
    assert copied.is_file()
    assert not copied.is_symlink()


def test_check_mode_reports_drift_without_writing(tmp_path: Path) -> None:
    config_dir, _ = _sync(tmp_path, populate_home=True)
    settings = config_dir / "pi" / "settings.json"
    original = settings.read_text()

    (tmp_path / "home" / ".pi" / "agent" / "settings.json").write_text('{"changed": 1}\n')
    config_dir, result = _sync(tmp_path, "--check", populate_home=True)

    assert result.returncode == 1
    assert "outdated" in result.stdout
    assert settings.read_text() == original


def test_sync_is_idempotent(tmp_path: Path) -> None:
    config_dir, _ = _sync(tmp_path, populate_home=True)
    snapshot = {
        path.relative_to(config_dir): path.read_bytes()
        for path in config_dir.rglob("*")
        if path.is_file()
    }

    _, result = _sync(tmp_path, "--check", populate_home=True)

    assert result.returncode == 0, result.stdout
    assert "matches the host" in result.stdout
    for relative, content in snapshot.items():
        assert (config_dir / relative).read_bytes() == content


# -- compose / env agreement -------------------------------------------------


def test_config_is_the_only_bind_mount_and_it_is_read_only() -> None:
    binds = [v for v in _service("engine")["volumes"] if str(v).startswith(("./", "/"))]
    binds = [v for v in binds if not str(v).startswith("/home/agent/")]

    assert binds == ["./config:/config:ro"]


def test_volume_mount_points_exist_in_the_image() -> None:
    """Docker copies image ownership into a fresh named volume.

    A volume whose mount point is missing from the image starts out owned by
    root, and the container's own user then cannot write to it.
    """
    dockerfile = DOCKERFILE.read_text()
    mkdirs = " ".join(dockerfile.split("\n"))

    # /config is the read-only bind mount; it is not a volume.
    targets = {
        str(volume).split(":")[1]
        for volume in _service("engine")["volumes"]
        if not str(volume).startswith("/config")
    }
    for target in targets:
        assert target in mkdirs, f"{target} is mounted but never created in the image"
    assert "chown -R agent:agent /home/agent" in mkdirs


def test_opencode_state_is_persisted_like_pi_state() -> None:
    """opencode keeps credentials and sessions outside the config mount."""
    volumes = {str(v) for v in _service("engine")["volumes"]}

    assert "opencode-data:/home/agent/.local/share/opencode" in volumes
    assert "pi-agent:/home/agent/.pi/agent" in volumes
    for volume in ("opencode-data", "pi-agent", "engine-data", "agent-workspace"):
        assert volume in _compose()["volumes"], f"{volume} is not declared"


def test_generated_env_container_paths_match_the_compose_mounts(tmp_path: Path) -> None:
    env = _generated_env(tmp_path)
    # Each volume is "source:target[:mode]"; the engine only cares about target.
    targets = {str(v).split(":")[1] for v in _service("engine")["volumes"]}

    assert env["WORKSPACE_ROOT"] in targets
    assert env["DATA_DIR"] in targets
    assert env["ENGINE_CONFIG"] == "/config/workflow.yaml"
    # The engine path must live inside the read-only config mount.
    assert env["ENGINE_CONFIG"].startswith("/config/")


def test_generated_env_uses_container_paths_only(tmp_path: Path) -> None:
    config_dir, _ = _sync(tmp_path)
    env = _env_file(config_dir / "engine.env")

    for name in ("WORKSPACE_ROOT", "DATA_DIR", "ENGINE_CONFIG"):
        assert env[name].startswith("/"), f"{name} must be absolute in the container"
    assert "<home>" not in (config_dir / "engine.env").read_text()
    # Inside a container the dashboard has to bind every interface; the host
    # port is published on loopback by docker-compose.yml.
    assert env["DASHBOARD_HOST"] == "0.0.0.0"


def test_compose_pulls_and_never_builds() -> None:
    # Deployments must work from docker/docker-compose.yml + docker/config/
    # alone, so the published image is the only source of the engine -- there is
    # deliberately no build file to overlay.
    compose = _compose()
    assert "build" not in compose
    for name, service in compose["services"].items():
        assert "build" not in service, f"{name} must not build"
    assert not BUILD_OVERLAY.exists(), "users pull the image; do not ship a build path"


def test_docker_readme_documents_every_environment_variable() -> None:
    """The deployment README is the env-var reference; keep it complete."""
    from op_agent_engine.config import EngineSettings

    readme = (DOCKER_DIR / "README.md").read_text()
    missing = [
        name.upper()
        for name in EngineSettings.model_fields
        if name.upper() not in readme
    ]
    assert not missing, f"undocumented in docker/README.md: {missing}"

    # Compose-level variables are not engine settings, so they are easy to
    # forget when one of them is added to the compose file.
    compose_text = COMPOSE.read_text()
    for name in set(re.findall(r"\$\{(OP_AGENT_ENGINE_TAG|DASHBOARD_BIND)", compose_text)):
        assert name in readme, name
    # The image defaults the README promises must match the Dockerfile.
    dockerfile = DOCKERFILE.read_text()
    for name, value in (
        ("ENGINE_CONFIG", "/config/workflow.yaml"),
        ("WORKSPACE_ROOT", "/home/agent/agents"),
        ("DATA_DIR", "/home/agent/.local/share/op-agent-engine"),
    ):
        assert f"{name}={value}" in dockerfile, name
        assert value in readme, f"{name} default is stale in docker/README.md"


def test_every_docker_artifact_lives_under_docker() -> None:
    """The docker/ directory is the whole deployment; the root stays code."""
    for name in (
        "Dockerfile",
        "docker-compose.yml",
        "entrypoint.sh",
        "config-sync.sh",
        "provision-users.sh",
        "version.json",
    ):
        assert (DOCKER_DIR / name).is_file(), f"docker/{name} missing"

    for stale in ("Dockerfile", "docker-compose.yml", "docker-compose.build.yml"):
        assert not (REPO_ROOT / stale).exists(), f"{stale} belongs in docker/"

    # .dockerignore is the one exception: docker only reads it from the build
    # context root, which is the repository.
    assert (REPO_ROOT / ".dockerignore").is_file()
    # CI builds from the repository context with the Dockerfile under docker/.
    workflow = WORKFLOW.read_text()
    assert "file: docker/Dockerfile" in workflow


def test_image_defaults_the_container_paths(tmp_path: Path) -> None:
    # A pull-only user writes a config/engine.env with just the URL and tokens,
    # so the paths have to come from the image and match the mounts.
    dockerfile = DOCKERFILE.read_text()
    targets = {str(v).split(":")[1] for v in _service("engine")["volumes"]}
    expected = {
        "ENGINE_CONFIG": "/config/workflow.yaml",
        "WORKSPACE_ROOT": "/home/agent/agents",
        "DATA_DIR": "/home/agent/.local/share/op-agent-engine",
    }

    for name, value in expected.items():
        assert f"{name}={value}" in dockerfile, name
        assert value in targets or value == "/config/workflow.yaml"
    assert "DASHBOARD_HOST=0.0.0.0" in dockerfile


def test_compose_runs_the_expected_commands() -> None:
    assert _service("engine")["command"] == ["run"]
    assert _service("dashboard")["command"] == ["dashboard"]
    # One-shot tasks: the installer's steps, available without the installer.
    for task, command in (
        ("provision", "provision"),
        ("cleanup", "cleanup"),
        ("check-config", "check-config"),
    ):
        assert _service(task)["command"] == [command]
        assert _service(task)["profiles"] == ["tasks"]
        assert _service(task)["restart"] == "no"


def test_provision_users_script_never_hands_out_the_host_uid() -> None:
    """The wrapper stages a readable copy instead of mounting config writable."""
    script = (REPO_ROOT / "docker" / "provision-users.sh").read_text()

    # engine.env is 0600 and host-owned, so the container user can neither read
    # nor write it. A staged copy is mounted as /config for this one command...
    assert 'cp "${config_dir}/engine.env" "${config_dir}/workflow.yaml" "$work/"' in script
    assert '-v "${work}:/config"' in script
    # ...and only the staged result is copied back, still private.
    assert 'cp "${work}/engine.env" "${config_dir}/engine.env"' in script
    assert 'chmod 0600 "${config_dir}/engine.env"' in script
    # No UID/GID matching, and the real config directory stays read-only.
    assert "--user" not in script
    assert "config:/config:rw" not in script


def test_docs_explain_provisioning_instead_of_the_installer() -> None:
    docs = (REPO_ROOT / "docs" / "DOCKER.md").read_text()

    assert "docker/provision-users.sh" in docs
    assert "OP_PROVISION_TOKEN" in docs
    # A changed env file needs a recreate, not a restart.
    assert "docker compose up -d" in docs


def test_stop_grace_period_covers_the_engine_shutdown_budget(tmp_path: Path) -> None:
    budget = int(_generated_env(tmp_path)["STOP_GRACE_SECONDS"]) + 20  # SIGKILL grace
    assert _service("engine")["stop_grace_period"] == "90s"
    assert budget <= 90


# -- publishing --------------------------------------------------------------


def _docker_version() -> str:
    return json.loads(VERSION_FILE.read_text())["version"]


def test_image_version_is_semver_and_matches_the_package_version() -> None:
    version = _docker_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)

    package = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    # The image tag and `op-agent-engine --version` must not disagree.
    assert version == package["project"]["version"]


def test_compose_defaults_to_the_published_image() -> None:
    assert _service("engine")["image"].startswith(f"{IMAGE_REPO}:")


def test_workflow_publishes_to_the_same_repository() -> None:
    text = WORKFLOW.read_text()

    assert f"IMAGE: {IMAGE_REPO}" in text
    assert "DOCKERHUB_USERNAME" in text
    assert "DOCKERHUB_TOKEN" in text
    # The tag must come from docker/version.json, not from the workflow.
    assert "VERSION_FILE: docker/version.json" in text
    assert "docker/build-push-action" in text
