"""Container packaging invariants.

The container design relies on a few things lining up:

* nothing is mounted from the host: deployment settings and provider keys are
  environment variables in ``docker/.env`` and all state lives in volumes;
* the engine paths the image sets are exactly the mount points declared in
  ``docker-compose.yml``, and ``docker/.env`` never overrides them with a host
  path;
* the image never receives operator state or secrets (``.dockerignore``);
* no auth.json/settings.json/config file is required or documented as a mount:
  the agent CLIs read provider credentials from the environment.

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
VERSION_FILE = DOCKER_DIR / "version.json"
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


def _is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", path],
        cwd=REPO_ROOT,
        check=False,
    )
    return result.returncode == 0


# -- repository contents -----------------------------------------------------


def test_dockerignore_keeps_secrets_and_state_out_of_the_image() -> None:
    patterns = {
        line.strip()
        for line in DOCKERIGNORE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    for required in (".git", ".env", "docker/.env", ".venv"):
        assert required in patterns


def test_env_example_is_tracked_and_the_real_env_is_not() -> None:
    assert not _is_ignored("docker/.env.example")
    assert _is_ignored("docker/.env")
    # Nothing in the deployment is tracked that could hold a real credential.
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "docker"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    assert not [path for path in tracked if path.endswith("auth.json")]


def test_agent_env_var_selects_the_runner() -> None:
    """`AGENT` is the single knob for pi versus opencode."""
    env_template = (DOCKER_DIR / ".env.example").read_text()
    entrypoint = (DOCKER_DIR / "entrypoint.sh").read_text()

    # Switching agent CLIs is one variable, not four agent edits.
    assert "\nAGENT=pi\n" in env_template
    # The container refuses to start rather than failing inside a config build.
    assert ': "${AGENT:?AGENT is not set' in entrypoint


def test_entrypoint_validates_the_environment_and_seeds_nothing() -> None:
    entrypoint = (DOCKER_DIR / "entrypoint.sh").read_text()

    for required in (
        "OP_BASE_URL",
        "OP_PROJECT",
        "AGENT",
        "WORKSPACE_ROOT",
        "DATA_DIR",
    ):
        assert f': "${{{required}:?' in entrypoint, required

    # Settings and credentials come from environment variables; there is no
    # /config bind mount to copy from.
    for stale in ("CONFIG_DIR", "seed_file"):
        assert stale not in entrypoint, stale

    # The agents commit during a run, so the entrypoint writes an identity from
    # the environment (no mounted gitconfig).
    assert "git config --global user.name" in entrypoint
    assert "GIT_AUTHOR_NAME" in entrypoint
    assert "GIT_AUTHOR_EMAIL" in entrypoint


def test_container_scripts_are_executable() -> None:
    for script in ("entrypoint.sh", "provision-users.sh"):
        path = DOCKER_DIR / script
        assert path.is_file()
        assert os.access(path, os.X_OK), f"{path} must be executable"


# -- compose / env agreement -------------------------------------------------


def test_nothing_is_mounted_from_the_host() -> None:
    for volume in _service("engine")["volumes"]:
        assert not str(volume).startswith(("./", "/")), volume


def test_volume_mount_points_exist_in_the_image() -> None:
    """Docker copies image ownership into a fresh named volume.

    A volume whose mount point is missing from the image starts out owned by
    root, and the container's own user then cannot write to it.
    """
    dockerfile = DOCKERFILE.read_text()
    mkdirs = " ".join(dockerfile.split("\n"))

    targets = {str(volume).split(":")[1] for volume in _service("engine")["volumes"]}
    for target in targets:
        assert target in mkdirs, f"{target} is mounted but never created in the image"
    assert "chown -R agent:agent /home/agent" in mkdirs


def test_opencode_state_is_persisted_like_pi_state() -> None:
    volumes = {str(v) for v in _service("engine")["volumes"]}

    assert "opencode-data:/home/agent/.local/share/opencode" in volumes
    assert "pi-agent:/home/agent/.pi/agent" in volumes
    for volume in ("opencode-data", "pi-agent", "engine-data", "agent-workspace"):
        assert volume in _compose()["volumes"], f"{volume} is not declared"


def test_image_paths_match_the_compose_mounts() -> None:
    """The engine paths are set by the image, so they must match the mounts."""
    dockerfile = DOCKERFILE.read_text()
    # Each volume is "source:target[:mode]"; the engine only cares about target.
    targets = {str(v).split(":")[1] for v in _service("engine")["volumes"]}

    for name, value in (
        ("WORKSPACE_ROOT", "/home/agent/agents"),
        ("DATA_DIR", "/home/agent/.local/share/op-agent-engine"),
        ("PROMPTS_DIR", "/opt/op-agent-engine/prompts"),
    ):
        assert f"{name}={value}" in dockerfile, name
        if name in ("WORKSPACE_ROOT", "DATA_DIR"):
            assert value in targets, f"{name} points outside the mounted volumes"
    assert "ENGINE_CONFIG" not in dockerfile


def test_generated_env_keeps_host_paths_out() -> None:
    template = (DOCKER_DIR / ".env.example").read_text()
    env = _env_file(DOCKER_DIR / ".env.example")

    assert "<home>" not in template
    assert "/home/" not in template
    # The image owns the container paths, so a stale copy in.env must not
    # silently point at the wrong directory.
    for name in ("WORKSPACE_ROOT", "DATA_DIR", "ENGINE_CONFIG"):
        assert name not in env, f"{name} must come from the image"
    # Inside a container the dashboard has to bind every interface; the host
    # port is published on loopback by docker-compose.yml.
    assert env["DASHBOARD_HOST"] == "0.0.0.0"
    assert env["DASHBOARD_PORT"] == "8765"


def test_compose_pulls_and_never_builds() -> None:
    # Deployments must work from docker/docker-compose.yml alone, so the
    # published image is the only source of the engine -- there is deliberately
    # no build file to overlay.
    compose = _compose()
    assert "build" not in compose
    for name, service in compose["services"].items():
        assert "build" not in service, f"{name} must not build"
    assert not BUILD_OVERLAY.exists(), "users pull the image; do not ship a build path"


def test_docs_list_every_environment_variable() -> None:
    """The reference docs must name every setting; the README stays minimal."""
    from op_agent_engine.config import EngineSettings

    readme = (REPO_ROOT / "README.md").read_text()
    config = (REPO_ROOT / "docs" / "CONFIG.md").read_text()
    missing = [
        name.upper()
        for name in EngineSettings.model_fields
        if name.upper() not in readme and name.upper() not in config
    ]
    assert not missing, f"undocumented in README.md or docs/CONFIG.md: {missing}"

    for required in (
        "OP_BASE_URL",
        "OP_PROJECT",
        "OP_ENGINE_TOKEN",
        "AGENT",
        "OP_TOKEN_FRONTEND",
        "OP_TOKEN_BACKEND",
        "OP_TOKEN_DESIGNER",
        "OP_TOKEN_REVIEWER",
    ):
        assert required in readme, f"{required} is required but not in README.md"

    # Compose-level variables are not engine settings, so they are easy to
    # forget when one of them is added to the compose file.
    compose_text = COMPOSE.read_text()
    for name in set(re.findall(r"\$\{(OP_AGENT_ENGINE_TAG|DASHBOARD_BIND)", compose_text)):
        assert name in readme or name in config, name


def test_every_docker_artifact_lives_under_docker() -> None:
    """The docker/ directory is the whole deployment; the root stays code."""
    for name in (
        "Dockerfile",
        "docker-compose.yml",
        "entrypoint.sh",
        "provision-users.sh",
        "version.json",
        ".env.example",
    ):
        assert (DOCKER_DIR / name).is_file(), f"docker/{name} missing"

    for stale in ("Dockerfile", "docker-compose.yml", "docker-compose.build.yml"):
        assert not (REPO_ROOT / stale).exists(), f"{stale} belongs in docker/"

    #.dockerignore is the one exception: docker only reads it from the build
    # context root, which is the repository.
    assert (REPO_ROOT / ".dockerignore").is_file()
    # CI builds from the repository context with the Dockerfile under docker/.
    workflow = WORKFLOW.read_text()
    assert "file: docker/Dockerfile" in workflow


def test_image_defaults_the_container_paths() -> None:
    # A pull-only user writes.env with just the deployment values, so the paths
    # have to come from the image and match the mounts.
    dockerfile = DOCKERFILE.read_text()
    targets = {str(v).split(":")[1] for v in _service("engine")["volumes"]}
    expected = {
        "WORKSPACE_ROOT": "/home/agent/agents",
        "DATA_DIR": "/home/agent/.local/share/op-agent-engine",
    }

    for name, value in expected.items():
        assert f"{name}={value}" in dockerfile, name
        assert value in targets, f"{name} points outside a mounted volume"
    assert "DASHBOARD_HOST=0.0.0.0" in dockerfile


def test_compose_runs_the_expected_commands() -> None:
    assert _service("engine")["command"] == ["run"]
    assert _service("dashboard")["command"] == ["dashboard"]
    # One-shot tasks: the installer's steps, available without the installer.
    for task, command in (
        ("provision", ["provision", "--apply", "--json"]),
        ("cleanup", ["cleanup"]),
        ("check-config", ["check-config"]),
    ):
        assert _service(task)["command"] == command
        assert _service(task)["profiles"] == ["tasks"]
        assert _service(task)["restart"] == "no"


def test_provision_users_script_is_interactive_and_private() -> None:
    """The script prompts on the host and writes a private.env."""
    script = (REPO_ROOT / "docker" / "provision-users.sh").read_text()

    # The administrator token is asked for at run time and never stored.
    assert 'ask_secret "  Administrator API token"' in script
    assert "unset_env OP_PROVISION_TOKEN" in script
    # Per-token verification runs in the container.
    assert "engine verify-token --login" in script
    #.env is written 0600 and holds every credential.
    assert 'chmod 0600 "$env_file"' in script
    assert "set_env OP_ENGINE_TOKEN" in script
    # No UID/GID matching and no host configuration directory to prepare.
    assert "--user" not in script
    assert "/config" not in script


def test_docs_explain_provisioning_instead_of_the_installer() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    docs = (REPO_ROOT / "docs" / "DOCKER.md").read_text()

    assert "docker/.env" in docs or ".env" in docs
    assert "./provision-users.sh" in readme
    # A changed env file needs a recreate, not a restart.
    assert "docker compose up -d" in readme
    assert "keeps the old environment" in docs


def test_stop_grace_period_covers_the_engine_shutdown_budget() -> None:
    env = _env_file(DOCKER_DIR / ".env.example")
    budget = int(env["STOP_GRACE_SECONDS"]) + 20 # SIGKILL grace
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
