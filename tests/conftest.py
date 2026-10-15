import pytest

import app.ai as ai


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    """Tests never wait between AI retries."""
    monkeypatch.setattr(ai, "RETRY_DELAYS", ())
