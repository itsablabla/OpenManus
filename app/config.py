"""
Application configuration — loads config.toml with environment variable substitution.

Key change from upstream: _load_config now reads the file as text first,
substitutes ${VAR_NAME} placeholders using os.environ, then parses TOML.
This allows config/config.toml to be generated from Railway env vars at startup
via entrypoint.sh — keeping secrets out of git while staying compatible with
the upstream AppConfig / LLMSettings structure.
"""

import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Dict, Optional

from pydantic import BaseModel, model_validator


PACKAGE_ROOT = Path(__file__).parent.parent
CONFIG_ROOT = PACKAGE_ROOT / "config"


def _substitute_env_vars(text: str) -> str:
    """Replace ${VAR_NAME} placeholders in text with os.environ values."""

    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        value = os.environ.get(var_name, "")
        if not value:
            print(
                f"[CONFIG] Warning: environment variable {var_name!r} is not set "
                f"— defaulting to empty string.",
                file=sys.stderr,
            )
        return value

    return re.sub(r"\$\{([^}]+)\}", replacer, text)


def _load_config(config_path: Path) -> dict:
    """Load and parse a TOML config file with env var substitution."""
    raw = config_path.read_text(encoding="utf-8")
    substituted = _substitute_env_vars(raw)
    return tomllib.loads(substituted)


class LLMSettings(BaseModel):
    model: str = "gpt-4o"
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    api_type: str = "openai"
    api_version: str = ""
    max_tokens: int = 8192
    temperature: float = 0.0


class AppConfig(BaseModel):
    llm: Dict[str, LLMSettings] = {}

    @model_validator(mode="before")
    @classmethod
    def build_llm(cls, values: dict) -> dict:
        raw_llm = values.get("llm", {})
        if isinstance(raw_llm, dict):
            # Flatten: if there's a top-level [llm] block (not named sub-blocks),
            # treat it as the "default" profile.
            if raw_llm and not any(isinstance(v, dict) for v in raw_llm.values()):
                values["llm"] = {"default": raw_llm}
        return values


def load_app_config(config_file: str = "config.toml") -> AppConfig:
    config_path = CONFIG_ROOT / config_file

    if not config_path.exists():
        print(
            f"[CONFIG] {config_path} not found — using default LLM config. "
            "Set LLM_MODEL, LLM_BASE_URL, and LLM_API_KEY env vars.",
            file=sys.stderr,
        )
        return AppConfig(
            llm={
                "default": LLMSettings(
                    model=os.environ.get("LLM_MODEL", "gpt-4o"),
                    base_url=os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1"),
                    api_key=os.environ.get("LLM_API_KEY", ""),
                )
            }
        )

    raw = _load_config(config_path)
    config = AppConfig(**raw)

    # Debug: log resolved LLM profiles at startup
    for name, settings in config.llm.items():
        print(
            f"[CONFIG] LLM[{name}] model={settings.model!r}  "
            f"base_url={settings.base_url!r}  api_type={settings.api_type!r}"
        )

    return config
