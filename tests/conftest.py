import pytest


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Every test writes to its own temporary state folder, never the real ./state."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    return tmp_path / "state"
