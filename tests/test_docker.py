"""Container packaging invariants.

The container design relies on a few things lining up:

* nothing is mounted from the host: deployment settings and provider keys are
  environment variables in ``docker/.env`` and all state lives in volumes;
* the engine paths the image sets are exactly the mount points declared in
  ``docker-compose.yml``, and ``docker/.env`` never overrides them with a host
  path;
* the image never receives operator state or secrets (``.dockerignore``);
* no auth.json/settings.json/config file is required or documented as a mount:
  pi reads provider credentials from the environment.

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

from op_agent_engine.config import AGENT_MODEL_VARS, TEAM

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


def test_pi_is_baked_in_without_a_selector() -> None:
    env_template = (DOCKER_DIR / ".env.example").read_text()
    entrypoint = (DOCKER_DIR / "entrypoint.sh").read_text()
    dockerfile = DOCKERFILE.read_text()

    assert "\nAGENT=" not in env_template
    assert ': "${AGENT:' not in entrypoint
    assert '"@earendil-works/pi-coding-agent@${PI_VERSION}"' in dockerfile


def test_entrypoint_validates_the_environment_and_seeds_no_credentials() -> None:
    entrypoint = (DOCKER_DIR / "entrypoint.sh").read_text()

    for required in (
        "OP_BASE_URL",
        "OP_PROJECT",
        "WORKSPACE_ROOT",
        "DATA_DIR",
    ):
        assert f': "${{{required}:?' in entrypoint, required

    # Settings and credentials come from environment variables; there is no
    # /config bind mount to copy from, and the only seeded files are the
    # agent standards described below.
    for stale in ("CONFIG_DIR", "seed_file"):
        assert stale not in entrypoint, stale

    # The agents commit during a run, so the entrypoint writes an identity from
    # the environment (no mounted gitconfig).
    assert "git config --global user.name" in entrypoint
    assert "GIT_AUTHOR_NAME" in entrypoint
    assert "GIT_AUTHOR_EMAIL" in entrypoint


def test_every_configured_agent_has_an_engineering_standard() -> None:
    """A team entry without an AGENTS.md fails its first run in the workspace."""
    for _, login, _, _ in TEAM:
        standard = REPO_ROOT / "agents" / login / "AGENTS.md"
        assert standard.is_file(), f"{login} has no AGENTS.md"
        text = standard.read_text(encoding="utf-8")
        assert text.startswith("# AGENTS.md"), login
        assert "## 1. Workflow contract" in text, login


def test_image_keeps_agent_standards_outside_the_workspace_volume() -> None:
    """An existing volume never receives files added by a later image."""
    dockerfile = DOCKERFILE.read_text()

    # The entrypoint syncs from this copy, which the workspace volume does not
    # hide, so an upgrade can add VFX/Audio to an existing deployment.
    assert "COPY --chown=agent:agent agents/ /opt/op-agent-engine/agents/" in dockerfile
    # A fresh volume is still seeded the conventional Docker way.
    assert "COPY --chown=agent:agent agents/ /home/agent/agents/" in dockerfile


def _entrypoint_env(tmp_path: Path, standards: Path, workspace: Path) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_engine = fake_bin / "op-agent-engine"
    fake_engine.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_engine.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    return {
        **os.environ,
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(home),
        "OP_BASE_URL": "https://openproject.example.com",
        "OP_PROJECT": "demo",
        "WORKSPACE_ROOT": str(workspace),
        "DATA_DIR": str(tmp_path / "data"),
        "AGENT_STANDARDS_DIR": str(standards),
    }


def test_entrypoint_syncs_standards_into_an_existing_workspace(tmp_path: Path) -> None:
    """An upgrade repairs missing agents and refreshes the existing standards."""
    standards = tmp_path / "image-agents"
    for agent, content in (
        ("frontendEngineer", "new frontend standard\n"),
        ("audioDesigner", "new audio standard\n"),
        ("vfxArtist", "new vfx standard\n"),
    ):
        (standards / agent).mkdir(parents=True)
        (standards / agent / "AGENTS.md").write_text(content, encoding="utf-8")

    workspace = tmp_path / "workspace"
    # This is an old deployment: it has the previous agents and no VFX/Audio.
    (workspace / "frontendEngineer").mkdir(parents=True)
    (workspace / "frontendEngineer" / "AGENTS.md").write_text(
        "old frontend standard\n", encoding="utf-8"
    )
    clone = workspace / "frontendEngineer" / "repositories" / "repo"
    clone.mkdir(parents=True)
    (clone / "tracked.txt").write_text("keep me\n", encoding="utf-8")

    subprocess.run(
        ["sh", str(DOCKER_DIR / "entrypoint.sh")],
        env=_entrypoint_env(tmp_path, standards, workspace),
        check=True,
        capture_output=True,
        text=True,
    )

    assert (workspace / "frontendEngineer" / "AGENTS.md").read_text() == (
        "new frontend standard\n"
    )
    assert (workspace / "audioDesigner" / "AGENTS.md").read_text() == (
        "new audio standard\n"
    )
    assert (workspace / "vfxArtist" / "AGENTS.md").read_text() == (
        "new vfx standard\n"
    )
    # Syncing standards must never touch an agent's clone.
    assert (clone / "tracked.txt").read_text() == "keep me\n"


def test_concurrent_entrypoint_syncs_do_not_collide(tmp_path: Path) -> None:
    """Every container must start when several sync the shared volume at once.

    The engine, dashboard and one-shot services run in separate containers (and
    therefore separate PID namespaces) but share the workspace volume. Their
    temporary files must be unique even when the containers' PIDs are the same.
    """
    standards = tmp_path / "image-agents"
    agents = {f"agent{index}": f"standard {index}\n" for index in range(10)}
    for agent, content in agents.items():
        (standards / agent).mkdir(parents=True)
        (standards / agent / "AGENTS.md").write_text(content, encoding="utf-8")

    workspace = tmp_path / "workspace"
    env = _entrypoint_env(tmp_path, standards, workspace)
    # Containers share the workspace volume but not their own home directory,
    # so give each process its own HOME to model that.
    environments = []
    for index in range(2):
        home = tmp_path / f"container-home-{index}"
        home.mkdir()
        environments.append({**env, "HOME": str(home)})
    command = ["sh", str(DOCKER_DIR / "entrypoint.sh")]
    processes = [
        subprocess.Popen(
            command,
            env=container_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for container_env in environments
    ]
    results = [process.communicate(timeout=60) for process in processes]

    for process, (stdout, stderr) in zip(processes, results, strict=True):
        assert process.returncode == 0, (process.returncode, stdout, stderr)

    for agent, content in agents.items():
        assert (workspace / agent / "AGENTS.md").read_text() == content
    # No interrupted copy may leave its temporary file behind.
    assert not [path for path in workspace.glob("*/.AGENTS.md.*") if path.is_file()]


def test_entrypoint_removes_temporary_file_after_a_failed_copy(tmp_path: Path) -> None:
    """A failed copy must not leave a stray temporary file in the workspace."""
    standards = tmp_path / "image-agents"
    (standards / "frontendEngineer").mkdir(parents=True)
    (standards / "frontendEngineer" / "AGENTS.md").write_text(
        "standard\n", encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    env = _entrypoint_env(tmp_path, standards, workspace)
    # Fail the copy after mktemp has created the temporary file. PATH's first
    # entry is the helper's fake bin directory.
    failing_cp = Path(env["PATH"].split(os.pathsep)[0]) / "cp"
    failing_cp.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    failing_cp.chmod(0o755)

    result = subprocess.run(
        ["sh", str(DOCKER_DIR / "entrypoint.sh")],
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert not list(workspace.glob("*/.AGENTS.md.*"))


def test_entrypoint_uses_a_unique_temporary_file_per_sync() -> None:
    """A PID-derived name collides across containers; mktemp does not."""
    entrypoint = (DOCKER_DIR / "entrypoint.sh").read_text()

    assert 'mktemp "${target_dir}/.${file_name}.XXXXXX"' in entrypoint
    assert '${target_dir}/.${file_name}.$$' not in entrypoint


def test_container_scripts_are_executable() -> None:
    for script in ("entrypoint.sh", "agent-init.sh"):
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


def test_state_volumes_are_persisted() -> None:
    volumes = {str(v) for v in _service("engine")["volumes"]}

    assert "pi-agent:/home/agent/.pi/agent" in volumes
    assert set(_compose()["volumes"]) == {
        "pi-agent",
        "engine-data",
        "agent-workspace",
    }


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
    # The image binds the dashboard inside the container (0.0.0.0); .env only
    # controls the host interface compose publishes it on, loopback by default.
    assert "DASHBOARD_HOST" not in env
    assert env["DASHBOARD_BIND"] == "127.0.0.1"
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
        "OP_TOKEN_FRONTEND",
        "OP_TOKEN_BACKEND",
        "OP_TOKEN_DESIGNER",
        "OP_TOKEN_VFX",
        "OP_TOKEN_AUDIO",
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
        "agent-init.sh",
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


def test_agent_init_pulls_the_current_image_before_provisioning() -> None:
    """Re-running is the upgrade path, so a stale local image is not reused."""
    script = (DOCKER_DIR / "agent-init.sh").read_text()

    # A local image from the previous release knows the previous agent team.
    # Provisioning with it silently skips agents added by the new release.
    assert 'say "pulling $image"' in script
    assert "docker compose pull --quiet" in script
    assert "docker image inspect" not in script
    assert script.index("docker compose pull") < script.index("engine provision")


def test_agent_init_script_is_interactive_and_private() -> None:
    """The script prompts on the host and writes a private.env."""
    script = (REPO_ROOT / "docker" / "agent-init.sh").read_text()

    # The administrator token is asked for at run time and never stored.
    assert 'ask_secret "Administrator API token"' in script
    assert "unset_env OP_PROVISION_TOKEN" in script
    # Per-token verification runs in the container.
    assert "engine verify-token --login" in script
    #.env is written 0600 and holds every credential.
    assert 'chmod 0600 "$env_file"' in script
    assert "set_env OP_ENGINE_TOKEN" in script
    # No UID/GID matching and no host configuration directory to prepare.
    assert "--user" not in script
    assert "/config" not in script


def test_agent_init_asks_for_the_provider_and_its_key() -> None:
    """The wizard sets up the harness and the model provider credential."""
    script = (REPO_ROOT / "docker" / "agent-init.sh").read_text()

    # The provider and the model are separate questions; the script joins them.
    assert 'ask_text "Model provider"' in script
    assert 'ask_text "Model (e.g. deepseek/deepseek-v4.1-flash-1017)"' in script
    assert 'agent_model="${provider}/${model}"' in script
    # The provider's own credential variable is asked for and stored.
    assert 'key=$(ask_secret "$primary"' in script
    assert 'set_env "$primary" "$key"' in script
    # Nothing tells the operator to finish the job by editing .env ...
    assert "set the model provider key" not in script
    # ... but the wizard does say that every answer is an editable .env line.
    assert "Every answer from this wizard is a line in" in script
    assert "docker compose up -d" in script


def test_agent_init_handles_the_local_llama_cpp_endpoint() -> None:
    """llama.cpp is selected by a URL; its API key is optional."""
    script = (DOCKER_DIR / "agent-init.sh").read_text()

    assert "llama.cpp LLAMA_BASE_URL LLAMA_API_KEY" in script
    # The endpoint is typed visibly, not as a secret, while ordinary provider
    # keys keep the hidden prompt.
    assert '*_URL|*_ENDPOINT)' in script
    assert 'key=$(ask_text "$primary" "$existing")' in script
    assert 'key=$(ask_secret "$primary" "$hint")' in script
    assert "LLAMA_API_KEY is optional" in script


def test_agent_init_leaves_github_credentials_to_the_operator() -> None:
    """The wizard sets up OpenProject; GitHub tokens are manual .env values."""
    script = (DOCKER_DIR / "agent-init.sh").read_text()
    env_template = (DOCKER_DIR / ".env.example").read_text()

    # No GitHub prompt, verification or write belongs in this wizard.
    assert 'step 3 "GitHub token"' not in script
    assert 'ask_secret "  GitHub token (GH_TOKEN)"' not in script
    assert "a GitHub token is required" not in script
    assert "verify_github_token" not in script
    assert "--entrypoint gh" not in script
    assert "set_env GH_TOKEN" not in script
    assert "set_env GH_TOKEN_REVIEWER" not in script
    assert "Use a dedicated GitHub token" not in script
    # The template still documents the manual values, including the optional
    # dedicated reviewer identity for branch-protected repositories.
    for name in (
        "GH_TOKEN",
        "GH_TOKEN_REVIEWER",
        "GIT_REVIEWER_NAME",
        "GIT_REVIEWER_EMAIL",
    ):
        assert name in env_template, name


def test_agent_init_finishes_with_an_env_reminder() -> None:
    """The last step tells the operator what to finish before starting."""
    script = (DOCKER_DIR / "agent-init.sh").read_text()

    reminder = 'step 8 "Before you start the stack"'
    assert reminder in script
    assert "Finish updating $env_file" in script
    assert "add GH_TOKEN" in script
    assert script.index('step 7 "Validating the configuration"') < script.index(
        reminder
    )
    assert "docker compose up -d" in script[script.index(reminder) :]


def test_agent_init_prints_account_credentials_on_separate_lines() -> None:
    """Passwords need to be easy to copy while creating each API token."""
    script = (DOCKER_DIR / "agent-init.sh").read_text()

    assert "account_block() {" in script
    assert 'printf \'      login:    %s\\n\'' in script
    assert 'printf \'      password: %s\\n\'' in script
    assert 'printf \'      token:    %s\\n\'' in script
    assert "copy this password now; it is shown only once" in script
    # Steps are separated from their content by a blank line as well.
    assert "printf '\\n[%s] %s\\n\\n'" in script


def test_agent_init_offers_per_agent_models_from_one_provider() -> None:
    """One provider, but an agent may use another model from it."""
    script = (DOCKER_DIR / "agent-init.sh").read_text()

    assert 'ask_text "A separate model for each agent? (y/N)"' in script
    # Asked before any model prompt, so nothing is entered twice.
    assert script.index("A separate model for each agent?") < script.index(
        'ask_text "Model (e.g.'
    )
    for variable in AGENT_MODEL_VARS.values():
        assert variable in script, variable
    # Switching back to a shared model prefills from an override.
    assert 'existing_model=$(get_env "$name")' in script
    # The provider is asked once and prefixed onto every override, so the
    # script cannot produce the mixed-provider configuration check-config
    # rejects.
    assert 'set_env "$name" "${provider}/${override}"' in script
    assert 'unset_env "$name"' in script


def test_docs_explain_provisioning_instead_of_the_installer() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    docs = (REPO_ROOT / "docs" / "DOCKER.md").read_text()

    assert "docker/.env" in docs or ".env" in docs
    assert "./agent-init.sh" in readme
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


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def _triggers() -> dict:
    workflow = _workflow()
    # PyYAML resolves the bare key `on` to the boolean True.
    return workflow.get("on") or workflow.get(True) or {}


def test_workflow_tests_pull_requests_on_the_same_inputs_as_main() -> None:
    triggers = _triggers()

    assert "pull_request" in triggers, "contributions get no CI without this"
    assert triggers["pull_request"]["branches"] == ["main"]
    # The paths that rebuild the image must be the paths a pull request tests.
    assert set(triggers["pull_request"]["paths"]) == set(triggers["push"]["paths"])


def test_publish_never_runs_for_a_pull_request() -> None:
    publish = _workflow()["jobs"]["publish"]
    condition = " ".join(publish.get("if", "").split())

    # A job without a condition also runs on pull_request events, where the
    # Docker Hub credentials are not available and must not be reachable.
    assert condition, "publish needs an `if` that excludes pull_request"
    assert "pull_request" not in condition
    assert "github.event_name == 'push'" in condition
    assert "github.event_name == 'workflow_dispatch'" in condition


def test_the_test_job_needs_no_secrets() -> None:
    # Fork pull requests run this job without secrets, so it must not read any.
    job = yaml.safe_dump(_workflow()["jobs"]["test"])

    assert "secrets." not in job
    assert "DOCKERHUB" not in job
