"""One configuration boundary for the runner and UI; secrets never enter graph state."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODELS = {"gemini": "gemini-3.8-flash", "openai": "gpt-5-mini"}
SECRET_NAMES = ("GOOGLE_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY")
CONFIG_NAMES = (*SECRET_NAMES, "OUTREACH_PROVIDER", "OUTREACH_MODEL", "OUTREACH_DATA_DIR")


@dataclass(frozen=True)
class RuntimeConfig:
    provider: str
    model: str
    model_key: str = field(repr=False)
    tavily_key: str = field(repr=False)
    data_dir: str

    @property
    def issues(self) -> list[str]:
        if self.provider not in DEFAULT_MODELS:
            return ["OUTREACH_PROVIDER must be gemini or openai."]
        if self.provider == "gemini" and not self.model.startswith("gemini-"):
            return ["OUTREACH_MODEL must be a Gemini model when OUTREACH_PROVIDER=gemini."]
        if self.provider == "openai" and self.model.startswith("gemini-"):
            return ["Choose an OpenAI model when OUTREACH_PROVIDER=openai."]
        return []

    @property
    def missing_credentials(self) -> list[str]:
        missing = []
        if not self.model_key:
            missing.append(
                "GOOGLE_API_KEY (or GEMINI_API_KEY)"
                if self.provider == "gemini"
                else "OPENAI_API_KEY"
            )
        if not self.tavily_key:
            missing.append("TAVILY_API_KEY")
        return missing

    @property
    def ready(self) -> bool:
        return not self.issues and not self.missing_credentials

    @property
    def problem(self) -> str:
        if self.issues:
            return " ".join(self.issues)
        return (
            "Configure " + " and ".join(self.missing_credentials) + " in .env or Streamlit secrets."
        )


def resolve_config(
    credentials: Mapping | None = None,
    *,
    environ: Mapping | None = None,
    env_path: str | Path | None = PROJECT_ROOT / ".env",
) -> RuntimeConfig:
    # Read .env on each invocation: edits take effect without polluting os.environ.
    sources = [
        credentials or {},
        os.environ if environ is None else environ,
        dotenv_values(env_path) if env_path is not None else {},
    ]

    def value(*names: str) -> str:
        for source in sources:
            for name in names:
                candidate = source.get(name)
                if candidate is not None and str(candidate).strip():
                    return str(candidate).strip()
        return ""

    provider = (value("OUTREACH_PROVIDER") or "gemini").lower()
    model = (value("OUTREACH_MODEL") or DEFAULT_MODELS.get(provider, "")).removeprefix("models/")
    key = (
        value("GOOGLE_API_KEY", "GEMINI_API_KEY")
        if provider == "gemini"
        else value("OPENAI_API_KEY")
    )
    return RuntimeConfig(
        provider=provider,
        model=model,
        model_key=key,
        tavily_key=value("TAVILY_API_KEY"),
        data_dir=value("OUTREACH_DATA_DIR") or str(PROJECT_ROOT / "data"),
    )
