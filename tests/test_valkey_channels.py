"""C1 — Django Valkey channel shapes match theorem-valkey-client doctrine."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from apps.keys.valkey_publish import (
    apikey_revoke_channel,
    apikey_revoke_channel_global,
    publish_key_revocation,
)
from apps.tenancy.tenant_cache import membership_invalidate_channel


def test_apikey_revoke_channel_matches_rust_doctrine():
    assert apikey_revoke_channel("Travis-Gilbert") == "t:Travis-Gilbert:control_apikey:revoke"
    assert apikey_revoke_channel_global() == "t:_global:control_apikey:revoke"
    assert apikey_revoke_channel("") == "t:_global:control_apikey:revoke"


def test_membership_invalidate_channel_matches_rust_doctrine():
    assert (
        membership_invalidate_channel("Travis-Gilbert")
        == "t:Travis-Gilbert:control_apikey:membership_invalidate"
    )


def test_publish_key_revocation_uses_tenant_channel_and_bare_key_id():
    mock_client = MagicMock()
    mock_redis = MagicMock()
    mock_redis.from_url.return_value = mock_client
    with (
        patch("apps.keys.valkey_publish.settings") as settings,
        patch.dict("sys.modules", {"redis": mock_redis}),
    ):
        settings.VALKEY_URL = "redis://127.0.0.1:6379/0"
        publish_key_revocation("key-abc", tenant_slug="Travis-Gilbert")
    mock_client.publish.assert_called_once_with(
        "t:Travis-Gilbert:control_apikey:revoke",
        "key-abc",
    )


def test_publish_key_revocation_falls_back_to_global_without_tenant():
    mock_client = MagicMock()
    mock_redis = MagicMock()
    mock_redis.from_url.return_value = mock_client
    with (
        patch("apps.keys.valkey_publish.settings") as settings,
        patch.dict("sys.modules", {"redis": mock_redis}),
    ):
        settings.VALKEY_URL = "redis://127.0.0.1:6379/0"
        publish_key_revocation("key-xyz")
    mock_client.publish.assert_called_once_with(
        "t:_global:control_apikey:revoke",
        "key-xyz",
    )


def test_legacy_channel_names_are_gone():
    """Guard against regressing to pre-doctrine channel strings."""
    root = Path(__file__).resolve().parents[1]
    revoke_src = (root / "apps/keys/valkey_publish.py").read_text(encoding="utf-8")
    membership_src = (root / "apps/tenancy/tenant_cache.py").read_text(encoding="utf-8")
    assert "theorem:apikey:revoke" not in revoke_src
    assert "control:membership:invalidate" not in membership_src
