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
from typing import TYPE_CHECKING, Any, NoReturn
from urllib.parse import parse_qs, urlencode, urlparse

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

_MONITORING_HOST = "monitoring.solaredge.com"
# Credentials and OAuth codes are only ever sent to / accepted from this domain.
_SOLAREDGE_DOMAIN = "solaredge.com"

# Cookie set by the monitoring backend once the OAuth token exchange succeeds.
# Its presence means the session is still usable and login can be skipped.
_SESSION_COOKIE_NAME = "se_monitoring_auth"

# Refresh SSO session at most every hour to avoid re-issuing the OAuth flow.
_LOGIN_REFRESH_SECONDS = 3600

_LAYOUT_BASE_URL = "https://monitoring.solaredge.com/services/layout"
_DASHBOARD_BASE_URL = "https://monitoring.solaredge.com/services/dashboard"

_PLAYBACK_BASE_URL = f"{_LAYOUT_BASE_URL}/playback/site"
# The compact endpoint returns a packed array; the verbose one returns explicit
# per-measurement timestamps. They disagree on how the date range is read, see
# ``_async_fetch_playback``.
_PLAYBACK_COMPACT = "optimizers-compact"
_PLAYBACK_VERBOSE = "optimizers"

# Both playback endpoints reject a range wider than this with BAD_ARGUMENTS.
_MAX_PLAYBACK_SPAN = timedelta(days=8)

# Site-level production/consumption. The power endpoint answers in watts and
# serves sub-daily slots; the energy endpoint answers in watt-hours and serves
# daily and coarser slots. They share the same measurement shape.
# Measured energy per optimizer/string/inverter over a date range.
_BY_INVERTER_URL = f"{_LAYOUT_BASE_URL}/energy/site"

# Units the by-inverter endpoint reports energy in, as a factor to Wh.
_ENERGY_UNIT_TO_WH = {"watt-hour": 1.0, "kilo-watt-hour": 1000.0, "mega-watt-hour": 1000000.0}

_SITE_POWER_URL = f"{_DASHBOARD_BASE_URL}/power/sites"
_SITE_ENERGY_URL = f"{_DASHBOARD_BASE_URL}/energy/sites"

# Accepted chart-time-unit values, mapped to the hours one slot covers.
# Only the first two are valid for the power endpoint, only the rest for the
# energy one; "hours" is rejected by the energy endpoint.
_SLOT_HOURS = {
    "quarter-hours": 0.25,
    "hours": 1.0,
    "days": 24.0,
    "months": None,
    "years": None,
}
_POWER_RESOLUTIONS = ("quarter-hours", "hours")

# Widest span each resolution accepts before answering BAD_ARGUMENTS. Measured
# against the live API; months and years are far wider than anyone asks for.
_MAX_CONSUMPTION_SPAN = {
    "quarter-hours": timedelta(days=7),
    "hours": timedelta(days=31),
    "days": timedelta(days=99),
}


def _raise_login_error(
    resp: aiohttp.ClientResponse,
    message: str = "Failed to extract authorization code during login.",
) -> NoReturn:
    """Raise a 401 for a failed login (extracted to satisfy TRY301).

    ``ClientResponseError`` rather than a plain ``ClientError`` so that callers
    can tell bad credentials apart from a transient failure. Home Assistant's
    config flow maps status 401/403 to ``invalid_auth``.
    """
    raise aiohttp.ClientResponseError(
        request_info=resp.request_info,
        history=resp.history,
        status=401,
        message=message,
    )


def _is_solaredge_url(url: str) -> bool:
    """Return True if the URL points at a solaredge.com host."""
    host = (urlparse(url).hostname or "").lower()
    return host == _SOLAREDGE_DOMAIN or host.endswith(f".{_SOLAREDGE_DOMAIN}")


@dataclasses.dataclass
class EnergyData:
    """Energy data for a single hourly time slot.

    start_time is naive and expressed in the site's local time.

    values maps equipment serial numbers to energy in Wh. Optimizer values
    are from the API; string, inverter, and site values are aggregated by
    summing child optimizer values.
    """

    start_time: datetime
    values: dict[str, float]


@dataclasses.dataclass
class ConsumptionData:
    """Site-level energy for a single time slot. Values are in Wh.

    start_time is naive and expressed in the site's local time, matching
    :class:`EnergyData`.

    A value is ``None`` when the site cannot measure it. Sites without a
    consumption meter (``hasConsumptionAndGrid`` is false in
    :meth:`SolarEdgeWeb.async_get_site_components`) only report production;
    everything else stays ``None`` rather than being reported as zero.
    """

    start_time: datetime
    production: float | None = None
    consumption: float | None = None
    imported: float | None = None
    exported: float | None = None
    self_consumption: float | None = None
    consumption_from_grid: float | None = None
    production_to_home: float | None = None
    production_to_grid: float | None = None


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
        self._site_components: dict[str, Any] = {}
        self._last_login_time = 0.0
        self._auth_headers: dict[str, str] = {}
        self._site_utc_offset: timedelta | None = None

    async def async_login(self) -> None:
        """Login via OAuth2 PKCE. Reuses the monitoring session for 1 hour."""
        session_cookie = self._find_cookie(_SESSION_COOKIE_NAME)
        if (
            session_cookie is not None
            and self._auth_headers
            and (time.time() - self._last_login_time < _LOGIN_REFRESH_SECONDS)
        ):
            _LOGGER.debug("Skipping login. Reusing monitoring session and auth headers.")
            return

        _LOGGER.debug("Starting OAuth2 login flow...")

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
            # Drain the body so the connection returns to the pool even when the
            # authorization code is already present in the redirect history.
            await resp.read()
            code = self._extract_code_from_history(resp)

            if not code:
                code = await self._submit_login_form(resp)

            if not code:
                _raise_login_error(resp)

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

            auth_headers = {"Authorization": f"Bearer {oauth_tokens['access_token']}"}

            # Establish the backend monitoring session.
            resp = await self.session.post(
                _AUTH_EXCHANGE_URL,
                json=oauth_tokens,
                headers=auth_headers,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            await resp.read()

        except aiohttp.ClientError:
            _LOGGER.exception("Error during SolarEdge login")
            raise

        # Only invalidate the cached layout once the new session is established,
        # so a failed login does not throw away a still-usable cache.
        self._auth_headers = auth_headers
        self._equipment = {}
        self._site_structure = {}
        self._site_components = {}
        self._last_login_time = time.time()
        _LOGGER.debug("Successfully completed OAuth2 login flow.")

    async def _submit_login_form(self, resp: aiohttp.ClientResponse) -> str | None:
        """Parse the login form and submit credentials. Returns the OAuth code."""
        raw_html = await resp.text()
        action = html.unescape(str(resp.url))
        form_match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', raw_html, re.IGNORECASE)
        form_html = raw_html
        if form_match:
            parsed_action = html.unescape(form_match.group(1))
            if parsed_action.startswith("/"):
                parsed_url = urlparse(str(resp.url))
                action = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_action}"
            else:
                action = parsed_action
            # Only scrape inputs belonging to this form, not the whole page.
            form_end = raw_html.lower().find("</form>", form_match.end())
            form_html = raw_html[form_match.end() : form_end if form_end != -1 else len(raw_html)]

        # Never post credentials to a host outside solaredge.com.
        if not _is_solaredge_url(action):
            _LOGGER.error("Refusing to submit credentials to unexpected host: %s", action)
            _raise_login_error(resp, "Login form points at an unexpected host.")

        form_data = _extract_form_inputs(form_html)
        if not form_data:
            # Fall back to the whole page if the form span looked empty.
            form_data = _extract_form_inputs(raw_html)

        form_data["username"] = self.username
        form_data["password"] = self.password

        _LOGGER.debug("Submitting login form to %s", action)
        resp = await self.session.post(action, data=form_data, timeout=self.timeout)
        await resp.read()
        return self._extract_code_from_history(resp)

    def _extract_code_from_history(self, resp: aiohttp.ClientResponse) -> str | None:
        """Scan redirect history for the OAuth authorization code."""
        for r in [*resp.history, resp]:
            url = str(r.url)
            if not _is_solaredge_url(url):
                continue
            parsed = urlparse(url)
            if "callback" in parsed.path:
                qs = parse_qs(parsed.query)
                if "error" in qs:
                    _LOGGER.error("OAuth Error: %s", qs.get("error_description", qs["error"]))
                if "code" in qs:
                    return qs["code"][0]
        return None

    async def async_get_equipment(self, include_inactive: bool = False) -> dict[str, dict[str, Any]]:
        """Get equipment keyed by full serial number. Cached after first call.

        Retired/replaced equipment (``properties.status == "INACTIVE"``) is
        excluded by default, because it keeps the same display name as its live
        replacement. Pass ``include_inactive=True`` to include those units.
        """
        await self.async_login()
        if self._equipment:
            _LOGGER.debug(
                "Using cached %s equipment for site: %s",
                len(self._equipment),
                self.site_id,
            )
            return self._equipment if include_inactive else _exclude_inactive(self._equipment)

        _LOGGER.debug("Fetching equipment for site: %s", self.site_id)
        url = f"{_LAYOUT_BASE_URL}/logical/generic/v2/site/{self.site_id}?include-optimizers=true"
        resp_json = await self._async_get_json(url, "equipment")
        self._site_structure = resp_json.get("siteStructure", {})

        def extract_nested(node: dict[str, Any], data_dict: dict[str, dict[str, Any]]) -> None:
            node_type = node.get("type")
            # Skip container nodes; keep inverters, strings, optimizers.
            if node_type not in ("FOLDER", "SITE"):
                device_id = _device_id(node)
                if device_id:
                    data_dict[device_id] = node
            for child_node in node.get("children", []):
                extract_nested(child_node, data_dict)

        self._equipment = {}
        if self._site_structure:
            extract_nested(self._site_structure, self._equipment)
        _LOGGER.debug("Found %s equipment for site: %s", len(self._equipment), self.site_id)
        return self._equipment if include_inactive else _exclude_inactive(self._equipment)

    async def async_get_site_components(self) -> dict[str, Any]:
        """Get which features the site has. Cached until the next login.

        Useful keys: ``hasConsumptionAndGrid`` (a consumption/import-export
        meter is installed), ``hasStorage``, ``hasProduction``, ``siteType``
        and ``inverterCount``. Without a consumption meter every consumption
        value returned by :meth:`async_get_consumption_data` is ``None``.
        """
        await self.async_login()
        if self._site_components:
            _LOGGER.debug("Using cached site components for site: %s", self.site_id)
            return self._site_components

        _LOGGER.debug("Fetching site components for site: %s", self.site_id)
        url = f"{_DASHBOARD_BASE_URL}/site-details/{self.site_id}/components"
        self._site_components = await self._async_get_json(url, "site components")
        return self._site_components

    async def async_get_data_availability(self) -> dict[str, Any]:
        """Get the date range the site has data for.

        Returns ``productionDataAvailableFrom``, ``consumptionDataAvailableFrom``
        (``None`` without a consumption meter), ``productionDataAvailableUntil``
        and ``lastUpdateTime``. Dates are in the site's local timezone.
        """
        await self.async_login()
        _LOGGER.debug("Fetching data availability for site: %s", self.site_id)
        url = f"{_DASHBOARD_BASE_URL}/data-availability/sites/{self.site_id}"
        return await self._async_get_json(url, "data availability")

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

        Some sites answer the compact endpoint with a header-only payload; for
        those the verbose ``optimizers`` endpoint is used as a fallback.

        If start_date/end_date are not provided, defaults to the last 7 days
        up to the end of today (in local time). The API rejects ranges wider
        than 7 days with HTTP 400, so the default is already at the maximum.
        """
        await self.async_get_equipment()

        # Default to last 7 days up to the end of today (local time).
        # The compact API interprets dates as the site's local timezone despite
        # the Z suffix.
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

        if _as_naive(end_date) - _as_naive(start_date) > _MAX_PLAYBACK_SPAN:
            _LOGGER.warning(
                "Requested range %s..%s is wider than the 7 days the API allows; expect HTTP 400",
                start_date,
                end_date,
            )

        resp_json = await self._async_fetch_playback(_PLAYBACK_COMPACT, start_date, end_date)
        energy_data = _decode_playback(resp_json, start_date, self._site_structure)
        if any(data.values for data in energy_data):
            return energy_data

        # The compact endpoint returns HTTP 200 with an empty payload on some
        # sites. Without this fallback every slot would be silently zero.
        # See https://github.com/Solarlibs/solaredge-web/issues/13
        _LOGGER.warning(
            "The compact playback endpoint returned no measurements for site %s. "
            "Falling back to the verbose optimizers endpoint",
            self.site_id,
        )
        return await self._async_get_energy_data_verbose(_as_naive(start_date), _as_naive(end_date))

    async def _async_get_energy_data_verbose(self, start_date: datetime, end_date: datetime) -> list[EnergyData]:
        """Fetch hourly energy data from the verbose playback endpoint.

        The verbose endpoint reads the range as real UTC while the compact one
        reads it as site-local, so the window has to be shifted by the site's
        UTC offset. That offset is published nowhere in the layout, so it is
        learned from the measurementTime of a first response and then cached;
        re-reading it every time lets the cache self-correct across DST.
        Widening the range instead is not an option: the API caps it at 7 days.
        """
        offset = self._site_utc_offset or timedelta(0)
        resp_json = await self._async_fetch_playback(_PLAYBACK_VERBOSE, start_date - offset, end_date - offset)

        observed = _extract_utc_offset(resp_json)
        if observed is not None and observed != offset:
            _LOGGER.debug("Site UTC offset is %s; re-requesting the shifted window", observed)
            self._site_utc_offset = observed
            resp_json = await self._async_fetch_playback(_PLAYBACK_VERBOSE, start_date - observed, end_date - observed)
        elif observed is None:
            _LOGGER.warning("Could not determine the site UTC offset for site %s", self.site_id)

        return _decode_playback_verbose(resp_json, self._site_structure, start_date, end_date)

    async def _async_fetch_playback(self, endpoint: str, start_date: datetime, end_date: datetime) -> dict[str, Any]:
        """Fetch a playback response for the given endpoint and date range."""
        url = (
            f"{_PLAYBACK_BASE_URL}/{self.site_id}/{endpoint}"
            f"?resolution=hours"
            f"&start-date={_to_utc_iso(start_date)}&end-date={_to_utc_iso(end_date)}"
        )
        return await self._async_get_json(url, "energy data")

    async def async_get_consumption_data(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        resolution: str = "hours",
    ) -> list[ConsumptionData]:
        """Get site-level production and consumption. Values are in Wh.

        ``resolution`` is one of ``quarter-hours``, ``hours``, ``days``,
        ``months`` or ``years``. Sub-daily resolutions come from the power
        endpoint in watts and are converted to Wh by multiplying by the slot
        duration, the same way :meth:`async_get_energy_data` converts playback
        data, so hourly slots from both methods line up. Daily and coarser
        resolutions are reported by the API in Wh already.

        If start_date/end_date are not provided, defaults to the last 7 days up
        to today, in the site's local time. The API rejects wider ranges with
        HTTP 400: 7 days for ``quarter-hours``, 31 days for ``hours`` and
        99 days for ``days``.

        Consumption, import and export are ``None`` unless the site has a
        consumption meter; see :meth:`async_get_site_components`.
        """
        if resolution not in _SLOT_HOURS:
            msg = f"Unsupported resolution {resolution!r}; expected one of {', '.join(_SLOT_HOURS)}"
            raise ValueError(msg)

        components = await self.async_get_site_components()

        # The API takes plain dates and reads them in the site's timezone.
        end = (_as_naive(end_date) if end_date else datetime.now()).date()
        start = _as_naive(start_date).date() if start_date else end - timedelta(days=7)
        max_span = _MAX_CONSUMPTION_SPAN.get(resolution)
        if max_span is not None and end - start > max_span:
            _LOGGER.warning(
                "Requested range %s..%s is wider than the %s days the API allows for %s; expect HTTP 400",
                start,
                end,
                max_span.days,
                resolution,
            )

        is_power = resolution in _POWER_RESOLUTIONS
        base_url = _SITE_POWER_URL if is_power else _SITE_ENERGY_URL
        params = [
            ("chart-time-unit", resolution),
            ("start-date", start.isoformat()),
            ("end-date", end.isoformat()),
            *[("measurement-types", t) for t in _measurement_types(bool(components.get("hasStorage")))],
        ]
        url = f"{base_url}/{self.site_id}?{urlencode(params)}"

        _LOGGER.debug(
            "Fetching %s consumption data for site: %s (%s..%s)",
            resolution,
            self.site_id,
            start,
            end,
        )
        resp_json = await self._async_get_json(url, "consumption data")

        # The power endpoint returns the measurements at the top level; the
        # energy endpoint nests them under "chart" next to a summary.
        if is_power:
            measurements = resp_json.get("measurements", [])
            # Watts over a slot of known length; Wh = W * hours.
            scale = _SLOT_HOURS[resolution] or 1.0
        else:
            measurements = resp_json.get("chart", {}).get("measurements", [])
            scale = 1.0
        return _decode_dashboard_measurements(measurements, scale)

    async def async_get_energy_totals(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, float]:
        """Get total energy per equipment over a date range. Values are in Wh.

        Keyed like :attr:`EnergyData.values`, so per-optimizer, per-string,
        per-inverter and site totals can be looked up with the same ids
        :meth:`async_get_equipment` returns.

        Unlike :meth:`async_get_energy_data`, which sums hourly playback power
        and therefore approximates, these are the energy figures the API
        itself reports, with an explicit unit. There is no time series: the
        result is one total per device for the whole range.

        If start_date/end_date are not provided, defaults to today.
        """
        await self.async_get_equipment()

        end = (_as_naive(end_date) if end_date else datetime.now()).date()
        start = _as_naive(start_date).date() if start_date else end

        inverter_serials = _collect_inverter_serials(self._site_structure)
        if not inverter_serials:
            _LOGGER.warning("No inverters found in the layout for site %s", self.site_id)
            return {}

        params = [
            ("start-date", start.isoformat()),
            ("end-date", end.isoformat()),
            *[("inverter-serials", serial) for serial in inverter_serials],
            ("include-max-temperature", "false"),
            ("include-color", "true"),
        ]
        url = f"{_BY_INVERTER_URL}/{self.site_id}/by-inverter?{urlencode(params)}"

        _LOGGER.debug("Fetching energy totals for site: %s (%s..%s)", self.site_id, start, end)
        resp_json = await self._async_get_json(url, "energy totals")
        return _decode_energy_totals(resp_json, self._site_structure)

    async def _async_get_json(self, url: str, description: str) -> dict[str, Any]:
        """GET a monitoring API endpoint and return the decoded JSON body.

        Sends the bearer token from the last login plus the CSRF token the
        backend hands out as a cookie; endpoints behind the API gateway reject
        the request without it.
        """
        headers = dict(self._auth_headers)
        csrf_token_cookie = self._find_cookie("CSRF-TOKEN")
        if csrf_token_cookie and csrf_token_cookie.value:
            headers["X-CSRF-TOKEN"] = csrf_token_cookie.value

        try:
            resp = await self.session.get(url, headers=headers, timeout=self.timeout)
            _LOGGER.debug("Got %s from %s", resp.status, url)
            resp.raise_for_status()
            resp_json: dict[str, Any] = await resp.json()
        except aiohttp.ClientError:
            _LOGGER.exception("Error fetching %s from %s", description, url)
            raise
        return resp_json

    def _find_cookie(self, name: str, host: str = _MONITORING_HOST) -> Morsel[str] | None:
        """Find a cookie by name that applies to the given host.

        Matches parent domains too, so a cookie scoped to ``solaredge.com``
        is still found for ``monitoring.solaredge.com``.
        """
        for cookie in self.session.cookie_jar:
            if cookie.key != name:
                continue
            domain = cookie["domain"]
            if domain == host or host.endswith(f".{domain}"):
                return cookie
        return None


def _extract_form_inputs(raw_html: str) -> dict[str, str]:
    """Collect name/value pairs from every ``<input>`` in the given HTML."""
    form_data: dict[str, str] = {}
    for input_match in re.finditer(r"<input[^>]+>", raw_html, re.IGNORECASE):
        attrs = input_match.group(0)
        name_m = re.search(r'name=["\']([^"\']+)["\']', attrs, re.IGNORECASE)
        value_m = re.search(r'value=["\']([^"\']*)["\']', attrs, re.IGNORECASE)
        if name_m:
            form_data[html.unescape(name_m.group(1))] = html.unescape(value_m.group(1)) if value_m else ""
    return form_data


def _as_naive(dt: datetime) -> datetime:
    """Drop tzinfo, keeping the wall-clock time."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _to_utc_iso(dt: datetime) -> str:
    """Convert datetime to ISO-8601 UTC string ending in Z."""
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _device_id(node: dict[str, Any]) -> str | None:
    """Extract the device ID from a layout node, matching async_get_equipment."""
    return node.get("serial") or node.get("properties", {}).get("identifier") or node.get("uuid")


def _exclude_inactive(equipment: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return equipment without retired (``properties.status == "INACTIVE"``) units."""
    return {
        equipment_id: data
        for equipment_id, data in equipment.items()
        if data.get("properties", {}).get("status") != "INACTIVE"
    }


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


def _collect_optimizer_serials(site_structure: dict[str, Any]) -> dict[str, str]:
    """Map optimizer short serials to full serials using the site structure."""
    short_to_full: dict[str, str] = {}

    def collect(node: dict[str, Any]) -> None:
        if node.get("type") == "OPTIMIZER":
            full_serial = node.get("serial", "")
            if full_serial:
                short_to_full[full_serial.split("-")[0]] = full_serial
        for child in node.get("children", []):
            collect(child)

    if site_structure:
        collect(site_structure)
    return short_to_full


def _add_value(values: dict[str, float], key: str, power_w: float) -> None:
    """Accumulate a power value under the given key."""
    values[key] = values.get(key, 0.0) + power_w


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
    serials: list[str] = list(resp_json.get("optimizerSerials", []))
    compress_power: list[Any] = list(resp_json.get("compressPowerData", []))
    try:
        time_slots = int(resp_json.get("timeSlotsCount", 0))
    except (TypeError, ValueError):
        _LOGGER.warning("Invalid timeSlotsCount in playback response: %r", resp_json.get("timeSlotsCount"))
        return []

    if not compress_power or time_slots == 0 or not serials:
        _LOGGER.warning("No data returned or empty arrays in playback response.")
        return []

    # Header is [version, data_start_idx] followed by a [meta, offset] pair per
    # optimizer. Anything shorter carries no measurements at all, which some
    # sites return with HTTP 200. Reporting it beats emitting silent zeros.
    header_len = 2 + 2 * len(serials)
    if len(compress_power) <= header_len:
        _LOGGER.warning(
            "Playback response contains no measurements: compressPowerData has %s entries but %s optimizers need more than %s",
            len(compress_power),
            len(serials),
            header_len,
        )
        return []

    try:
        data_start_idx = int(compress_power[1])
    except (TypeError, ValueError):
        _LOGGER.warning("Invalid compressPowerData header: %r", compress_power[:2])
        return []

    # Map each optimizer short serial to its parent names for aggregation.
    opt_to_parents = _build_opt_to_parent_map(site_structure)
    short_to_full = _collect_optimizer_serials(site_structure)

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
            _add_value(values, short_to_full.get(short_serial, short_serial), power_w)

            # Aggregate into parent string, inverter, and site.
            for parent_name in opt_to_parents.get(short_serial, []):
                _add_value(values, parent_name, power_w)

        energy_data_list.append(EnergyData(start_time=slot_time, values=values))

    _LOGGER.debug("Decoded %s hourly slots for %s optimizers.", len(energy_data_list), len(serials))
    return energy_data_list


def _measurement_types(has_storage: bool) -> list[str]:
    """Return the measurement-types the web app asks for on this kind of site.

    The distribution breakdowns come in with-storage and without-storage
    flavors; asking for the wrong one loses the battery legs.
    """
    suffix = "with-storage" if has_storage else "without-storage"
    return [
        "production",
        "consumption",
        "import",
        "export",
        f"production-distribution-{suffix}",
        f"consumption-distribution-{suffix}",
    ]


def _as_float(value: Any, scale: float = 1.0) -> float | None:
    """Convert an API value to a float, keeping None (unmeasured) as None."""
    if value is None:
        return None
    try:
        return float(value) * scale
    except (TypeError, ValueError):
        return None


def _decode_dashboard_measurements(measurements: list[dict[str, Any]], scale: float) -> list[ConsumptionData]:
    """Decode dashboard power/energy measurements into ConsumptionData.

    ``scale`` converts a raw value into Wh: the slot length in hours for the
    power endpoint (which answers in watts), 1.0 for the energy endpoint.
    """
    if not measurements:
        _LOGGER.warning("No measurements returned in dashboard response.")
        return []

    result: list[ConsumptionData] = []
    for measurement in measurements:
        raw_time = measurement.get("measurementTime")
        if not raw_time:
            continue
        try:
            # The offset is the site's, so dropping it yields site-local time.
            slot_time = _as_naive(datetime.fromisoformat(raw_time))
        except (TypeError, ValueError):
            _LOGGER.warning("Skipping measurement with invalid time: %r", raw_time)
            continue

        production_distribution = measurement.get("productionDistribution") or {}
        consumption_distribution = measurement.get("consumptionDistribution") or {}
        result.append(
            ConsumptionData(
                start_time=slot_time,
                production=_as_float(measurement.get("production"), scale),
                consumption=_as_float(measurement.get("consumption"), scale),
                imported=_as_float(measurement.get("import"), scale),
                exported=_as_float(measurement.get("export"), scale),
                self_consumption=_as_float(consumption_distribution.get("consumptionFromSolar"), scale),
                consumption_from_grid=_as_float(consumption_distribution.get("consumptionFromGrid"), scale),
                production_to_home=_as_float(production_distribution.get("productionToHome"), scale),
                production_to_grid=_as_float(production_distribution.get("productionToGrid"), scale),
            )
        )

    _LOGGER.debug("Decoded %s dashboard measurements.", len(result))
    return result


def _collect_inverter_serials(site_structure: dict[str, Any]) -> list[str]:
    """List the serials of every inverter in the layout, in document order."""
    serials: list[str] = []

    def collect(node: dict[str, Any]) -> None:
        if node.get("type") == "INVERTER":
            serial = node.get("serial")
            if serial:
                serials.append(serial)
        for child in node.get("children", []):
            collect(child)

    if site_structure:
        collect(site_structure)
    return serials


def _build_string_id_map(site_structure: dict[str, Any]) -> dict[tuple[str, int], str]:
    """Map (inverter serial, string order) to the string's device id.

    The by-inverter response identifies strings only by ``stringRelativeOrder``,
    a 1-based position within their inverter, so they have to be matched back
    to the layout by position.
    """
    result: dict[tuple[str, int], str] = {}

    def walk(node: dict[str, Any], inverter_serial: str | None, counter: list[int]) -> None:
        node_type = node.get("type")
        if node_type == "INVERTER":
            inverter_serial = node.get("serial")
            counter = [0]
        if node_type == "STRING" and inverter_serial:
            counter[0] += 1
            order = node.get("order")
            try:
                relative_order = int(order) if order is not None else counter[0]
            except (TypeError, ValueError):
                relative_order = counter[0]
            device_id = _device_id(node)
            if device_id:
                result[(inverter_serial, relative_order)] = device_id
        for child in node.get("children", []):
            walk(child, inverter_serial, counter)

    if site_structure:
        walk(site_structure, None, [0])
    return result


def _energy_wh(energy: Any) -> float | None:
    """Read an ``{"value": .., "unit": ..}`` object as watt-hours."""
    if not isinstance(energy, dict):
        return None
    value = _as_float(energy.get("value"))
    if value is None:
        return None
    unit = str(energy.get("unit") or "watt-hour").lower()
    factor = _ENERGY_UNIT_TO_WH.get(unit)
    if factor is None:
        _LOGGER.warning("Unknown energy unit %r; assuming watt-hour", unit)
        factor = 1.0
    return value * factor


def _decode_energy_totals(resp_json: dict[str, Any], site_structure: dict[str, Any]) -> dict[str, float]:
    """Decode a by-inverter response into energy totals keyed by device id.

    The site total is the sum of its inverters; the response has no site entry.
    """
    inverters = resp_json.get("inverters", [])
    if not inverters:
        _LOGGER.warning("No inverters returned in the by-inverter energy response.")
        return {}

    string_ids = _build_string_id_map(site_structure)
    site_key = _device_id(site_structure) if site_structure else None

    totals: dict[str, float] = {}
    for inverter in inverters:
        serial = inverter.get("serial")
        energy = _energy_wh(inverter.get("energy"))
        if serial and energy is not None:
            _add_value(totals, serial, energy)
            if site_key:
                _add_value(totals, site_key, energy)

        for position, string in enumerate(inverter.get("strings", []), start=1):
            order = string.get("stringRelativeOrder", position)
            energy = _energy_wh(string.get("energy"))
            try:
                string_key = string_ids.get((serial, int(order)))
            except (TypeError, ValueError):
                string_key = None
            if string_key and energy is not None:
                _add_value(totals, string_key, energy)

        for optimizer in inverter.get("optimizers", []):
            optimizer_serial = optimizer.get("serial")
            energy = _energy_wh(optimizer.get("energy"))
            if optimizer_serial and energy is not None:
                _add_value(totals, optimizer_serial, energy)

    _LOGGER.debug("Decoded energy totals for %s devices.", len(totals))
    return totals


def _extract_utc_offset(resp_json: dict[str, Any]) -> timedelta | None:
    """Read the site's UTC offset from the first dated measurement, if any."""
    for entry in resp_json.get("optimizerPowerMeasurementsList", []):
        for measurement in entry.get("powerMeasurements", []):
            raw_time = measurement.get("measurementTime")
            if not raw_time:
                continue
            try:
                parsed = datetime.fromisoformat(raw_time)
            except (TypeError, ValueError):
                continue
            if parsed.tzinfo is not None:
                return parsed.utcoffset()
    return None


def _decode_playback_verbose(
    resp_json: dict[str, Any],
    site_structure: dict[str, Any],
    start_date: datetime | None = None,
    end_date: datetime | None = None,
) -> list[EnergyData]:
    """Decode the verbose playback response into an hourly EnergyData list.

    Unlike the compact response, each measurement carries an explicit
    ``measurementTime`` already offset to the site's local timezone, so slot
    times are read from the payload instead of being derived from start_date.
    Only slots with production are present, and results are filtered to
    [start_date, end_date] when given.
    """
    measurements_list: list[dict[str, Any]] = list(resp_json.get("optimizerPowerMeasurementsList", []))
    if not measurements_list:
        _LOGGER.warning("No data returned in verbose playback response.")
        return []

    opt_to_parents = _build_opt_to_parent_map(site_structure)
    short_to_full = _collect_optimizer_serials(site_structure)
    window_start = _as_naive(start_date) if start_date else None
    window_end = _as_naive(end_date) if end_date else None

    slots: dict[datetime, dict[str, float]] = {}
    for entry in measurements_list:
        short_serial = (entry.get("serial") or "").split("-")[0]
        if not short_serial:
            continue
        full_serial = short_to_full.get(short_serial, short_serial)
        parents = opt_to_parents.get(short_serial, [])

        for measurement in entry.get("powerMeasurements", []):
            raw_time = measurement.get("measurementTime")
            if not raw_time:
                continue
            try:
                # The offset is the site's, so dropping it yields site-local time.
                slot_time = _as_naive(datetime.fromisoformat(raw_time))
                power_w = float(measurement.get("powerW"))
            except (TypeError, ValueError):
                continue
            if power_w <= 0:
                continue
            if (window_start and slot_time < window_start) or (window_end and slot_time > window_end):
                continue

            values = slots.setdefault(slot_time, {})
            _add_value(values, full_serial, power_w)
            for parent_name in parents:
                _add_value(values, parent_name, power_w)

    _LOGGER.debug("Decoded %s hourly slots for %s optimizers (verbose).", len(slots), len(measurements_list))
    return [EnergyData(start_time=slot_time, values=slots[slot_time]) for slot_time in sorted(slots)]


__all__ = [
    "ConsumptionData",
    "EnergyData",
    "SolarEdgeWeb",
]
