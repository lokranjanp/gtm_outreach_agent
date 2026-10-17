import asyncio

import pytest

from outreach.brain import GeminiBrain, OpenAIBrain
from outreach.config import resolve_config
from outreach.demo import DemoBrain, DemoResearch
from outreach.runner import MissingCredentials, Runner
from outreach.storage import Store


def config(values=None, **kwargs):
    return resolve_config(environ=values or {}, env_path=None, **kwargs)


@pytest.mark.parametrize("key_name", ["GOOGLE_API_KEY", "GEMINI_API_KEY"])
def test_gemini_is_default_and_needs_no_openai_key(key_name):
    result = config({key_name: "test-gemini", "TAVILY_API_KEY": "test-tavily"})
    assert result.ready
    assert result.provider == "gemini" and result.model == "gemini-3.8-flash"
    assert result.model_key == "test-gemini"


def test_alias_and_configuration_source_precedence():
    result = config(
        {"GOOGLE_API_KEY": "process-key", "GEMINI_API_KEY": "other-process-key"},
        credentials={"GEMINI_API_KEY": "secret-key", "TAVILY_API_KEY": "secret-tavily"},
    )
    assert result.model_key == "secret-key"
    assert config({"GOOGLE_API_KEY": "primary", "GEMINI_API_KEY": "alias"}).model_key == "primary"


def test_openai_remains_explicitly_selectable():
    result = config(
        {
            "OUTREACH_PROVIDER": "openai",
            "OPENAI_API_KEY": "test-openai",
            "TAVILY_API_KEY": "test-tavily",
        }
    )
    assert result.ready and result.model == "gpt-5-mini"


@pytest.mark.parametrize(
    "values",
    [
        {"OUTREACH_PROVIDER": "unknown"},
        {"OUTREACH_MODEL": "gpt-5-mini"},
        {"OUTREACH_PROVIDER": "openai", "OUTREACH_MODEL": "gemini-3.8-flash"},
    ],
)
def test_invalid_provider_model_combination_disables_live_calls(values):
    result = config(values)
    assert not result.ready
    assert result.issues


def test_missing_keys_and_secret_redaction():
    result = config({"TAVILY_API_KEY": "private-tavily"})
    assert result.missing_credentials == ["GOOGLE_API_KEY (or GEMINI_API_KEY)"]
    assert "OPENAI" not in result.problem
    assert "private-tavily" not in repr(result)
    assert "private-tavily" not in result.problem


def test_env_file_is_reread_without_changing_process_environment(tmp_path, monkeypatch):
    from outreach import config as module

    path = tmp_path / ".env"
    values = {"TAVILY_API_KEY": "first-key", "GEMINI_API_KEY": "model-key"}
    monkeypatch.setattr(module, "dotenv_values", lambda filename: dict(values))
    process = {}
    first = resolve_config(environ=process, env_path=path)
    values["TAVILY_API_KEY"] = "updated-key"
    second = resolve_config(environ=process, env_path=path)
    assert first.tavily_key == "first-key" and second.tavily_key == "updated-key"
    assert process == {}


@pytest.mark.parametrize("provider,brain_type", [("gemini", GeminiBrain), ("openai", OpenAIBrain)])
def test_runner_uses_selected_provider_and_same_tavily_key(
    tmp_path, monkeypatch, provider, brain_type
):
    from outreach import runner as module

    selected = config(
        {
            "OUTREACH_PROVIDER": provider,
            "GOOGLE_API_KEY": "gemini-key",
            "OPENAI_API_KEY": "openai-key",
            "TAVILY_API_KEY": "tavily-key",
        }
    )
    monkeypatch.setattr(module, "resolve_config", lambda credentials: selected)
    brain, research = Runner(Store(tmp_path)).services("live")
    assert isinstance(brain, brain_type)
    assert research.api_key == "tavily-key" and research.brain is brain


def test_missing_credentials_allow_saved_run_reading_and_demo(tmp_path, monkeypatch):
    from outreach import runner as module

    monkeypatch.setattr(module, "resolve_config", lambda credentials: config())
    runner = Runner(Store(tmp_path))
    brain, research = runner.services("live")
    assert isinstance(brain, MissingCredentials)
    with pytest.raises(ValueError, match="GOOGLE_API_KEY"):
        asyncio.run(brain.complete(None, "", {}))
    demo_brain, demo_research = runner.services("demo")
    assert isinstance(demo_brain, DemoBrain) and isinstance(demo_research, DemoResearch)
