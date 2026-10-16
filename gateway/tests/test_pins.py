"""Offline checks on the gateway's pins; no gateway needed.

- LiteLLM's version and the price-list commit agree across Terraform, the
  local stack and CI.
- The pinned price list prices the default model of every provider the
  gateway serves (a "gateway" block in server/config/llm_defaults.json),
  the default speech models of the providers whose speech it serves
  ("gateway": true in server/config/speech_defaults.json) and OpenAI's
  default embedding model, so OpenCompany's defaults are never refused as
  unpriced.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx
import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TERRAFORM = (REPO / "gateway/infra/terraform/main.tf").read_text(encoding="utf-8")
VARIABLES = (REPO / "gateway/infra/terraform/variables.tf").read_text(encoding="utf-8")
GATEWAY_CONFIG = yaml.safe_load((REPO / "gateway/litellm/config.yaml").read_text(encoding="utf-8"))
LOCAL_CONFIG = yaml.safe_load((REPO / "gateway/local/config.local.yaml").read_text(encoding="utf-8"))
COMPOSE = yaml.safe_load((REPO / "gateway/local/docker-compose.yml").read_text(encoding="utf-8"))
CI = yaml.safe_load((REPO / ".github/workflows/gateway-ci.yml").read_text(encoding="utf-8"))
LITELLM = COMPOSE["services"]["litellm"]

# The providers the gateway serves are the ones with a "gateway" block in
# server/config/llm_defaults.json (OpenCompany's services/gateway/routing.py).
PROVIDERS = json.loads((REPO / "server/config/llm_defaults.json").read_text(encoding="utf-8"))["providers"]
SERVED = {name: config["gateway"] for name, config in PROVIDERS.items() if isinstance(config.get("gateway"), dict)}
SPEECH = json.loads((REPO / "server/config/speech_defaults.json").read_text(encoding="utf-8"))["providers"]
SPEECH_SERVED = {name: block for name, block in SPEECH.items() if block.get("gateway") is True}
# DEFAULT_EMBEDDING_MODELS["openai"] in server/services/memory/vector_store.py.
EMBEDDING_MODEL = "text-embedding-3-small"


def _terraform_string(name: str) -> str:
    match = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', TERRAFORM, re.MULTILINE)
    assert match, f"{name} not found in gateway/infra/terraform/main.tf"
    return match.group(1)


def test_pins_agree() -> None:
    version = _terraform_string("litellm_version")
    module = re.search(r'source\s*=\s*"BerriAI/litellm/aws"\s*\n\s*version\s*=\s*"([^"]+)"', TERRAFORM)
    assert module and module.group(1) == version, "module version and local.litellm_version differ in main.tf"
    assert LITELLM["image"].endswith(f":v{version}"), LITELLM["image"]
    assert CI["env"]["LITELLM_VERSION"] == version, CI["env"]
    assert LITELLM["environment"]["LITELLM_MODEL_COST_MAP_URL"] == _terraform_string("LITELLM_MODEL_COST_MAP_URL")


def _price_overrides(config: dict) -> dict[str, dict]:
    return {
        deployment["model_name"]: {key: value for key, value in deployment["litellm_params"].items() if "cost_per" in key}
        for deployment in config["model_list"]
        if any("cost_per" in key for key in deployment["litellm_params"])
    }


def test_price_overrides_agree() -> None:
    """The contract tests run on the local config, so it must bill as the deployed one does."""
    overrides = _price_overrides(GATEWAY_CONFIG)
    assert overrides, "gateway/litellm/config.yaml has no price overrides"
    assert _price_overrides(LOCAL_CONFIG) == overrides


def test_every_vendor_key_has_a_secret() -> None:
    """ECS injects only the secrets Terraform creates (secret_env_names)."""
    block = re.search(r'variable "secret_env_names".*?default\s*=\s*\[(.*?)\]', VARIABLES, re.DOTALL)
    assert block, "secret_env_names not found in gateway/infra/terraform/variables.tf"
    secrets = set(re.findall(r'"([A-Z0-9_]+)"', block.group(1)))
    keys = {
        str(deployment["litellm_params"]["api_key"]).removeprefix("os.environ/")
        for deployment in GATEWAY_CONFIG["model_list"]
        if str(deployment["litellm_params"].get("api_key", "")).startswith("os.environ/")
    }
    assert keys <= secrets, f"vendor keys with no secret: {sorted(keys - secrets)}"


def test_gemini_runs_on_vertex_with_the_agent_platform_key() -> None:
    """An Agent Platform key (AQ.) works on Vertex AI only; the Gemini Developer API refuses it."""
    gemini = [d["litellm_params"] for d in GATEWAY_CONFIG["model_list"] if d["model_name"] in ("gemini/*", "gemini-*")]
    assert len(gemini) == 2, gemini
    for params in gemini:
        assert params["api_key"] == "os.environ/GOOGLE_AGENT_API_KEY", params
        assert params["api_base"] == "https://aiplatform.googleapis.com/v1/publishers/google", params


@pytest.fixture(scope="module")
def prices() -> dict:
    url = LITELLM["environment"]["LITELLM_MODEL_COST_MAP_URL"]
    assert re.search(r"/[0-9a-f]{40}/", url), "the price list must be pinned to a commit"
    try:
        return httpx.get(url, timeout=60).raise_for_status().json()
    except httpx.HTTPError as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"price list unreachable: {exc}")


def _priced(prices: dict, *candidates: str) -> bool:
    """LiteLLM prices a model under its wire name or its bare id, and counts
    it as priced when that entry has any cost_per field: per token, per
    character (tts-1) or per second (Whisper). The same test refuses
    unpriced models (proxy/auth/auth_checks.py)."""
    entry = next((prices[key] for key in candidates if key in prices), {})
    return any("cost_per" in key for key in entry)


def test_served_default_models_are_priced(prices: dict) -> None:
    assert SERVED, "no provider in llm_defaults.json has a gateway block"
    unpriced = []
    for provider, block in SERVED.items():
        model = PROVIDERS[provider]["default_model"]
        # The native Gemini route's wire name is bare; its price is under gemini/<id>.
        if not _priced(prices, block.get("model_prefix", "") + model, model, f"{provider}/{model}"):
            unpriced.append(f"{provider}: {model}")
    assert not unpriced, f"served default models with no price in the pinned list: {unpriced}"


def test_served_speech_and_embedding_defaults_are_priced(prices: dict) -> None:
    # The route (URL and prefix) of a speech provider is its llm_defaults.json gateway block.
    assert SPEECH_SERVED and set(SPEECH_SERVED) <= set(SERVED), SPEECH_SERVED.keys()
    unpriced = []
    for provider, block in SPEECH_SERVED.items():
        prefix = SERVED[provider].get("model_prefix", "")
        for direction in ("tts", "stt"):
            model = (block.get(direction) or {}).get("default_model")
            if model and not _priced(prices, prefix + model, model):
                unpriced.append(f"{provider} {direction}: {model}")
    if not _priced(prices, SERVED["openai"]["model_prefix"] + EMBEDDING_MODEL, EMBEDDING_MODEL):
        unpriced.append(f"openai embeddings: {EMBEDDING_MODEL}")
    assert not unpriced, f"served speech and embedding defaults with no price in the pinned list: {unpriced}"
