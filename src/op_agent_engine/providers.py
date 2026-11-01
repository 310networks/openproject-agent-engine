"""Model providers and the environment variables that authenticate them.

The pi agent reads its provider credentials from the environment: no
``auth.json``, ``settings.json`` or ``models.json`` is mounted. This table is
the single definition of "which variables belong to which provider". It backs
the runner's environment allowlist (:mod:`op_agent_engine.runners.generic`) and
``check-config``'s credential check.

``AGENT_MODEL`` is a ``<provider>/<model>`` reference, so the provider is the
part before the first ``/`` and the credential variables are looked up here.
The interactive setup script (``docker/agent-init.sh``) carries the same table
because it has to prompt before the image is running; ``tests/test_providers.py``
keeps the two copies in step.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

#: Provider id -> credential environment variables. The first entry is the
#: provider's primary API key; the rest are endpoints, project ids or the
#: variables of an alternative credential source. An empty tuple means the
#: provider authenticates interactively (``/login``), which an unattended
#: container run cannot do.
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
    "meta": ("META_API_KEY",),
    "minimax": ("MINIMAX_API_KEY",),
    "minimax-cn": ("MINIMAX_CN_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "moonshotai": ("MOONSHOT_API_KEY",),
    "moonshotai-cn": ("MOONSHOT_API_KEY",),
    "nvidia": ("NVIDIA_API_KEY",),
    "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
    # ChatGPT subscription; ``pi /login`` stores a token in auth.json, which a
    # container run cannot provide.
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


def credential_status(
    models: Iterable[str | None], env: Mapping[str, str]
) -> tuple[str, str]:
    """Describe whether the configured models have credentials in ``env``.

    ``models`` is every model reference in use (``AGENT_MODEL`` and the
    per-agent overrides). They may name different models but must share one
    provider, because the deployment stores one provider key. Returns
    ``(level, message)`` with level ``ok``, ``warn`` or ``error``; only
    variable names are ever reported, never values.
    """
    configured = [model.strip() for model in models if model and model.strip()]
    if not configured:
        return (
            "warn",
            "no model is configured (AGENT_MODEL and the AGENT_MODEL_<AGENT> "
            "overrides are unset): pi's own default is used, so its "
            "provider credentials are not checked",
        )

    providers: set[str] = set()
    for model in configured:
        provider = provider_of(model)
        if provider is None:
            return (
                "warn",
                f"model {model!r} has no <provider>/ prefix, so the provider "
                "credentials are not checked",
            )
        providers.add(provider)

    if len(providers) > 1:
        return (
            "error",
            "one model provider per deployment, but the configured models use "
            + ", ".join(sorted(providers)),
        )

    provider = providers.pop()
    variables = credential_variables(provider)
    if not variables:
        return (
            "warn",
            f"provider {provider!r} needs an interactive login, which an "
            "unattended run cannot provide; its credentials are not checked",
        )

    present = [name for name in variables if env.get(name, "").strip()]
    if present:
        return ("ok", f"provider {provider!r}: using {', '.join(present)}")
    return (
        "error",
        f"the configured models use provider {provider!r}, which needs one of "
        f"{', '.join(variables)}; none of them is set",
    )
