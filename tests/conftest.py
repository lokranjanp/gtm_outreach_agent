import pytest

from outreach.config import CONFIG_NAMES


@pytest.fixture(autouse=True)
def isolate_private_configuration(monkeypatch):
    """Tests must never read real keys or make configured production calls."""
    for name in CONFIG_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("outreach.config.dotenv_values", lambda path: {})
