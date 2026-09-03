"""Tests for SolarEdge Web."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from http.cookies import Morsel
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest

from solaredge_web import SolarEdgeWeb
from solaredge_web.solaredge import (
    _build_opt_to_parent_map,
    _decode_dashboard_measurements,
    _decode_energy_graph,
    _decode_energy_totals,
    _decode_playback,
    _decode_playback_verbose,
    _to_utc_iso,
)

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
    resp.read = AsyncMock(return_value=text.encode())
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
    session_cookie = _make_cookie("se_monitoring_auth", "session_value")

    equipment_resp = _mock_response(json_data=equipment_json)
    session = _make_mock_session(cookies=[session_cookie])
    session.get = AsyncMock(side_effect=[equipment_resp])

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    await client.async_get_equipment()
    await client.async_get_equipment()

    assert session.get.await_count == 1


async def test_async_get_equipment_skips_login_with_valid_session():
    """Login is skipped when a valid session cookie + auth headers exist."""
    equipment_json = _load_fixture("equipment.json")
    session_cookie = _make_cookie("se_monitoring_auth", "session_value")

    equipment_resp = _mock_response(json_data=equipment_json)
    session = _make_mock_session(cookies=[session_cookie])
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
    session_cookie = _make_cookie("se_monitoring_auth", "session_value")

    equipment_resp = _mock_response(json_data=equipment_json)
    session = _make_mock_session(cookies=[session_cookie])
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
    session_cookie = _make_cookie("se_monitoring_auth", "session_value")

    equipment_resp = _mock_response(json_data=equipment_json)
    session = _make_mock_session(cookies=[session_cookie])
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

    session_cookie = _make_cookie("se_monitoring_auth", "session_value")
    session = _make_mock_session(cookies=[session_cookie])

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

    session_cookie = _make_cookie("se_monitoring_auth", "session_value")
    session = _make_mock_session(cookies=[session_cookie])

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

    session_cookie = _make_cookie("se_monitoring_auth", "session_value")
    csrf_cookie = _make_cookie("CSRF-TOKEN", "csrf-value-123")
    session = _make_mock_session(cookies=[session_cookie, csrf_cookie])

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
    """Empty playback response returns an empty list, even after the fallback."""
    equipment_json = _load_fixture("equipment.json")
    session_cookie = _make_cookie("se_monitoring_auth", "session_value")
    session = _make_mock_session(cookies=[session_cookie])

    equipment_resp = _mock_response(json_data=equipment_json)
    empty_resp = _mock_response(json_data={"timeSlotsCount": 0, "optimizerSerials": [], "compressPowerData": []})
    empty_verbose_resp = _mock_response(json_data={"optimizerPowerMeasurementsList": []})
    session.get = AsyncMock(side_effect=[equipment_resp, empty_resp, empty_verbose_resp])

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 30, 4, 0, 0, tzinfo=timezone.utc)
    result = await client.async_get_energy_data(start, end)
    assert result == []


def test_decode_playback_header_only_returns_empty(caplog):
    """A header-only compressPowerData warns instead of yielding silent zeros.

    See https://github.com/Solarlibs/solaredge-web/issues/13
    """
    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    resp = {
        "timeSlotsCount": 48,
        "optimizerSerials": [f"7A01234{i}" for i in range(10)],
        "compressPowerData": [2.0, 2.0],
    }
    with caplog.at_level(logging.WARNING):
        assert _decode_playback(resp, start, {}) == []
    assert "no measurements" in caplog.text


def test_decode_playback_malformed_header_returns_empty():
    """Truncated or non-numeric headers return [] instead of raising."""
    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    # Only the version entry, no data_start_idx.
    assert _decode_playback({"timeSlotsCount": 4, "optimizerSerials": ["X"], "compressPowerData": [2.0]}, start, {}) == []
    # Non-numeric header.
    assert (
        _decode_playback(
            {"timeSlotsCount": 4, "optimizerSerials": ["X"], "compressPowerData": [2.0, "nope", 0.0, 0.0, 1.0]},
            start,
            {},
        )
        == []
    )
    # Non-numeric timeSlotsCount.
    assert (
        _decode_playback({"timeSlotsCount": "many", "optimizerSerials": ["X"], "compressPowerData": [2.0, 4.0]}, start, {})
        == []
    )


def test_decode_playback_verbose_matches_compact():
    """The verbose decoder produces the same values and slots as the compact one."""
    verbose = _load_fixture("playback_verbose.json")
    site_structure = _load_fixture("equipment.json")["siteStructure"]

    result = _decode_playback_verbose(verbose, site_structure)

    # Sparse: only the three slots that carry production.
    assert [ed.start_time for ed in result] == [
        datetime(2026, 7, 30, 1, 0),
        datetime(2026, 7, 30, 3, 0),
        datetime(2026, 7, 31, 9, 0),
    ]
    # measurementTime carries the site offset; slots come back naive local.
    assert all(ed.start_time.tzinfo is None for ed in result)

    slot1 = result[0].values
    assert slot1["7A012345-CA"] == 100.0
    assert slot1["7A012346-CA"] == 50.0
    assert slot1["7E012345_31"] == 150.0
    assert slot1["7E012345-57"] == 150.0
    assert slot1["TESTSITE01"] == 150.0


def test_decode_playback_verbose_filters_to_window():
    """Measurements outside [start_date, end_date] are dropped."""
    verbose = _load_fixture("playback_verbose.json")
    site_structure = _load_fixture("equipment.json")["siteStructure"]

    result = _decode_playback_verbose(
        verbose,
        site_structure,
        datetime(2026, 7, 30, 0, 0),
        datetime(2026, 7, 30, 4, 0),
    )

    assert [ed.start_time for ed in result] == [datetime(2026, 7, 30, 1, 0), datetime(2026, 7, 30, 3, 0)]


def test_decode_playback_verbose_empty():
    """A verbose response without measurements returns an empty list."""
    assert _decode_playback_verbose({}, {}) == []


async def test_async_get_energy_data_falls_back_to_verbose():
    """A header-only compact payload falls back to the verbose endpoint."""
    equipment_json = _load_fixture("equipment.json")
    verbose_json = _load_fixture("playback_verbose.json")
    session_cookie = _make_cookie("se_monitoring_auth", "session_value")
    session = _make_mock_session(cookies=[session_cookie])

    equipment_resp = _mock_response(json_data=equipment_json)
    header_only_resp = _mock_response(
        json_data={
            "timeSlotsCount": 4,
            "optimizerSerials": ["7A012345", "7A012346"],
            "compressPowerData": [2.0, 2.0],
        }
    )
    session.get = AsyncMock(
        side_effect=[
            equipment_resp,
            header_only_resp,
            _mock_response(json_data=verbose_json),
            _mock_response(json_data=verbose_json),
        ]
    )

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    start = datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 30, 4, 0, 0, tzinfo=timezone.utc)
    result = await client.async_get_energy_data(start, end)

    # Compact first, then verbose with the window as-is to learn the offset.
    assert "optimizers-compact" in session.get.await_args_list[1].args[0]
    probe_url = session.get.await_args_list[2].args[0]
    assert "/optimizers?" in probe_url
    assert "start-date=2026-07-30T00:00:00Z" in probe_url

    # The fixture is stamped -07:00, so the window is re-requested shifted by +7h.
    shifted_url = session.get.await_args_list[3].args[0]
    assert "start-date=2026-07-30T07:00:00Z" in shifted_url
    assert "end-date=2026-07-30T11:00:00Z" in shifted_url
    assert client._site_utc_offset == timedelta(hours=-7)

    # The result is filtered back to the requested local window.
    assert [ed.start_time for ed in result] == [datetime(2026, 7, 30, 1, 0), datetime(2026, 7, 30, 3, 0)]
    assert result[0].values["7A012345-CA"] == 100.0
    assert result[0].values["TESTSITE01"] == 150.0


async def test_verbose_fallback_reuses_cached_offset():
    """Once the site UTC offset is known, the verbose window is fetched once."""
    equipment_json = _load_fixture("equipment.json")
    verbose_json = _load_fixture("playback_verbose.json")
    session_cookie = _make_cookie("se_monitoring_auth", "session_value")
    session = _make_mock_session(cookies=[session_cookie])

    header_only = {"timeSlotsCount": 4, "optimizerSerials": ["7A012345"], "compressPowerData": [2.0, 2.0]}
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=equipment_json),
            _mock_response(json_data=header_only),
            _mock_response(json_data=verbose_json),
        ]
    )

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}
    client._site_utc_offset = timedelta(hours=-7)

    result = await client.async_get_energy_data(datetime(2026, 7, 30, 0, 0, 0), datetime(2026, 7, 30, 4, 0, 0))

    assert session.get.await_count == 3
    assert "start-date=2026-07-30T07:00:00Z" in session.get.await_args_list[2].args[0]
    assert [ed.start_time for ed in result] == [datetime(2026, 7, 30, 1, 0), datetime(2026, 7, 30, 3, 0)]


async def test_async_get_energy_data_no_fallback_when_compact_has_data():
    """The verbose endpoint is not called when the compact response is usable."""
    equipment_json = _load_fixture("equipment.json")
    playback_json = _load_fixture("playback.json")
    session_cookie = _make_cookie("se_monitoring_auth", "session_value")
    session = _make_mock_session(cookies=[session_cookie])

    session.get = AsyncMock(side_effect=[_mock_response(json_data=equipment_json), _mock_response(json_data=playback_json)])

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    await client.async_get_energy_data(
        datetime(2026, 7, 30, 0, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 30, 4, 0, 0, tzinfo=timezone.utc),
    )

    assert session.get.await_count == 2


async def test_login_failure_raises_401():
    """Bad credentials surface as a 401 so HA reports invalid_auth, not cannot_connect."""
    # No callback in the redirect history: the login form was re-served.
    login_page = _mock_response(
        text='<html><form action="/login"><input name="csrf" value="abc"></form></html>',
        url="https://login.solaredge.com/login",
    )
    session = _make_mock_session()
    session.get = AsyncMock(side_effect=[login_page])
    session.post = AsyncMock(side_effect=[login_page])

    client = SolarEdgeWeb("u", "wrong-password", "123", session, timeout=5)
    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client.async_login()
    assert err.value.status == 401


async def test_login_refuses_foreign_form_action():
    """Credentials are never posted to a host outside solaredge.com."""
    login_page = _mock_response(
        text='<html><form action="https://evil.example.com/steal"><input name="csrf" value="abc"></form></html>',
        url="https://login.solaredge.com/login",
    )
    session = _make_mock_session()
    session.get = AsyncMock(side_effect=[login_page])
    session.post = AsyncMock()

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    with pytest.raises(aiohttp.ClientResponseError) as err:
        await client.async_login()
    assert err.value.status == 401
    session.post.assert_not_awaited()


async def test_extract_code_ignores_foreign_callback():
    """An authorization code is only accepted from a solaredge.com host."""
    session = _make_mock_session()
    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)

    foreign = _mock_response(url="https://evil.example.com/mfe/auth/callback?code=stolen")
    assert client._extract_code_from_history(foreign) is None

    genuine = _mock_response(url="https://monitoring.solaredge.com/mfe/auth/callback?code=good")
    assert client._extract_code_from_history(genuine) == "good"


async def test_async_get_equipment_http_error():
    """A non-200 from the layout endpoint propagates as ClientResponseError."""
    session_cookie = _make_cookie("se_monitoring_auth", "session_value")
    session = _make_mock_session(cookies=[session_cookie])
    session.get = AsyncMock(side_effect=[_mock_failing_response(404)])

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_equipment()


def test_find_cookie_matches_parent_domain():
    """A cookie scoped to solaredge.com is found for monitoring.solaredge.com."""
    session = _make_mock_session(
        cookies=[
            _make_cookie("se_monitoring_auth", "v", domain="solaredge.com"),
            _make_cookie("other", "v", domain="login.solaredge.com"),
        ]
    )
    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)

    assert client._find_cookie("se_monitoring_auth") is not None
    # A cookie on a sibling host must not match.
    assert client._find_cookie("other") is None


async def test_login_is_reused_and_cache_survives(caplog):
    """A valid session cookie skips the OAuth flow and keeps the equipment cache."""
    equipment_json = _load_fixture("equipment.json")
    session_cookie = _make_cookie("se_monitoring_auth", "session_value")
    session = _make_mock_session(cookies=[session_cookie])
    session.get = AsyncMock(side_effect=[_mock_response(json_data=equipment_json)])
    session.post = AsyncMock()

    client = SolarEdgeWeb("u", "p", "123", session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}

    await client.async_get_equipment()
    await client.async_login()
    await client.async_get_equipment()

    # One layout fetch, no OAuth traffic, cache intact across the extra login.
    assert session.get.await_count == 1
    session.post.assert_not_awaited()


def _logged_in_client(session, site_id="123"):
    """Build a client that skips the OAuth flow because the session is valid."""
    client = SolarEdgeWeb("u", "p", site_id, session, timeout=5)
    client._last_login_time = 1e12
    client._auth_headers = {"Authorization": "Bearer test"}
    return client


async def test_async_get_site_components_is_cached():
    """Site components are fetched once and reused."""
    components = _load_fixture("site_components.json")
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(side_effect=[_mock_response(json_data=components)])

    client = _logged_in_client(session)
    first = await client.async_get_site_components()
    second = await client.async_get_site_components()

    assert first["hasConsumptionAndGrid"] is True
    assert second == first
    assert session.get.await_count == 1
    url = session.get.await_args_list[0].args[0]
    assert url.endswith("services/dashboard/site-details/123/components")


async def test_async_get_site_components_cache_cleared_by_login():
    """A fresh login drops the cached components."""
    components = _load_fixture("site_components.json")
    auth_resp = _mock_response(url="https://monitoring.solaredge.com/mfe/auth/callback?code=test_code")
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[_mock_response(json_data=components), auth_resp, _mock_response(json_data=components)]
    )
    session.post = AsyncMock(
        side_effect=[
            _mock_response(json_data={"access_token": "test_token"}),
            _mock_response(json_data={"ok": True}),
        ]
    )

    client = _logged_in_client(session)
    await client.async_get_site_components()
    client._last_login_time = 0.0
    client._auth_headers = {}
    await client.async_get_site_components()

    assert session.get.await_count == 3


async def test_async_get_data_availability():
    """Data availability is returned as-is and is not cached."""
    availability = _load_fixture("data_availability.json")
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(side_effect=[_mock_response(json_data=availability)] * 2)

    client = _logged_in_client(session)
    result = await client.async_get_data_availability()
    await client.async_get_data_availability()

    assert result["consumptionDataAvailableFrom"] == "2023-04-01"
    assert session.get.await_count == 2
    url = session.get.await_args_list[0].args[0]
    assert url.endswith("services/dashboard/data-availability/sites/123")


async def test_async_get_site_components_sends_auth_and_csrf_headers():
    """Dashboard requests carry the bearer token and the CSRF cookie value."""
    components = _load_fixture("site_components.json")
    session = _make_mock_session(
        cookies=[_make_cookie("se_monitoring_auth", "session_value"), _make_cookie("CSRF-TOKEN", "csrf-value-123")]
    )
    session.get = AsyncMock(side_effect=[_mock_response(json_data=components)])

    client = _logged_in_client(session)
    await client.async_get_site_components()

    headers = session.get.await_args_list[0].kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer test"
    assert headers.get("X-CSRF-TOKEN") == "csrf-value-123"


async def test_async_get_site_components_http_error():
    """HTTP failures propagate to the caller."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(side_effect=[_mock_failing_response(status=403)])

    client = _logged_in_client(session)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_site_components()


async def test_async_get_consumption_data_hourly_converts_watts_to_wh():
    """Hourly slots come from the power endpoint in W and are returned in Wh."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("site_components.json")),
            _mock_response(json_data=_load_fixture("dashboard_power_hours.json")),
        ]
    )

    client = _logged_in_client(session)
    data = await client.async_get_consumption_data(datetime(2026, 7, 30), datetime(2026, 7, 30))

    assert len(data) == 8
    # Naive site-local slot times, one hour apart.
    assert data[0].start_time == datetime(2026, 7, 30, 0, 0)
    assert data[5].start_time == datetime(2026, 7, 30, 5, 0)
    # 1-hour slots, so Wh equals the reported watts.
    assert data[5].production == 1500.0
    assert data[5].consumption == 600.0
    assert data[5].self_consumption == 600.0
    assert data[5].exported == 900.0
    assert data[5].consumption_from_grid == 0.0


async def test_async_get_consumption_data_quarter_hours_scales_by_slot():
    """Quarter-hour slots are a quarter of an hour long, so Wh = W / 4."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("site_components.json")),
            _mock_response(json_data=_load_fixture("dashboard_power_hours.json")),
        ]
    )

    client = _logged_in_client(session)
    data = await client.async_get_consumption_data(datetime(2026, 7, 30), datetime(2026, 7, 30), resolution="quarter-hours")

    assert data[5].production == 375.0
    assert data[5].consumption == 150.0


async def test_async_get_consumption_data_daily_uses_energy_endpoint():
    """Daily data is already in Wh and is read from chart.measurements."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("site_components.json")),
            _mock_response(json_data=_load_fixture("dashboard_energy_days.json")),
        ]
    )

    client = _logged_in_client(session)
    data = await client.async_get_consumption_data(datetime(2026, 7, 28), datetime(2026, 7, 30), resolution="days")

    url = session.get.await_args_list[1].args[0]
    assert "services/dashboard/energy/sites/123" in url
    assert "chart-time-unit=days" in url
    assert len(data) == 3
    assert data[0].start_time == datetime(2026, 7, 28)
    assert data[0].production == 34279.0
    assert data[0].consumption == 21500.0


async def test_async_get_consumption_data_url_and_measurement_types():
    """URL carries plain dates, the time unit, and repeated measurement-types."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("site_components.json")),
            _mock_response(json_data=_load_fixture("dashboard_power_hours.json")),
        ]
    )

    client = _logged_in_client(session)
    await client.async_get_consumption_data(datetime(2026, 7, 24), datetime(2026, 7, 30))

    url = session.get.await_args_list[1].args[0]
    assert "services/dashboard/power/sites/123" in url
    assert "chart-time-unit=hours" in url
    assert "start-date=2026-07-24" in url
    assert "end-date=2026-07-30" in url
    assert "measurement-types=production" in url
    assert "measurement-types=consumption" in url
    assert "measurement-types=import" in url
    assert "measurement-types=export" in url
    # No storage on this site, so the without-storage breakdown is requested.
    assert "consumption-distribution-without-storage" in url
    assert "with-storage" not in url.replace("without-storage", "")


async def test_async_get_consumption_data_requests_storage_breakdown():
    """Sites with a battery ask for the with-storage distributions."""
    components = dict(_load_fixture("site_components.json"), hasStorage=True)
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=components),
            _mock_response(json_data=_load_fixture("dashboard_power_hours.json")),
        ]
    )

    client = _logged_in_client(session)
    await client.async_get_consumption_data(datetime(2026, 7, 30), datetime(2026, 7, 30))

    url = session.get.await_args_list[1].args[0]
    assert "consumption-distribution-with-storage" in url
    assert "production-distribution-with-storage" in url
    assert "without-storage" not in url


async def test_async_get_consumption_data_without_meter_keeps_none():
    """A site with no consumption meter reports None, never zero."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("site_components_no_meter.json")),
            _mock_response(json_data=_load_fixture("dashboard_power_no_meter.json")),
        ]
    )

    client = _logged_in_client(session)
    data = await client.async_get_consumption_data(datetime(2026, 7, 30), datetime(2026, 7, 30))

    assert data[5].production == 1500.0
    assert data[5].consumption is None
    assert data[5].imported is None
    assert data[5].exported is None
    assert data[5].self_consumption is None


async def test_async_get_consumption_data_defaults_to_last_7_days():
    """Without dates the range is the last 7 days in the site's local time."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("site_components.json")),
            _mock_response(json_data=_load_fixture("dashboard_power_hours.json")),
        ]
    )

    client = _logged_in_client(session)
    await client.async_get_consumption_data()

    url = session.get.await_args_list[1].args[0]
    today = datetime.now().date()
    assert f"start-date={today - timedelta(days=7)}" in url
    assert f"end-date={today}" in url


async def test_async_get_consumption_data_warns_on_too_wide_range(caplog):
    """A range wider than the API allows is flagged before the request."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("site_components.json")),
            _mock_response(json_data=_load_fixture("dashboard_power_hours.json")),
        ]
    )

    client = _logged_in_client(session)
    with caplog.at_level(logging.WARNING):
        await client.async_get_consumption_data(datetime(2026, 7, 1), datetime(2026, 7, 30), resolution="quarter-hours")

    assert "wider than the 7 days" in caplog.text


async def test_async_get_consumption_data_rejects_unknown_resolution():
    """An unsupported resolution fails before any request is made."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock()

    client = _logged_in_client(session)
    with pytest.raises(ValueError, match="Unsupported resolution"):
        await client.async_get_consumption_data(resolution="weeks")

    session.get.assert_not_awaited()


async def test_async_get_consumption_data_bad_arguments_propagates():
    """A BAD_ARGUMENTS 400 surfaces as a ClientResponseError."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("site_components.json")),
            _mock_failing_response(status=400),
        ]
    )

    client = _logged_in_client(session)
    with pytest.raises(aiohttp.ClientResponseError):
        await client.async_get_consumption_data()


async def test_async_get_consumption_data_empty_response(caplog):
    """An empty payload returns an empty list and warns."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("site_components.json")),
            _mock_response(json_data={"measurements": []}),
        ]
    )

    client = _logged_in_client(session)
    with caplog.at_level(logging.WARNING):
        data = await client.async_get_consumption_data()

    assert data == []
    assert "No measurements returned" in caplog.text


def test_decode_dashboard_measurements_skips_invalid_entries():
    """Entries without a usable timestamp are dropped, not fatal."""
    measurements = [
        {"measurementTime": None, "production": 10.0},
        {"measurementTime": "not-a-date", "production": 10.0},
        {"measurementTime": "2026-07-30T01:00:00-07:00", "production": "bogus", "consumption": 20.0},
        {"measurementTime": "2026-07-30T02:00:00-07:00", "production": 30.0},
    ]

    data = _decode_dashboard_measurements(measurements, 1.0)

    assert [d.start_time for d in data] == [datetime(2026, 7, 30, 1, 0), datetime(2026, 7, 30, 2, 0)]
    assert data[0].production is None
    assert data[0].consumption == 20.0
    assert data[1].production == 30.0


async def test_async_get_energy_totals_keys_match_equipment():
    """Totals are keyed by the same device ids async_get_equipment returns."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("equipment.json")),
            _mock_response(json_data=_load_fixture("layout_energy_by_inverter.json")),
        ]
    )

    client = _logged_in_client(session)
    totals = await client.async_get_energy_totals(datetime(2026, 7, 30), datetime(2026, 7, 30))

    assert totals["7A012345-CA"] == 1596.25
    assert totals["7A012346-CA"] == 1422.25
    assert totals["7E012345_31"] == 23935.75
    assert totals["7E012345-57"] == 41821.0
    # The site total is the sum of the inverters; the response has no site entry.
    assert totals["TESTSITE01"] == 41821.0
    assert set(totals) <= set(await client.async_get_equipment()) | {"TESTSITE01"}


async def test_async_get_energy_totals_url():
    """The by-inverter URL carries plain dates and one serial per inverter."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("equipment.json")),
            _mock_response(json_data=_load_fixture("layout_energy_by_inverter.json")),
        ]
    )

    client = _logged_in_client(session)
    await client.async_get_energy_totals(datetime(2026, 7, 30), datetime(2026, 7, 30))

    url = session.get.await_args_list[1].args[0]
    assert "services/layout/energy/site/123/by-inverter" in url
    assert "start-date=2026-07-30" in url
    assert "end-date=2026-07-30" in url
    assert "inverter-serials=7E012345-57" in url
    assert "include-color=true" in url


async def test_async_get_energy_totals_defaults_to_today():
    """Without dates the range is today only."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("equipment.json")),
            _mock_response(json_data=_load_fixture("layout_energy_by_inverter.json")),
        ]
    )

    client = _logged_in_client(session)
    await client.async_get_energy_totals()

    url = session.get.await_args_list[1].args[0]
    today = datetime.now().date()
    assert f"start-date={today}" in url
    assert f"end-date={today}" in url


def test_decode_energy_totals_converts_units():
    """Energy reported in kWh is converted to Wh."""
    resp = {
        "inverters": [
            {
                "serial": "7E012345-57",
                "energy": {"value": 41.821, "unit": "kilo-watt-hour"},
                "strings": [{"energy": {"value": 23.93575, "unit": "kilo-watt-hour"}, "stringRelativeOrder": 1}],
                "optimizers": [{"serial": "7A012345-CA", "energy": {"value": 1.59625, "unit": "kilo-watt-hour"}}],
            }
        ]
    }

    totals = _decode_energy_totals(resp, _load_fixture("equipment.json")["siteStructure"])

    assert totals["7E012345-57"] == pytest.approx(41821.0)
    assert totals["7E012345_31"] == pytest.approx(23935.75)
    assert totals["7A012345-CA"] == pytest.approx(1596.25)


def test_decode_energy_totals_empty(caplog):
    """A response with no inverters returns an empty dict and warns."""
    with caplog.at_level(logging.WARNING):
        assert _decode_energy_totals({"inverters": []}, {}) == {}
    assert "No inverters returned" in caplog.text


async def test_async_get_energy_totals_without_inverters(caplog):
    """A layout with no inverters short-circuits without a request."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(side_effect=[_mock_response(json_data={"siteStructure": {"type": "SITE"}})])

    client = _logged_in_client(session)
    with caplog.at_level(logging.WARNING):
        assert await client.async_get_energy_totals() == {}

    assert session.get.await_count == 1
    assert "No inverters found" in caplog.text


async def test_async_get_site_energy_hourly():
    """Hourly site energy is returned in Wh with naive site-local times."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(side_effect=[_mock_response(json_data=_load_fixture("energy_graph_hours.json"))])

    client = _logged_in_client(session)
    data = await client.async_get_site_energy(datetime(2026, 7, 30), datetime(2026, 7, 30))

    url = session.get.await_args_list[0].args[0]
    assert "services/layout/energy-graph/site/123" in url
    assert "chart-time-unit=hours" in url
    assert "start-date=2026-07-30" in url
    assert len(data) == 24
    assert data[8].start_time == datetime(2026, 7, 30, 8, 0)
    assert data[8].energy == 1223.0
    # Slots the site has not reported yet stay None.
    assert data[23].energy is None


async def test_async_get_site_energy_defaults_per_resolution():
    """Each resolution defaults to a range the API accepts."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(side_effect=[_mock_response(json_data=_load_fixture("energy_graph_hours.json"))] * 2)

    client = _logged_in_client(session)
    await client.async_get_site_energy()
    await client.async_get_site_energy(resolution="days")

    today = datetime.now().date()
    hourly_url = session.get.await_args_list[0].args[0]
    daily_url = session.get.await_args_list[1].args[0]
    # "hours" only serves a single day.
    assert f"start-date={today}" in hourly_url
    assert f"end-date={today}" in hourly_url
    assert f"start-date={today - timedelta(days=7)}" in daily_url


async def test_async_get_site_energy_warns_on_too_wide_range(caplog):
    """Hourly data spanning more than a day is flagged before the request."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(side_effect=[_mock_response(json_data=_load_fixture("energy_graph_hours.json"))])

    client = _logged_in_client(session)
    with caplog.at_level(logging.WARNING):
        await client.async_get_site_energy(datetime(2026, 7, 24), datetime(2026, 7, 30))

    assert "wider than the 0 days" in caplog.text


async def test_async_get_site_energy_rejects_unknown_resolution():
    """quarter-hours is not served by this endpoint."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock()

    client = _logged_in_client(session)
    with pytest.raises(ValueError, match="Unsupported resolution"):
        await client.async_get_site_energy(resolution="quarter-hours")

    session.get.assert_not_awaited()


def test_decode_energy_graph_empty(caplog):
    """A response with no bars returns an empty list and warns."""
    with caplog.at_level(logging.WARNING):
        assert _decode_energy_graph({"energyBars": []}) == []
    assert "No energy bars returned" in caplog.text


async def test_async_get_live_power():
    """Live power comes from live-power, status and flow from power-flow."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data=_load_fixture("live_power.json")),
            _mock_response(json_data=_load_fixture("power_flow.json")),
        ]
    )

    client = _logged_in_client(session)
    live = await client.async_get_live_power()

    assert session.get.await_args_list[0].args[0].endswith("services/dashboard/live-power/sites/123")
    assert session.get.await_args_list[1].args[0].endswith("services/dashboard/power-flow/v2/sites/123")
    # Watts, not the kW the power-flow payload rounds to.
    assert live.current_power == 2438.2266
    assert live.max_power == 7600.0
    assert live.is_communicating is True
    assert live.last_update_time == datetime(2026, 7, 30, 17, 16, 45, 43000)
    assert live.power_flow["solarProduction"]["currentPower"] == 2.44


async def test_async_get_live_power_tolerates_missing_fields():
    """Absent or unparsable values become None instead of raising."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(
        side_effect=[
            _mock_response(json_data={}),
            _mock_response(json_data={"lastUpdateTime": "not-a-time"}),
        ]
    )

    client = _logged_in_client(session)
    live = await client.async_get_live_power()

    assert live.current_power is None
    assert live.max_power is None
    assert live.is_communicating is None
    assert live.last_update_time is None
    assert live.power_flow == {"lastUpdateTime": "not-a-time"}


async def test_async_get_site_information_is_cached():
    """Site information is fetched once and reused, and carries the timezone."""
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(side_effect=[_mock_response(json_data=_load_fixture("site_information.json"))])

    client = _logged_in_client(session)
    info = await client.async_get_site_information()
    await client.async_get_site_information()

    assert info["siteTimeZone"] == "America/Los_Angeles"
    assert info["peakPower"] == 10.53
    assert session.get.await_count == 1
    assert session.get.await_args_list[0].args[0].endswith("services/layout/information/site/123")


async def test_async_get_site_information_cache_cleared_by_login():
    """A fresh login drops the cached site information."""
    info = _load_fixture("site_information.json")
    auth_resp = _mock_response(url="https://monitoring.solaredge.com/mfe/auth/callback?code=test_code")
    session = _make_mock_session(cookies=[_make_cookie("se_monitoring_auth", "session_value")])
    session.get = AsyncMock(side_effect=[_mock_response(json_data=info), auth_resp, _mock_response(json_data=info)])
    session.post = AsyncMock(
        side_effect=[
            _mock_response(json_data={"access_token": "test_token"}),
            _mock_response(json_data={"ok": True}),
        ]
    )

    client = _logged_in_client(session)
    await client.async_get_site_information()
    client._last_login_time = 0.0
    client._auth_headers = {}
    await client.async_get_site_information()

    assert session.get.await_count == 3
