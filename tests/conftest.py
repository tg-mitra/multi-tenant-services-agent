import pytest


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Every test writes to its own temporary state folder, never the real ./state."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    # No tenant tokens by default: a test that does not mock the member API never reaches a real one.
    monkeypatch.setenv("TENANT_TOKEN_PLAN_A", "")
    monkeypatch.setenv("TENANT_TOKEN_PLAN_B", "")
    return tmp_path / "state"
