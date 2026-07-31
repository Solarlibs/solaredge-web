"""API for SolarEdge Web."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import html
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import aiohttp

if TYPE_CHECKING:
    from http.cookies import Morsel

_LOGGER = logging.getLogger(__name__)

# OAuth2 public client used by the SolarEdge monitoring web app.
# This is the same client_id the website itself uses; it is not a secret.
_OAUTH_CLIENT_ID = "ugfnsujd3384sshcjehaphlh3"
_OAUTH_REDIRECT_URI = "https://monitoring.solaredge.com/mfe/auth/callback"
_OAUTH_AUTHORIZE_URL = "https://login.solaredge.com/oauth2/authorize"
_OAUTH_TOKEN_URL = "https://login.solaredge.com/oauth2/token"  # noqa: S105
_AUTH_EXCHANGE_URL = "https://monitoring.solaredge.com/services/auth/token?legacy=false"

# Refresh SSO session at most every hour to avoid re-issuing the OAuth flow.
_LOGIN_REFRESH_SECONDS = 3600


def _raise_login_error() -> None:
    """Raise a login error (extracted to satisfy TRY301)."""
    raise aiohttp.ClientError("Failed to extract authorization code during login.")


@dataclasses.dataclass
class EnergyData:
    """Energy data for a single hourly time slot.

    values maps equipment serial numbers to energy in Wh. Optimizer values
    are from the API; string, inverter, and site values are aggregated by
    summing child optimizer values.
    """

    start_time: datetime
    values: dict[str, float]


class SolarEdgeWeb:
    """SolarEdge Web client using the OAuth2 PKCE flow."""

    def __init__(
        self,
        username: str,
        password: str,
        site_id: str,
        session: aiohttp.ClientSession,
        timeout: int = 10,
    ) -> None:
        """Initialize the SolarEdge Web client."""
        self.username = username
        self.password = password
        self.site_id = site_id
        self.session = session
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._equipment: dict[str, dict[str, Any]] = {}
        self._site_structure: dict[str, Any] = {}
        self._last_login_time = 0.0
        self._auth_headers: dict[str, str] = {}

    async def async_login(self) -> None:
        """Login via OAuth2 PKCE. Reuses SSO cookie for 1 hour."""
        sso_cookie = self._find_cookie("SolarEdge_SSO-1.4")
        if sso_cookie is not None and self._auth_headers and (time.time() - self._last_login_time < _LOGIN_REFRESH_SECONDS):
            _LOGGER.debug("Skipping login. Reusing SSO cookie and auth headers.")
            return

        _LOGGER.debug("Starting OAuth2 login flow...")
        self._equipment = {}
        self._site_structure = {}

        try:
            verifier_bytes = os.urandom(32)
            code_verifier = base64.urlsafe_b64encode(verifier_bytes).rstrip(b"=").decode("ascii")
            challenge_bytes = hashlib.sha256(code_verifier.encode("ascii")).digest()
            code_challenge = base64.urlsafe_b64encode(challenge_bytes).rstrip(b"=").decode("ascii")

            auth_url = (
                f"{_OAUTH_AUTHORIZE_URL}"
                f"?client_id={_OAUTH_CLIENT_ID}"
                f"&response_type=code"
                f"&redirect_uri={_OAUTH_REDIRECT_URI}"
                f"&code_challenge={code_challenge}"
                f"&code_challenge_method=S256"
            )

            resp = await self.session.get(auth_url, timeout=self.timeout)
            code = self._extract_code_from_history(resp)

            if not code:
                code = await self._submit_login_form(resp)

            if not code:
                _raise_login_error()

            _LOGGER.debug("Successfully obtained authorization code.")

            # Exchange code for OAuth tokens.
            token_data = {
                "grant_type": "authorization_code",
                "client_id": _OAUTH_CLIENT_ID,
                "redirect_uri": _OAUTH_REDIRECT_URI,
                "code": code,
                "code_verifier": code_verifier,
            }
            resp = await self.session.post(_OAUTH_TOKEN_URL, data=token_data, timeout=self.timeout)
            resp.raise_for_status()
            oauth_tokens = await resp.json()

            self._auth_headers = {"Authorization": f"Bearer {oauth_tokens['access_token']}"}

            # Establish the backend monitoring session.
            resp = await self.session.post(
                _AUTH_EXCHANGE_URL,
                json=oauth_tokens,
                headers=self._auth_headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()

            self._last_login_time = time.time()
            _LOGGER.debug("Successfully completed OAuth2 login flow.")

        except aiohttp.ClientError:
            _LOGGER.exception("Error during SolarEdge login")
            raise

    async def _submit_login_form(self, resp: aiohttp.ClientResponse) -> str | None:
        """Parse the login form and submit credentials. Returns the OAuth code."""
        raw_html = await resp.text()
        action = html.unescape(str(resp.url))
        form_match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', raw_html, re.IGNORECASE)
        if form_match:
            parsed_action = html.unescape(form_match.group(1))
            if parsed_action.startswith("/"):
                parsed_url = urlparse(str(resp.url))
                action = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_action}"
            else:
                action = parsed_action

        form_data: dict[str, str] = {}
        for input_match in re.finditer(r"<input[^>]+>", raw_html, re.IGNORECASE):
            attrs = input_match.group(0)
            name_m = re.search(r'name=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
            value_m = re.search(r'value=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
            if name_m:
                form_data[html.unescape(name_m.group(1))] = html.unescape(value_m.group(1)) if value_m else ""

        form_data["username"] = self.username
        form_data["password"] = self.password

        _LOGGER.debug("Submitting login form to %s", action)
        resp = await self.session.post(action, data=form_data, timeout=self.timeout)
        return self._extract_code_from_history(resp)

    def _extract_code_from_history(self, resp: aiohttp.ClientResponse) -> str | None:
        """Scan redirect history for the OAuth authorization code."""
        for r in [*list(resp.history), resp]:
            parsed = urlparse(str(r.url))
            if "callback" in parsed.path:
                qs = parse_qs(parsed.query)
                if "error" in qs:
                    _LOGGER.error("OAuth Error: %s", qs.get("error_description", qs["error"]))
                if "code" in qs:
                    return qs["code"][0]
        return None

    async def async_get_equipment(self) -> dict[str, dict[str, Any]]:
        """Get equipment keyed by full serial number. Cached after first call."""
        await self.async_login()
        if self._equipment:
            _LOGGER.debug(
                "Using cached %s equipment for site: %s",
                len(self._equipment),
                self.site_id,
            )
            return self._equipment

        _LOGGER.debug("Fetching equipment for site: %s", self.site_id)
        url = (
            f"https://monitoring.solaredge.com/services/layout/logical/generic/v2/site/{self.site_id}?include-optimizers=true"
        )
        try:
            resp = await self.session.get(url, headers=self._auth_headers, timeout=self.timeout)
            _LOGGER.debug("Got %s from %s", resp.status, url)
            resp.raise_for_status()
        except aiohttp.ClientError:
            _LOGGER.exception("Error fetching equipment from %s", url)
            raise

        resp_json = await resp.json()
        self._site_structure = resp_json.get("siteStructure", {})

        def extract_nested(node: dict[str, Any], data_dict: dict[str, dict[str, Any]]) -> None:
            node_type = node.get("type")
            # Skip container nodes; keep inverters, strings, optimizers.
            if node_type not in ("FOLDER", "SITE"):
                device_id = node.get("serial") or node.get("properties", {}).get("identifier") or node.get("uuid")
                if device_id:
                    data_dict[device_id] = node
            for child_node in node.get("children", []):
                extract_nested(child_node, data_dict)

        self._equipment = {}
        if self._site_structure:
            extract_nested(self._site_structure, self._equipment)
        _LOGGER.debug("Found %s equipment for site: %s", len(self._equipment), self.site_id)
        return self._equipment

    async def async_get_energy_data(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> list[EnergyData]:
        """Get hourly energy data. Values are in Wh.

        Returns per-optimizer, per-string, per-inverter, and site-level data.
        Uses compressPowerData (watts) from the playback API, converted to Wh
        by multiplying by the 1-hour slot duration. String/inverter/site values
        are aggregated by summing child optimizer values.

        If start_date/end_date are not provided, defaults to the last 7 days
        up to the end of today (in local time).
        """
        await self.async_login()
        await self.async_get_equipment()

        # Default to last 7 days up to the end of today (local time).
        # The API interprets dates as the site's local timezone despite the Z suffix.
        now = datetime.now()
        if end_date is None:
            end_date = now.replace(hour=23, minute=59, second=59, microsecond=999999)
        if start_date is None:
            start_date = (end_date - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)

        _LOGGER.debug(
            "Fetching playback data for site: %s (%s..%s)",
            self.site_id,
            start_date,
            end_date,
        )
        headers = dict(self._auth_headers)
        csrf_token_cookie = self._find_cookie("CSRF-TOKEN")
        if csrf_token_cookie and csrf_token_cookie.value:
            headers["X-CSRF-TOKEN"] = csrf_token_cookie.value

        start_str = _to_utc_iso(start_date)
        end_str = _to_utc_iso(end_date)
        url = (
            f"https://monitoring.solaredge.com/services/layout/playback/site/{self.site_id}"
            f"/optimizers-compact?resolution=hours"
            f"&start-date={start_str}&end-date={end_str}"
        )

        try:
            resp = await self.session.get(url, headers=headers, timeout=self.timeout)
            _LOGGER.debug("Got %s from %s", resp.status, url)
            resp.raise_for_status()
            resp_json = await resp.json()
        except aiohttp.ClientError:
            _LOGGER.exception("Error fetching energy data from %s", url)
            raise

        return _decode_playback(resp_json, start_date, self._site_structure)

    def _find_cookie(self, name: str) -> Morsel[str] | None:
        """Find a cookie by name on the monitoring domain."""
        for cookie in self.session.cookie_jar:
            if cookie["domain"] == "monitoring.solaredge.com" and cookie.key == name:
                return cookie
        return None


def _to_utc_iso(dt: datetime) -> str:
    """Convert datetime to ISO-8601 UTC string ending in Z."""
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _device_id(node: dict[str, Any]) -> str | None:
    """Extract the device ID from a layout node, matching async_get_equipment."""
    return node.get("serial") or node.get("properties", {}).get("identifier") or node.get("uuid")


def _build_opt_to_parent_map(
    site_structure: dict[str, Any],
) -> dict[str, list[str]]:
    """Map each optimizer short serial to its parent device IDs (string, inverter, site).

    Returns a dict like {"7A012345": ["7410983B_31", "7410983B-57", "TESTSITE01"]}.
    Keys match the equipment dict so the coordinator can look up aggregated values.
    """
    result: dict[str, list[str]] = {}

    def walk(node: dict[str, Any], ancestors: list[str]) -> None:
        node_type = node.get("type")

        # Only STRING, INVERTER, and SITE are aggregation parents.
        child_ancestors = ancestors.copy()
        if node_type in ("STRING", "INVERTER", "SITE"):
            dev_id = _device_id(node)
            if dev_id:
                child_ancestors.append(dev_id)

        if node_type == "OPTIMIZER":
            short_serial = (node.get("serial") or "").split("-")[0]
            if short_serial:
                result[short_serial] = child_ancestors

        for child in node.get("children", []):
            walk(child, child_ancestors)

    walk(site_structure, [])
    return result


def _decode_playback(
    resp_json: dict[str, Any],
    start_date: datetime,
    site_structure: dict[str, Any],
) -> list[EnergyData]:
    """Decode compact playback response into hourly EnergyData list.

    The compressPowerData array has a header [version, data_start_idx], then
    metadata pairs [meta_i, offset_i] per optimizer, then the power values.
    For optimizer i at slot s: value = compressPowerData[data_start_idx + offset_i + s].
    Values are in watts; with 1-hour slots, Wh = W * 1h.

    In addition to per-optimizer values, string/inverter/site values are
    aggregated by summing child optimizer values using the site layout.
    """
    time_slots = int(resp_json.get("timeSlotsCount", 0))
    serials: list[str] = list(resp_json.get("optimizerSerials", []))
    compress_power: list[Any] = list(resp_json.get("compressPowerData", []))

    if not compress_power or time_slots == 0 or not serials:
        _LOGGER.warning("No data returned or empty arrays in playback response.")
        return []

    data_start_idx = int(compress_power[1])

    # Map each optimizer short serial to its parent names for aggregation.
    opt_to_parents = _build_opt_to_parent_map(site_structure)

    # Map short serials to full serials using the site structure.
    short_to_full: dict[str, str] = {}

    def collect_serials(node: dict[str, Any]) -> None:
        if node.get("type") == "OPTIMIZER":
            full_serial = node.get("serial", "")
            if full_serial:
                short_to_full[full_serial.split("-")[0]] = full_serial
        for child in node.get("children", []):
            collect_serials(child)

    if site_structure:
        collect_serials(site_structure)

    # The API returns slots in the site's local timezone, not UTC, despite the
    # Z suffix in the request. We label slots with start_date as-is so callers
    # can interpret them as local time and convert to UTC if needed.
    slot_delta = timedelta(hours=1)

    energy_data_list: list[EnergyData] = []
    for slot in range(time_slots):
        slot_time = start_date + slot_delta * slot
        values: dict[str, float] = {}

        for opt_idx, short_serial in enumerate(serials):
            offset_idx = 3 + (opt_idx * 2)
            if offset_idx >= len(compress_power):
                continue
            offset = int(compress_power[offset_idx])
            val_idx = data_start_idx + offset + slot
            if val_idx >= len(compress_power):
                continue

            raw = compress_power[val_idx]
            if raw is None:
                continue
            try:
                power_w = float(raw)
            except (TypeError, ValueError):
                continue
            if power_w <= 0:
                continue

            # Per-optimizer value keyed by full serial (matching equipment dict).
            full_serial = short_to_full.get(short_serial, short_serial)
            values[full_serial] = values.get(full_serial, 0.0) + power_w

            # Aggregate into parent string, inverter, and site.
            for parent_name in opt_to_parents.get(short_serial, []):
                values[parent_name] = values.get(parent_name, 0.0) + power_w

        energy_data_list.append(EnergyData(start_time=slot_time, values=values))

    _LOGGER.debug("Decoded %s hourly slots for %s optimizers.", len(energy_data_list), len(serials))
    return energy_data_list


__all__ = [
    "EnergyData",
    "SolarEdgeWeb",
]
