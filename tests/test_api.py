"""Tests for SolarEdge Web."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.cookies import Morsel
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp

from solaredge_web import SolarEdgeWeb
from solaredge_web.solaredge import _build_opt_to_parent_map, _decode_playback, _to_utc_iso

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text())


def _make_cookie(name: str, value: str, domain: str = "monitoring.solaredge.com", max_age: str = "3600") -> Morsel:
    morsel = Morsel()
    morsel.set(name, value, value)
    morsel["domain"] = domain
    morsel["max-age"] = max_age
    return morsel


def _mock_response(
    json_data=None, text="<html></html>", url="https://monitoring.solaredge.com/mfe/auth/callback?code=test_code", status=200
):
    resp = MagicMock()
    resp.status = status
    resp.url = url
    resp.history = []
    resp.raise_for_status = MagicMock()
    resp.json = AsyncMock(return_value=json_data if json_data is not None else {})
    resp.text = AsyncMock(return_value=text)
    return resp


def _mock_failing_response(status=404):
    resp = MagicMock()
    resp.status = status
    resp.raise_for_status = MagicMock(
        side_effect=aiohttp.ClientResponseError(request_info=MagicMock(), history=(), status=status, message="Not found")
    )
    return resp


def _make_mock_session(cookies=None):
    session = MagicMock()
    session.get = AsyncMock()
    session.post = AsyncMock()
    session.cookie_jar = cookies if cookies is not None else []
    return session


def test_to_utc_iso_naive():
    """Naive datetimes are assumed UTC."""
    assert _to_utc_iso(datetime(2026, 7, 30, 0, 0, 0)) == "2026-07-30T00:00:00Z"


def test_to_utc_iso_aware():
    """Aware datetimes are converted to UTC."""
    dt = datetime(2026, 7, 30, 2, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    assert _to_utc_iso(dt) == "2026-07-30T00:00:00Z"


def test_build_opt_to_parent_map():
    """Verify the optimizer-to-parent-names mapping from the site structure."""
    equipment_json = _load_fixture("equipment.json")
    site_structure = equipment_json["siteStructure"]
    result = _build_opt_to_parent_map(site_structure)
    # Both optimizers are under Test Site > Inverter 1 > String 1 (top-down order).
    # Device IDs: site identifier, inverter serial, string identifier.
    assert result["7A012345"] == ["TESTSITE01", "7E012345-57", "7E012345_31"]
    assert result["7A012346"] == ["TESTSITE01", "7E012345-57", "7E012345_31"]


def test_decode_playback_returns_optimizer_and_aggregated_values():
    """Per-optimizer, string, inverter, and site values are all present."""
    resp = _load_fixture("playback.json")
    equipment_json = _load_fixture("equipment.json")
    site_structure = equipment_json["siteStructure"]
    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)

    result = _decode_playback(resp, start, site_structure)

    assert len(result) == 4
    # Slot 1: opt0=100W, opt1=50W
    #   7A012345-CA (opt)   = 100
    #   7A012346-CA (opt)   = 50
    #   7E012345_31 (string) = 150 (100 + 50)
    #   7E012345-57 (inverter) = 150
    #   TESTSITE01 (site)   = 150
    slot1 = result[1].values
    assert slot1["7A012345-CA"] == 100.0
    assert slot1["7A012346-CA"] == 50.0
    assert slot1["7E012345_31"] == 150.0
    assert slot1["7E012345-57"] == 150.0
    assert slot1["TESTSITE01"] == 150.0

    # Slot 3: opt0=120W, opt1=60W
    slot3 = result[3].values
    assert slot3["7A012345-CA"] == 120.0
    assert slot3["7A012346-CA"] == 60.0
    assert slot3["7E012345_31"] == 180.0
    assert slot3["7E012345-57"] == 180.0
    assert slot3["TESTSITE01"] == 180.0


def test_decode_playback_empty_response():
    """Empty or malformed responses return an empty list."""
    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    assert _decode_playback({}, start, {}) == []
    assert _decode_playback({"timeSlotsCount": 0, "optimizerSerials": ["X"], "compressPowerData": [2.0, 6.0]}, start, {}) == []
    assert _decode_playback({"timeSlotsCount": 4, "optimizerSerials": [], "compressPowerData": [2.0, 6.0]}, start, {}) == []


def test_decode_playback_uses_power_not_normalized():
    """Verify compressPowerData (watts) is used, not compressData (0..1)."""
    resp = _load_fixture("playback.json")
    equipment_json = _load_fixture("equipment.json")
    site_structure = equipment_json["siteStructure"]
    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)

    result = _decode_playback(resp, start, site_structure)

    # compressPowerData slot 1: 100.0, 50.0 (watts) -> 100, 50 Wh
    # compressData slot 1: 0.25, 0.13 (normalized) -> would give 250, 130 if *1000
    assert result[1].values["7A012345-CA"] == 100.0
    assert result[1].values["7A012346-CA"] == 50.0


def test_decode_playback_no_site_structure_uses_short_serials():
    """Without site structure, only short serials appear (no aggregation)."""
    resp = _load_fixture("playback.json")
    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    result = _decode_playback(resp, start, {})
    assert result[1].values == {"7A012345": 100.0, "7A012346": 50.0}


async def test_async_get_equipment_parses_v2_site_structure():
    """async_get_equipment extracts inverters, strings, and optimizers."""
    equipment_json = _load_fixture("equipment.json")

    auth_resp = _mock_response(
        url="https://monitoring.solaredge.com/mfe/auth/callback?code=test_code",
    )
    token_resp = _mock_response(
        json_data={"access_token": "test_token", "expires_in": 3600},
    )
    exchange_resp = _mock_response(json_data={"ok": True})
    equipment_resp = _mock_response(json_data=equipment_json)

    session = _make_mock_session()
    session.get = AsyncMock(side_effect=[auth_resp, equipment_resp])
    session.post = AsyncMock(side_effect=[token_resp, exchange_resp])

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    equipment = await client.async_get_equipment()

    assert "7E012345-57" in equipment
    assert "7A012345-CA" in equipment
    assert equipment["7E012345-57"]["type"] == "INVERTER"
    assert all(e["type"] not in ("FOLDER", "SITE") for e in equipment.values())


async def test_async_get_equipment_caches_result():
    """Second call returns cached equipment without hitting the API."""
    equipment_json = _load_fixture("equipment.json")
    sso_cookie = _make_cookie("SolarEdge_SSO-1.4", "sso_value")

    equipment_resp = _mock_response(json_data=equipment_json)
    session = _make_mock_session(cookies=[sso_cookie])
    session.get = AsyncMock(side_effect=[equipment_resp])

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    await client.async_get_equipment()
    await client.async_get_equipment()

    assert session.get.await_count == 1


async def test_async_get_equipment_skips_login_with_valid_sso():
    """Login is skipped when a valid SSO cookie + auth headers exist."""
    equipment_json = _load_fixture("equipment.json")
    sso_cookie = _make_cookie("SolarEdge_SSO-1.4", "sso_value")

    equipment_resp = _mock_response(json_data=equipment_json)
    session = _make_mock_session(cookies=[sso_cookie])
    session.get = AsyncMock(side_effect=[equipment_resp])
    session.post = AsyncMock()

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    equipment = await client.async_get_equipment()
    assert "7A012345-CA" in equipment
    session.post.assert_not_awaited()


async def test_async_get_equipment_excludes_inactive_by_default():
    """Retired optimizers (INACTIVE) are excluded by default, incl. from cache."""
    equipment_json = _load_fixture("equipment_with_inactive.json")
    sso_cookie = _make_cookie("SolarEdge_SSO-1.4", "sso_value")

    equipment_resp = _mock_response(json_data=equipment_json)
    session = _make_mock_session(cookies=[sso_cookie])
    session.get = AsyncMock(side_effect=[equipment_resp])
    session.post = AsyncMock()

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    # Live replacement and other active optimizers are kept...
    equipment = await client.async_get_equipment()
    assert "7A012345-CA" in equipment
    assert "7A012346-CA" in equipment
    # ...the retired unit sharing its name is not.
    assert "7A012340-CA" not in equipment
    # Devices without a status field (inverter, string) are preserved.
    assert "7E012345-57" in equipment
    assert "7E012345_31" in equipment

    # Same result on the cached second call.
    equipment = await client.async_get_equipment()
    assert "7A012340-CA" not in equipment
    assert "7A012345-CA" in equipment


async def test_async_get_equipment_include_inactive_returns_all():
    """include_inactive=True keeps retired units."""
    equipment_json = _load_fixture("equipment_with_inactive.json")
    sso_cookie = _make_cookie("SolarEdge_SSO-1.4", "sso_value")

    equipment_resp = _mock_response(json_data=equipment_json)
    session = _make_mock_session(cookies=[sso_cookie])
    session.get = AsyncMock(side_effect=[equipment_resp])
    session.post = AsyncMock()

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    equipment = await client.async_get_equipment(include_inactive=True)
    assert "7A012340-CA" in equipment
    assert "7A012345-CA" in equipment


async def test_async_get_energy_data_includes_aggregations():
    """async_get_energy_data returns per-optimizer, string, inverter, and site values."""
    playback_json = _load_fixture("playback.json")
    equipment_json = _load_fixture("equipment.json")

    sso_cookie = _make_cookie("SolarEdge_SSO-1.4", "sso_value")
    session = _make_mock_session(cookies=[sso_cookie])

    equipment_resp = _mock_response(json_data=equipment_json)
    playback_resp = _mock_response(json_data=playback_json)
    session.get = AsyncMock(side_effect=[equipment_resp, playback_resp])

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 30, 4, 0, 0, tzinfo=timezone.utc)
    result = await client.async_get_energy_data(start, end)

    assert len(result) == 4
    slot1 = result[1].values
    assert slot1["7A012345-CA"] == 100.0
    assert slot1["7A012346-CA"] == 50.0
    assert slot1["7E012345_31"] == 150.0
    assert slot1["7E012345-57"] == 150.0
    assert slot1["TESTSITE01"] == 150.0


async def test_async_get_energy_data_uses_correct_url():
    """Verify the playback URL is built with UTC dates and resolution=hours."""
    playback_json = _load_fixture("playback.json")
    equipment_json = _load_fixture("equipment.json")

    sso_cookie = _make_cookie("SolarEdge_SSO-1.4", "sso_value")
    session = _make_mock_session(cookies=[sso_cookie])

    equipment_resp = _mock_response(json_data=equipment_json)
    playback_resp = _mock_response(json_data=playback_json)
    session.get = AsyncMock(side_effect=[equipment_resp, playback_resp])

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 30, 4, 0, 0, tzinfo=timezone.utc)
    await client.async_get_energy_data(start, end)

    playback_call = session.get.await_args_list[1]
    url = playback_call.args[0]
    assert "services/layout/playback/site/123/optimizers-compact" in url
    assert "resolution=hours" in url
    assert "start-date=2026-07-30T00:00:00Z" in url
    assert "end-date=2026-07-30T04:00:00Z" in url


async def test_async_get_energy_data_sends_auth_and_csrf_headers():
    """Playback request includes Authorization and X-CSRF-TOKEN headers."""
    playback_json = _load_fixture("playback.json")
    equipment_json = _load_fixture("equipment.json")

    sso_cookie = _make_cookie("SolarEdge_SSO-1.4", "sso_value")
    csrf_cookie = _make_cookie("CSRF-TOKEN", "csrf-value-123")
    session = _make_mock_session(cookies=[sso_cookie, csrf_cookie])

    equipment_resp = _mock_response(json_data=equipment_json)
    playback_resp = _mock_response(json_data=playback_json)
    session.get = AsyncMock(side_effect=[equipment_resp, playback_resp])

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 30, 4, 0, 0, tzinfo=timezone.utc)
    await client.async_get_energy_data(start, end)

    playback_call = session.get.await_args_list[1]
    headers = playback_call.kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer test"
    assert headers.get("X-CSRF-TOKEN") == "csrf-value-123"


async def test_async_get_energy_data_empty_response():
    """Empty playback response returns an empty list."""
    equipment_json = _load_fixture("equipment.json")
    sso_cookie = _make_cookie("SolarEdge_SSO-1.4", "sso_value")
    session = _make_mock_session(cookies=[sso_cookie])

    equipment_resp = _mock_response(json_data=equipment_json)
    empty_resp = _mock_response(json_data={"timeSlotsCount": 0, "optimizerSerials": [], "compressPowerData": []})
    session.get = AsyncMock(side_effect=[equipment_resp, empty_resp])

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 30, 4, 0, 0, tzinfo=timezone.utc)
    result = await client.async_get_energy_data(start, end)
    assert result == []
