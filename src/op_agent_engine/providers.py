"""Model providers and the environment variables that authenticate them.

The pi agent reads its provider credentials from the environment: no
``auth.json``, ``settings.json`` or ``models.json`` is mounted. This table is
the single definition of "which variables belong to which provider". It backs
the runner's environment allowlist (:mod:`op_agent_engine.runners.generic`) and
``check-config``'s credential check.

``AGENT_MODEL`` is a ``<provider>/<model>`` reference, so the provider is the
part before the first ``/`` and the credential variables are looked up here.
Every agent gets its provider from its own model reference: ``AGENT_MODEL``
sets the deployment default and ``AGENT_MODEL_<AGENT>`` may name a different
provider for one agent. The deployment therefore stores one credential set per
provider it uses. The interactive setup script (``docker/agent-init.sh``)
carries the same table because it has to prompt before the image is running;
``tests/test_providers.py`` keeps the two copies in step.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path

#: Provider id -> credential environment variables. The first entry is normally
#: the provider's primary API key; the rest are endpoints, project ids or the
#: variables of an alternative credential source. A local provider can make its
#: endpoint the first entry instead (``llama.cpp``); see
#: :data:`PROVIDER_REQUIRED_ENV_VARS`. An empty tuple means the provider
#: authenticates interactively (``/login``), which an unattended container run
#: cannot do.
PROVIDER_ENV_VARS: Mapping[str, tuple[str, ...]] = {
    "amazon-bedrock": (
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
    ),
    "ant-ling": ("ANT_LING_API_KEY",),
    "anthropic": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_OAUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
    ),
    "azure": (
        "AZURE_OPENAI_API_KEY",
        "AZURE_OPENAI_ENDPOINT",
        "AZURE_OPENAI_BASE_URL",
        "AZURE_OPENAI_RESOURCE_NAME",
        "AZURE_OPENAI_API_VERSION",
        "AZURE_OPENAI_DEPLOYMENT_NAME_MAP",
    ),
    "baseten": ("BASETEN_API_KEY",),
    "cerebras": ("CEREBRAS_API_KEY",),
    "cloudflare-ai-gateway": (
        "CLOUDFLARE_API_KEY",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_GATEWAY_ID",
    ),
    "cloudflare-workers-ai": ("CLOUDFLARE_API_KEY", "CLOUDFLARE_ACCOUNT_ID"),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "fireworks": ("FIREWORKS_API_KEY",),
    "github-copilot": ("COPILOT_GITHUB_TOKEN",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "google-vertex": (
        "GOOGLE_CLOUD_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ),
    "groq": ("GROQ_API_KEY",),
    "huggingface": ("HF_TOKEN",),
    "kimi-coding": ("KIMI_API_KEY",),
    # Pi's built-in llama.cpp provider: a local (or LAN-exposed) router server.
    # The URL selects the server and the API key is optional; Pi falls back to
    # the literal key ``local`` for an unauthenticated server.
    "llama.cpp": ("LLAMA_BASE_URL", "LLAMA_API_KEY"),
    "meta": ("META_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "minimax-cn": ("MINIMAX_CN_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "moonshotai": ("MOONSHOT_API_KEY",),
    "moonshotai-cn": ("MOONSHOT_API_KEY",),
    "nvidia": ("NVIDIA_API_KEY",),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    # ChatGPT subscription. Pi exposes no environment variable for this
    # provider: ``pi /login`` stores an OAuth credential in auth.json. An
    # unattended run cannot log in, but a one-time interactive login in the
    # persistent agent directory (the ``pi-agent`` volume) is usable by later
    # runs.
    "openai-codex": (),
    "opencode": ("OPENCODE_API_KEY",),
    "opencode-go": ("OPENCODE_API_KEY",),
    "openrouter": ("OPENROUTER_API_KEY",),
    "qwen-token-plan": ("QWEN_TOKEN_PLAN_API_KEY",),
    "qwen-token-plan-cn": ("QWEN_TOKEN_PLAN_CN_API_KEY",),
    "qwen-token-plan-individual": ("QWEN_TOKEN_PLAN_API_KEY",),
    "radius": ("RADIUS_API_KEY",),
    "together": ("TOGETHER_API_KEY",),
    "typesafe": ("TYPESAFE_API_KEY",),
    "vercel-ai-gateway": ("AI_GATEWAY_API_KEY",),
    "xai": ("XAI_API_KEY",),
    "xiaomi": ("XIAOMI_API_KEY",),
    "xiaomi-token-plan-ams": ("XIAOMI_TOKEN_PLAN_AMS_API_KEY",),
    "xiaomi-token-plan-cn": ("XIAOMI_TOKEN_PLAN_CN_API_KEY",),
    "xiaomi-token-plan-sgp": ("XIAOMI_TOKEN_PLAN_SGP_API_KEY",),
    "zai": ("ZAI_API_KEY",),
    "zai-coding-cn": ("ZAI_CODING_CN_API_KEY",),
}

#: Providers whose first variable is not enough: these must be present even
#: when another variable of the provider is set. ``llama.cpp`` is selected by
#: its router URL, so an API key alone cannot make the provider usable.
PROVIDER_REQUIRED_ENV_VARS: Mapping[str, tuple[str, ...]] = {
    "llama.cpp": ("LLAMA_BASE_URL",),
}

#: Variables that are not tied to a model provider but that the agents need:
#: ``gh`` pushes and opens pull requests with them.
EXTRA_AGENT_ENV_VARS: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN")


def provider_of(model: str | None) -> str | None:
    """Return the provider prefix of a model reference, if it has one.

    ``openrouter/deepseek/deepseek-v4.1-flash-1017`` belongs to the
    ``openrouter`` provider: only the first ``/`` separates the provider from
    the model id, which may contain slashes itself.
    """
    if not model or "/" not in model:
        return None
    prefix, model_id = model.split("/", 1)
    prefix = prefix.strip().lower()
    if not prefix or not model_id.strip():
        return None
    return prefix


def credential_variables(provider: str) -> tuple[str, ...]:
    """The credential variables for a provider (empty if it has none)."""
    return PROVIDER_ENV_VARS.get(provider.strip().lower(), ())


def stored_credential_providers(agent_dir: Path | str | None) -> set[str]:
    """Provider ids with a stored credential in ``agent_dir/auth.json``.

    ``pi /login`` writes API keys and OAuth credentials there, including the
    ChatGPT subscription token that has no environment variable. Only the
    top-level keys (provider ids) are read; values are never returned or
    logged. A missing, unreadable or malformed file yields an empty set.
    """
    if agent_dir is None:
        return set()
    try:
        raw = json.loads((Path(agent_dir) / "auth.json").read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(raw, dict):
        return set()
    return {
        str(provider).strip().lower()
        for provider in raw
        if str(provider).strip()
    }


#: Severity ordering used to combine one status per provider.
_LEVEL_ORDER = {"ok": 0, "warn": 1, "error": 2}


def credential_status(
    models: Iterable[str | None],
    env: Mapping[str, str],
    *,
    stored_providers: Iterable[str] = (),
) -> tuple[str, str]:
    """Describe whether every configured model has credentials.

    ``models`` is every model reference in use (``AGENT_MODEL`` and the
    per-agent overrides). Each model carries its own provider, so a deployment
    may mix providers; each provider present needs its own credentials. A
    provider with a stored ``auth.json`` entry (see
    :func:`stored_credential_providers`) is satisfied even when it has no
    environment variable. The overall level is the most severe per-provider
    level, and the message names every provider in a deterministic order. Only
    variable names are ever reported, never values.
    """
    stored = {
        provider.strip().lower()
        for provider in stored_providers
        if provider and provider.strip()
    }
    configured = list(
        dict.fromkeys(
            model.strip() for model in models if model and model.strip()
        )
    )
    if not configured:
        return (
            "warn",
            "no model is configured (AGENT_MODEL and the AGENT_MODEL_<AGENT> "
            "overrides are unset): pi's own default is used, so its "
            "provider credentials are not checked",
        )

    reports: list[tuple[str, str]] = []
    seen: set[str] = set()
    for model in configured:
        provider = provider_of(model)
        if provider is None:
            reports.append(
                (
                    "warn",
                    f"model {model!r} has no <provider>/ prefix, so its "
                    "provider credentials are not checked",
                )
            )
            continue
        if provider in seen:
            continue
        seen.add(provider)
        reports.append(
            _provider_credential_status(provider, env, stored=provider in stored)
        )

    level = max((level for level, _ in reports), key=_LEVEL_ORDER.__getitem__)
    message = "; ".join(message for _, message in reports)
    return level, message


def _provider_credential_status(
    provider: str, env: Mapping[str, str], *, stored: bool
) -> tuple[str, str]:
    """The credential status for one provider (``ok``, ``warn`` or ``error``)."""
    variables = credential_variables(provider)
    required = PROVIDER_REQUIRED_ENV_VARS.get(provider, ())
    missing_required = [
        name for name in required if not env.get(name, "").strip()
    ]
    if missing_required:
        return (
            "error",
            f"provider {provider!r} requires "
            f"{', '.join(missing_required)}; its variables are "
            f"{', '.join(variables)}",
        )

    if stored:
        return (
            "ok",
            f"provider {provider!r}: using a stored auth.json credential",
        )

    if not variables:
        return (
            "warn",
            f"provider {provider!r} has no environment variable: it uses an "
            "interactive login stored in auth.json. A one-time login in the "
            "persistent agent directory works for later runs; its "
            "credentials are not checked here",
        )

    present = [name for name in variables if env.get(name, "").strip()]
    if present:
        return ("ok", f"provider {provider!r}: using {', '.join(present)}")
    return (
        "error",
        f"provider {provider!r} needs one of "
        f"{', '.join(variables)}; none of them is set",
    )
