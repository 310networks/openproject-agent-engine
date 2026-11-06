"""The model provider table and its three consumers.

``AGENT_MODEL`` is ``<provider>/<model>``; the provider selects the credential
variable, and a per-agent model may select a different provider. The table is
defined once in :mod:`op_agent_engine.providers` and is used by the runner's
environment allowlist and by ``check-config``. The interactive setup script
carries a copy (it has to prompt before the image runs), so both copies are
compared here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from op_agent_engine.providers import (
    PROVIDER_ENV_VARS,
    credential_status,
    credential_variables,
    provider_of,
    stored_credential_providers,
)
from op_agent_engine.runners.generic import _INHERITED_ENV_VARS

DOCKER_DIR = Path(__file__).resolve().parents[1] / "docker"
SCRIPT = DOCKER_DIR / "agent-init.sh"


def _script_table() -> dict[str, tuple[str, ...]]:
    text = SCRIPT.read_text()
    match = re.search(
        r"provider_table=\$\(cat <<'EOF'\n(.*?)\nEOF", text, re.DOTALL
    )
    assert match, "agent-init.sh: provider table not found"
    table: dict[str, tuple[str, ...]] = {}
    for line in match.group(1).splitlines():
        name, *variables = line.split()
        table[name] = () if variables == ["-"] else tuple(variables)
    return table


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("openrouter/deepseek/deepseek-v4.1-flash-1017", "openrouter"),
        ("anthropic/claude-sonnet-4-5", "anthropic"),
        ("OpenRouter/some-model", "openrouter"),
        ("llama.cpp/qwen2.5-coder-7b", "llama.cpp"),
        # A bare model id has no provider prefix.
        ("sonnet", None),
        # Nothing before or after the slash is not a provider reference.
        ("/model", None),
        ("provider/", None),
        ("", None),
        (None, None),
    ],
)
def test_provider_of(model: str | None, expected: str | None) -> None:
    assert provider_of(model) == expected


def test_credential_status_accepts_any_credential_of_the_provider() -> None:
    level, message = credential_status(
        ["anthropic/claude-sonnet-4-5"], {"ANTHROPIC_AUTH_TOKEN": "sk-ant-oauth"}
    )

    assert level == "ok"
    assert "ANTHROPIC_AUTH_TOKEN" in message


def test_credential_status_names_the_missing_variables_only() -> None:
    level, message = credential_status(
        ["openrouter/deepseek/deepseek-v4.1-flash-1017"], {}
    )

    assert level == "error"
    assert "OPENROUTER_API_KEY" in message


def test_credential_status_never_echoes_the_secret() -> None:
    _, message = credential_status(
        ["openrouter/deepseek/deepseek-v4.1-flash-1017"],
        {"OPENROUTER_API_KEY": "sk-or-super-secret"},
    )

    assert "sk-or-super-secret" not in message


@pytest.mark.parametrize("model", [None, "", "sonnet", "not-a-provider/model"])
def test_credential_status_only_warns_when_it_cannot_know(model: str | None) -> None:
    level, _ = credential_status([model], {})

    assert level == "warn"


def test_llama_cpp_needs_only_its_router_url() -> None:
    level, message = credential_status(
        ["llama.cpp/qwen2.5-coder-7b"],
        {"LLAMA_BASE_URL": "http://192.168.1.10:8080"},
    )

    assert level == "ok"
    assert "LLAMA_BASE_URL" in message


def test_llama_cpp_accepts_the_optional_api_key() -> None:
    level, message = credential_status(
        ["llama.cpp/qwen2.5-coder-7b"],
        {
            "LLAMA_BASE_URL": "http://192.168.1.10:8080",
            "LLAMA_API_KEY": "llama-secret",
        },
    )

    assert level == "ok"
    assert "LLAMA_BASE_URL" in message
    assert "LLAMA_API_KEY" in message
    assert "llama-secret" not in message


def test_llama_cpp_requires_the_router_url_even_with_an_api_key() -> None:
    level, message = credential_status(
        ["llama.cpp/qwen2.5-coder-7b"], {"LLAMA_API_KEY": "llama-secret"}
    )

    assert level == "error"
    assert "LLAMA_BASE_URL" in message
    assert "llama-secret" not in message


def test_interactive_only_providers_warn() -> None:
    level, message = credential_status(["openai-codex/gpt-6.1-sol"], {})

    assert level == "warn"
    assert "interactive login" in message


def test_a_stored_auth_json_credential_satisfies_a_provider(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text(
        '{"openai-codex": {"type": "oauth", "access": "secret"}}'
    )
    stored = stored_credential_providers(tmp_path)

    level, message = credential_status(
        ["openai-codex/gpt-6.1-sol"], {}, stored_providers=stored
    )

    assert level == "ok"
    assert "stored auth.json credential" in message
    assert "secret" not in message


def test_a_stored_credential_satisfies_an_api_key_provider(tmp_path: Path) -> None:
    # The modern ``openai`` provider supports both an API key and a ChatGPT
    # subscription OAuth login; the stored credential wins.
    (tmp_path / "auth.json").write_text('{"openai": {"type": "oauth"}}')

    level, message = credential_status(
        ["openai/gpt-5.6-sol"],
        {},
        stored_providers=stored_credential_providers(tmp_path),
    )

    assert level == "ok"
    assert "provider 'openai': using a stored auth.json credential" in message


@pytest.mark.parametrize(
    "content",
    [None, "", "not json", "[]", '"string"'],
)
def test_stored_credentials_tolerate_a_missing_or_bad_file(
    tmp_path: Path, content: str | None
) -> None:
    if content is not None:
        (tmp_path / "auth.json").write_text(content)

    assert stored_credential_providers(tmp_path) == set()


def test_stored_credentials_without_an_agent_dir() -> None:
    assert stored_credential_providers(None) == set()


def test_unknown_provider_has_no_variables() -> None:
    assert credential_variables("nope") == ()


def test_agents_may_use_different_models_of_one_provider() -> None:
    level, message = credential_status(
        [
            "openrouter/deepseek/deepseek-v4.1-flash-1017",
            "openrouter/anthropic/claude-sonnet-4-5",
            "openrouter/moonshotai/kimi-k2.6",
        ],
        {"OPENROUTER_API_KEY": "sk-or-test"},
    )

    assert level == "ok"
    assert "OPENROUTER_API_KEY" in message


def test_each_provider_needs_its_own_credentials() -> None:
    level, message = credential_status(
        ["openrouter/deepseek/deepseek-v4.1-flash-1017", "anthropic/claude-sonnet-4-5"],
        {"OPENROUTER_API_KEY": "sk-or-test", "ANTHROPIC_API_KEY": "sk-ant-test"},
    )

    assert level == "ok"
    assert "provider 'openrouter': using OPENROUTER_API_KEY" in message
    assert "provider 'anthropic': using ANTHROPIC_API_KEY" in message


def test_a_mixed_provider_deployment_reports_the_missing_one() -> None:
    level, message = credential_status(
        ["openrouter/deepseek/deepseek-v4.1-flash-1017", "anthropic/claude-sonnet-4-5"],
        {"OPENROUTER_API_KEY": "sk-or-test"},
    )

    assert level == "error"
    assert "provider 'anthropic' needs one of ANTHROPIC_API_KEY" in message
    assert "provider 'openrouter': using OPENROUTER_API_KEY" in message
    assert "sk-or-test" not in message


def test_a_mixed_provider_deployment_supports_a_subscription() -> None:
    level, message = credential_status(
        ["openrouter/deepseek/deepseek-v4.1-flash-1017", "anthropic/claude-sonnet-4-5"],
        {
            "OPENROUTER_API_KEY": "sk-or-test",
            "ANTHROPIC_OAUTH_TOKEN": "sk-ant-oat-test",
        },
    )

    assert level == "ok"
    assert "ANTHROPIC_OAUTH_TOKEN" in message
    assert "sk-ant-oat-test" not in message


def test_no_configured_model_warns() -> None:
    level, message = credential_status([], {})

    assert level == "warn"
    assert "no model is configured" in message


def test_script_provider_table_matches_the_engine() -> None:
    """agent-init.sh asks for exactly the keys the runner will forward."""
    assert _script_table() == dict(PROVIDER_ENV_VARS)


def test_every_provider_credential_is_forwarded_to_the_agent() -> None:
    for provider, variables in PROVIDER_ENV_VARS.items():
        for name in variables:
            assert name in _INHERITED_ENV_VARS, f"{provider}: {name}"
