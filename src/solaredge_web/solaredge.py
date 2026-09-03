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
from datetime import date, datetime, timedelta, timezone
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
# Static site facts, including the site's IANA timezone.
_SITE_INFORMATION_URL = f"{_LAYOUT_BASE_URL}/information/site"

# Live per-device readings. The optimizer endpoint takes the serials to read
# as a JSON array body, so a whole site costs one request.
_OPTIMIZER_INFORMATION_URL = f"{_LAYOUT_BASE_URL}/information/optimizers"
_INVERTER_INFORMATION_URL = f"{_LAYOUT_BASE_URL}/information/inverters"

# Measured energy: the site total, and the per-optimizer/string/inverter
# breakdown under the /by-inverter suffix.
_LAYOUT_ENERGY_URL = f"{_LAYOUT_BASE_URL}/energy/site"

# Site-level energy per time slot, as the web app charts it. The /optimizers
# suffix charts a set of optimizers instead, summed into a single series.
_ENERGY_GRAPH_URL = f"{_LAYOUT_BASE_URL}/energy-graph/site"

# Site power per time slot. Accepts hours (multi-day) and quarter-hours
# (a single day); days is rejected.
_SITE_PLAYBACK_SPANS = {"hours": (timedelta(days=7), timedelta(days=7)), "quarter-hours": (timedelta(0), timedelta(0))}

# Widest span this endpoint accepts per resolution, and the default span used
# when the caller does not pass dates. "hours" only serves a single day.
_SITE_ENERGY_SPANS = {
    "hours": (timedelta(0), timedelta(0)),
    "days": (timedelta(days=30), timedelta(days=7)),
    "months": (timedelta(days=364), timedelta(days=364)),
    "years": (None, timedelta(days=3650)),
}

# Units the by-inverter endpoint reports energy in, as a factor to Wh.
_ENERGY_UNIT_TO_WH = {"watt-hour": 1.0, "kilo-watt-hour": 1000.0, "mega-watt-hour": 1000000.0}

# Live figures. live-power reports watts precisely; power-flow rounds to kW
# but adds the site status and, on metered sites, the consumption/grid legs.
_LIVE_POWER_URL = f"{_DASHBOARD_BASE_URL}/live-power/sites"
_POWER_FLOW_URL = f"{_DASHBOARD_BASE_URL}/power-flow/v2/sites"

# Per-inverter energy totals and power series.
_INVERTER_ENERGY_URL = f"{_DASHBOARD_BASE_URL}/inverters/energy/sites"
_INVERTER_POWER_URL = f"{_DASHBOARD_BASE_URL}/inverters/power/sites"
_MAX_INVERTER_POWER_SPAN = {"quarter-hours": timedelta(days=7), "hours": timedelta(days=31)}

_SITE_POWER_URL = f"{_DASHBOARD_BASE_URL}/power/sites"
_SITE_ENERGY_URL = f"{_DASHBOARD_BASE_URL}/energy/sites"

# Sub-daily chart-time-unit values, mapped to the hours one slot covers, which
# is what turns the watts the power endpoint answers with into Wh. The energy
# endpoint serves the coarser units and answers in Wh already; it rejects
# "hours", and the power endpoint rejects everything below.
_POWER_SLOT_HOURS = {"quarter-hours": 0.25, "hours": 1.0}
_ENERGY_RESOLUTIONS = ("days", "months", "years")
_CONSUMPTION_RESOLUTIONS = (*_POWER_SLOT_HOURS, *_ENERGY_RESOLUTIONS)

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


@dataclasses.dataclass
class SiteEnergyData:
    """Site-level energy for a single time slot. Value is in Wh.

    start_time is naive and expressed in the site's local time. ``energy`` is
    ``None`` for slots the site has not reported yet.
    """

    start_time: datetime
    energy: float | None


@dataclasses.dataclass
class SitePowerData:
    """Site-level power for a single time slot. Value is in W.

    start_time is naive and expressed in the site's local time. ``power`` is
    ``None`` for slots the site has not reported.
    """

    start_time: datetime
    power: float | None


@dataclasses.dataclass
class InverterPowerData:
    """Per-inverter power for a single time slot. Values are in W.

    start_time is naive and expressed in the site's local time. ``values`` maps
    inverter serials to power; inverters that did not report for a slot are
    absent rather than zero.
    """

    start_time: datetime
    values: dict[str, float]


@dataclasses.dataclass
class LivePower:
    """The site's current power. Values are in W.

    last_update_time is naive and expressed in the site's local time.

    ``power_flow`` is the raw power-flow payload. Sites with a consumption
    meter or a battery report extra legs there (consumption, grid, storage)
    that a production-only site does not have, so it is passed through rather
    than flattened.
    """

    current_power: float | None
    max_power: float | None
    is_communicating: bool | None
    last_update_time: datetime | None
    power_flow: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class OptimizerData:
    """Live readings for a single optimizer.

    Power is in W, voltages in V and current in A. ``voltage`` is the module
    side, ``optimizer_voltage`` the optimizer's output.

    Unlike every other timestamp in this module, ``last_measurement`` is an
    aware UTC datetime, because this endpoint reports a real UTC instant
    rather than the site's local time.
    """

    serial: str
    power: float | None = None
    voltage: float | None = None
    optimizer_voltage: float | None = None
    current: float | None = None
    last_measurement: datetime | None = None
    model: str | None = None
    modules: list[dict[str, Any]] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class InverterData:
    """Live readings and firmware details for a single inverter.

    Power is in W, voltage in V, energy in Wh, current in A and isolation
    resistance in kOhm. ``last_measurement`` is an aware UTC datetime, as for
    :class:`OptimizerData`.
    """

    serial: str
    power: float | None = None
    dc_voltage: float | None = None
    status: str | None = None
    energy_on_grid: float | None = None
    energy_off_grid: float | None = None
    power_limit_percent: float | None = None
    isolation_resistance: float | None = None
    residual_current: float | None = None
    last_measurement: datetime | None = None
    model: str | None = None
    manufacturer: str | None = None
    communication: str | None = None
    cpu_version: str | None = None
    dsp1_version: str | None = None
    dsp2_version: str | None = None


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
        self._site_information: dict[str, Any] = {}
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
        self._site_information = {}
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

    async def async_get_site_information(self) -> dict[str, Any]:
        """Get static facts about the site. Cached until the next login.

        Returns ``peakPower`` (kWp), ``installationDate``, ``lifeTimeDate``,
        ``latitude``/``longitude``, the site's current wall-clock ``siteTime``
        and ``siteTimeZone`` as an IANA name, e.g. ``America/Los_Angeles``.

        The timezone is worth having: every timestamp this client returns is
        naive and expressed in it, so it is the only way for a caller to make
        those values absolute.
        """
        await self.async_login()
        if self._site_information:
            _LOGGER.debug("Using cached site information for site: %s", self.site_id)
            return self._site_information

        _LOGGER.debug("Fetching site information for site: %s", self.site_id)
        url = f"{_SITE_INFORMATION_URL}/{self.site_id}"
        self._site_information = await self._async_get_json(url, "site information")
        return self._site_information

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

    async def async_get_optimizer_data(self) -> dict[str, OptimizerData]:
        """Get live readings for every optimizer, keyed by full serial.

        One request covers the whole site: the endpoint takes the serials to
        read as a JSON array body.

        These are the values the module-level view shows, refreshed by the
        inverter every few minutes. Optimizers that are asleep report a power
        of 0 or no live data at all; those still appear in the result with
        ``None`` readings so callers can tell them apart from missing devices.
        """
        equipment = await self.async_get_equipment()
        serials = [eq_id for eq_id, data in equipment.items() if data.get("type") == "OPTIMIZER"]
        if not serials:
            _LOGGER.warning("No optimizers found in the layout for site %s", self.site_id)
            return {}

        _LOGGER.debug("Fetching live data for %s optimizers on site: %s", len(serials), self.site_id)
        resp_json = await self._async_post_json(_OPTIMIZER_INFORMATION_URL, serials, "optimizer data")
        return _decode_optimizer_data(resp_json, serials)

    async def async_get_inverter_data(self) -> dict[str, InverterData]:
        """Get live readings and firmware details for every inverter, keyed by serial."""
        await self.async_get_equipment()
        serials = _collect_inverter_serials(self._site_structure)
        if not serials:
            _LOGGER.warning("No inverters found in the layout for site %s", self.site_id)
            return {}

        _LOGGER.debug("Fetching live data for %s inverters on site: %s", len(serials), self.site_id)
        params = [("inverter-serials", serial) for serial in serials]
        url = f"{_INVERTER_INFORMATION_URL}?{urlencode(params)}"
        resp_json = await self._async_get_json(url, "inverter data")
        return _decode_inverter_data(resp_json, serials)

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

        The last day of the window comes back short on sites west of UTC:
        end-date is matched against a real UTC instant while slots are labelled
        in the site's local time, so a naive end of 23:59:59 stops at
        23:59:59Z, which is 16:59 local at UTC-7. Measured on one such site,
        a day totals 40802 Wh when it is the last day of the window and
        43207 Wh when it is an interior day.

        Earlier days are unaffected, so callers polling a rolling window fill
        the gap in on a later refresh, once that day is no longer last. This
        is why passing no dates is fine for that pattern. An aware end_date
        widens the window correctly, but the newest hours can still be empty:
        per-optimizer playback data lags the site-level figures from
        :meth:`async_get_site_energy` by an hour or two.
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
        # end-date is compared against a real UTC instant even though the slots
        # come back labelled in the site's local time, so the last day of the
        # window loses everything after that instant. Do not "fix" this by
        # splitting the range into per-day requests: every day then becomes a
        # last day and loses its evening. See async_get_energy_data.
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
        if resolution not in _CONSUMPTION_RESOLUTIONS:
            msg = f"Unsupported resolution {resolution!r}; expected one of {', '.join(_CONSUMPTION_RESOLUTIONS)}"
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

        is_power = resolution in _POWER_SLOT_HOURS
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
            scale = _POWER_SLOT_HOURS[resolution]
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

        resp_json = await self._async_fetch_by_inverter(start, end, include_temperature=False)
        return _decode_energy_totals(resp_json, self._site_structure)

    async def async_get_optimizer_temperatures(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, float]:
        """Get each optimizer's highest temperature over a date range, in degrees Celsius.

        Keyed by full optimizer serial, matching :meth:`async_get_equipment`.
        The API reports whichever unit the site is configured for, so values
        are converted to Celsius here.

        This is a maximum over the range, not a current reading: with the
        default of today it is the peak so far today. Optimizers that did not
        report a temperature are absent from the result.
        """
        await self.async_get_equipment()

        end = (_as_naive(end_date) if end_date else datetime.now()).date()
        start = _as_naive(start_date).date() if start_date else end

        resp_json = await self._async_fetch_by_inverter(start, end, include_temperature=True)
        return _decode_optimizer_temperatures(resp_json)

    async def _async_fetch_by_inverter(self, start: date, end: date, include_temperature: bool) -> dict[str, Any]:
        """Fetch the per-inverter energy breakdown for a date range."""
        inverter_serials = _collect_inverter_serials(self._site_structure)
        if not inverter_serials:
            _LOGGER.warning("No inverters found in the layout for site %s", self.site_id)
            return {}

        params = [
            ("start-date", start.isoformat()),
            ("end-date", end.isoformat()),
            *[("inverter-serials", serial) for serial in inverter_serials],
            ("include-max-temperature", "true" if include_temperature else "false"),
            ("include-color", "true"),
        ]
        url = f"{_LAYOUT_ENERGY_URL}/{self.site_id}/by-inverter?{urlencode(params)}"

        _LOGGER.debug("Fetching by-inverter data for site: %s (%s..%s)", self.site_id, start, end)
        return await self._async_get_json(url, "by-inverter data")

    async def async_get_site_energy(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        resolution: str = "hours",
    ) -> list[SiteEnergyData]:
        """Get site-level energy per time slot. Values are in Wh.

        ``resolution`` is one of ``hours``, ``days``, ``months`` or ``years``.
        These are the figures the monitoring site charts, measured rather than
        derived from power like :meth:`async_get_energy_data`, but only for the
        site as a whole.

        The API is strict about how much each resolution may cover: ``hours``
        serves a single day, ``days`` at most 31 and ``months`` at most 12.
        Defaults follow those limits when start_date/end_date are omitted.
        """
        if resolution not in _SITE_ENERGY_SPANS:
            msg = f"Unsupported resolution {resolution!r}; expected one of {', '.join(_SITE_ENERGY_SPANS)}"
            raise ValueError(msg)

        await self.async_login()

        start, end = self._energy_graph_range(resolution, start_date, end_date)
        params = [
            ("chart-time-unit", resolution),
            ("start-date", start.isoformat()),
            ("end-date", end.isoformat()),
        ]
        url = f"{_ENERGY_GRAPH_URL}/{self.site_id}?{urlencode(params)}"

        _LOGGER.debug("Fetching %s site energy for site: %s (%s..%s)", resolution, self.site_id, start, end)
        resp_json = await self._async_get_json(url, "site energy")
        return _decode_energy_graph(resp_json)

    async def async_get_optimizer_energy(
        self,
        optimizer_serials: list[str],
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        resolution: str = "hours",
    ) -> list[SiteEnergyData]:
        """Get measured energy per time slot for a set of optimizers. Values are in Wh.

        These are exact figures, matching :meth:`async_get_energy_totals` to
        the decimal, where :meth:`async_get_energy_data` only approximates by
        summing hourly power.

        The API sums the requested optimizers into a single series rather than
        breaking them down, so a per-optimizer series costs one call per
        optimizer. That is a lot of requests for a large site; prefer
        :meth:`async_get_energy_data` unless the extra accuracy is needed.

        ``resolution`` and the range limits are as for
        :meth:`async_get_site_energy`.
        """
        if resolution not in _SITE_ENERGY_SPANS:
            msg = f"Unsupported resolution {resolution!r}; expected one of {', '.join(_SITE_ENERGY_SPANS)}"
            raise ValueError(msg)
        if not optimizer_serials:
            msg = "At least one optimizer serial is required"
            raise ValueError(msg)

        await self.async_login()
        start, end = self._energy_graph_range(resolution, start_date, end_date)
        params = [
            ("chart-time-unit", resolution),
            ("start-date", start.isoformat()),
            ("end-date", end.isoformat()),
            *[("optimizer-serials", serial) for serial in optimizer_serials],
        ]
        url = f"{_ENERGY_GRAPH_URL}/{self.site_id}/optimizers?{urlencode(params)}"

        _LOGGER.debug("Fetching %s energy for %s optimizers", resolution, len(optimizer_serials))
        resp_json = await self._async_get_json(url, "optimizer energy")
        return _decode_energy_graph(resp_json)

    async def async_get_site_energy_total(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> float | None:
        """Get the site's total measured energy over a date range, in Wh.

        The single number behind the site's energy figure, with no breakdown
        and no time series. Defaults to today.
        """
        await self.async_login()
        end = (_as_naive(end_date) if end_date else datetime.now()).date()
        start = _as_naive(start_date).date() if start_date else end

        params = [("start-date", start.isoformat()), ("end-date", end.isoformat())]
        url = f"{_LAYOUT_ENERGY_URL}/{self.site_id}?{urlencode(params)}"
        resp_json = await self._async_get_json(url, "site energy total")
        return _as_float(resp_json.get("energy"))

    async def async_get_site_power(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        resolution: str = "hours",
    ) -> list[SitePowerData]:
        """Get site-level power per time slot. Values are in W.

        ``resolution`` is ``hours`` (up to 8 days per request) or
        ``quarter-hours`` (a single day). This is the site total behind the
        playback view, so unlike :meth:`async_get_energy_data` it needs no
        per-optimizer aggregation.
        """
        if resolution not in _SITE_PLAYBACK_SPANS:
            msg = f"Unsupported resolution {resolution!r}; expected one of {', '.join(_SITE_PLAYBACK_SPANS)}"
            raise ValueError(msg)

        await self.async_login()
        max_span, default_span = _SITE_PLAYBACK_SPANS[resolution]
        end = (_as_naive(end_date) if end_date else datetime.now()).date()
        start = _as_naive(start_date).date() if start_date else end - default_span
        if end - start > max_span:
            _LOGGER.warning(
                "Requested range %s..%s is wider than the %s days the API allows for %s; expect HTTP 400",
                start,
                end,
                max_span.days,
                resolution,
            )

        params = [
            ("resolution", resolution),
            ("start-date", start.isoformat()),
            ("end-date", end.isoformat()),
        ]
        url = f"{_PLAYBACK_BASE_URL}/{self.site_id}?{urlencode(params)}"
        resp_json = await self._async_get_json(url, "site power")
        return _decode_site_power(resp_json)

    def _energy_graph_range(
        self,
        resolution: str,
        start_date: datetime | None,
        end_date: datetime | None,
    ) -> tuple[date, date]:
        """Resolve the date range for an energy-graph request, warning if too wide."""
        max_span, default_span = _SITE_ENERGY_SPANS[resolution]
        end = (_as_naive(end_date) if end_date else datetime.now()).date()
        start = _as_naive(start_date).date() if start_date else end - default_span
        if max_span is not None and end - start > max_span:
            _LOGGER.warning(
                "Requested range %s..%s is wider than the %s days the API allows for %s; expect HTTP 400",
                start,
                end,
                max_span.days,
                resolution,
            )
        return start, end

    async def async_get_inverter_energy_totals(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> dict[str, float]:
        """Get each inverter's total measured energy over a date range, in Wh.

        Keyed by inverter serial. :meth:`async_get_energy_totals` reports the
        same inverter figures alongside string and optimizer ones; this is the
        cheaper call when only the inverters are wanted, and it is the one the
        web app uses for its inverter comparison.
        """
        await self.async_login()
        end = (_as_naive(end_date) if end_date else datetime.now()).date()
        start = _as_naive(start_date).date() if start_date else end

        params = [
            ("start-date", start.isoformat()),
            ("end-date", end.isoformat()),
            ("normalized", "false"),
            ("page-number", "0"),
        ]
        url = f"{_INVERTER_ENERGY_URL}/{self.site_id}?{urlencode(params)}"
        resp_json = await self._async_get_json(url, "inverter energy totals")
        return _decode_inverter_energy_totals(resp_json)

    async def async_get_inverter_power(
        self,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
        resolution: str = "hours",
    ) -> list[InverterPowerData]:
        """Get per-inverter power per time slot. Values are in W.

        ``resolution`` is ``hours`` or ``quarter-hours``. Defaults to the last
        7 days.

        The response identifies inverters only by their position in an array,
        so the values are matched back to the layout by inverter order. That is
        the same order the API numbers them in, but it does mean the layout has
        to be fetched first.
        """
        if resolution not in _MAX_INVERTER_POWER_SPAN:
            msg = f"Unsupported resolution {resolution!r}; expected one of {', '.join(_MAX_INVERTER_POWER_SPAN)}"
            raise ValueError(msg)

        await self.async_get_equipment()
        serials = _collect_inverter_serials(self._site_structure)
        if not serials:
            _LOGGER.warning("No inverters found in the layout for site %s", self.site_id)
            return []

        end = (_as_naive(end_date) if end_date else datetime.now()).date()
        start = _as_naive(start_date).date() if start_date else end - timedelta(days=7)
        max_span = _MAX_INVERTER_POWER_SPAN[resolution]
        if end - start > max_span:
            _LOGGER.warning(
                "Requested range %s..%s is wider than the %s days the API allows for %s; expect HTTP 400",
                start,
                end,
                max_span.days,
                resolution,
            )

        params = [
            ("start-date", start.isoformat()),
            ("end-date", end.isoformat()),
            ("normalized", "false"),
            ("chart-time-unit", resolution),
            ("page-number", "0"),
        ]
        url = f"{_INVERTER_POWER_URL}/{self.site_id}?{urlencode(params)}"
        resp_json = await self._async_get_json(url, "inverter power")
        return _decode_inverter_power(resp_json, serials)

    async def async_get_live_power(self) -> LivePower:
        """Get the site's current power. Values are in W.

        Combines the two endpoints the web app polls: ``live-power`` for the
        current and rated AC power, and ``power-flow`` for the site status and
        the flow legs that exist on metered or battery sites.
        """
        await self.async_login()
        _LOGGER.debug("Fetching live power for site: %s", self.site_id)

        live = await self._async_get_json(f"{_LIVE_POWER_URL}/{self.site_id}", "live power")
        power_flow = await self._async_get_json(f"{_POWER_FLOW_URL}/{self.site_id}", "power flow")

        last_update_time = None
        raw_time = power_flow.get("lastUpdateTime")
        if raw_time:
            try:
                # The offset is the site's, so dropping it yields site-local time.
                last_update_time = _as_naive(datetime.fromisoformat(raw_time))
            except (TypeError, ValueError):
                _LOGGER.warning("Ignoring invalid lastUpdateTime: %r", raw_time)

        is_communicating = power_flow.get("isCommunicating")
        return LivePower(
            current_power=_as_float(live.get("currentAcPower")),
            max_power=_as_float(live.get("maxAcPower")),
            is_communicating=bool(is_communicating) if is_communicating is not None else None,
            last_update_time=last_update_time,
            power_flow=power_flow,
        )

    async def _async_post_json(self, url: str, payload: Any, description: str) -> dict[str, Any]:
        """POST a JSON body to a monitoring API endpoint and return the response."""
        try:
            resp = await self.session.post(url, json=payload, headers=self._request_headers(), timeout=self.timeout)
            _LOGGER.debug("Got %s from %s", resp.status, url)
            resp.raise_for_status()
            resp_json: dict[str, Any] = await resp.json()
        except aiohttp.ClientError:
            _LOGGER.exception("Error fetching %s from %s", description, url)
            raise
        return resp_json

    def _request_headers(self) -> dict[str, str]:
        """Build the auth headers for an API request.

        Sends the bearer token from the last login plus the CSRF token the
        backend hands out as a cookie; endpoints behind the API gateway reject
        the request without it.
        """
        headers = dict(self._auth_headers)
        csrf_token_cookie = self._find_cookie("CSRF-TOKEN")
        if csrf_token_cookie and csrf_token_cookie.value:
            headers["X-CSRF-TOKEN"] = csrf_token_cookie.value
        return headers

    async def _async_get_json(self, url: str, description: str) -> dict[str, Any]:
        """GET a monitoring API endpoint and return the decoded JSON body."""
        try:
            resp = await self.session.get(url, headers=self._request_headers(), timeout=self.timeout)
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
    """List the serials of every inverter in the layout, ordered.

    Sorted by the layout's ``order``, which is how the API numbers inverters,
    so a serial's position here matches its position in the arrays the
    per-inverter power endpoint returns. Falls back to document order for
    nodes without an order.
    """
    found: list[tuple[int, int, str]] = []

    def collect(node: dict[str, Any]) -> None:
        if node.get("type") == "INVERTER":
            serial = node.get("serial")
            if serial:
                raw_order = node.get("order")
                try:
                    order = int(raw_order) if raw_order is not None else len(found) + 1
                except (TypeError, ValueError):
                    order = len(found) + 1
                found.append((order, len(found), serial))
        for child in node.get("children", []):
            collect(child)

    if site_structure:
        collect(site_structure)
    return [serial for _, _, serial in sorted(found)]


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


def _parse_utc(raw_time: Any) -> datetime | None:
    """Parse a real UTC timestamp, e.g. ``2026-07-30T01:25:26Z``.

    ``fromisoformat`` only learned to accept the ``Z`` suffix in 3.11 and this
    package supports 3.10, so it is spelled out as an offset first.
    """
    if not raw_time:
        return None
    text = str(raw_time)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        _LOGGER.warning("Ignoring invalid timestamp: %r", raw_time)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _decode_optimizer_data(resp_json: dict[str, Any], serials: list[str]) -> dict[str, OptimizerData]:
    """Decode an optimizer information response, keyed by full serial."""
    basic_by_serial = {(entry.get("serial") or "").strip(): entry for entry in resp_json.get("basicInformationList") or []}
    live_by_serial = resp_json.get("serialToLiveData") or {}

    result: dict[str, OptimizerData] = {}
    for serial in serials:
        basic = basic_by_serial.get(serial) or {}
        live = live_by_serial.get(serial) or {}
        result[serial] = OptimizerData(
            serial=serial,
            power=_as_float(live.get("power_W")),
            voltage=_as_float(live.get("voltage_V")),
            optimizer_voltage=_as_float(live.get("optimizerVoltage_V")),
            current=_as_float(live.get("current_A")),
            last_measurement=_parse_utc(live.get("lastMeasurement")),
            model=basic.get("model"),
            modules=list(basic.get("modules") or []),
        )

    _LOGGER.debug("Decoded live data for %s optimizers.", len(result))
    return result


def _decode_inverter_data(resp_json: dict[str, Any], serials: list[str]) -> dict[str, InverterData]:
    """Decode an inverter information response, keyed by serial."""
    basic_by_serial = {(entry.get("serial") or "").strip(): entry for entry in resp_json.get("basicInformationList") or []}
    live_by_serial = resp_json.get("serialToLiveData") or {}

    result: dict[str, InverterData] = {}
    for serial in serials:
        basic = basic_by_serial.get(serial) or {}
        live = live_by_serial.get(serial) or {}
        result[serial] = InverterData(
            serial=serial,
            power=_as_float(live.get("pAc_W")),
            dc_voltage=_as_float(live.get("vDc_V")),
            status=live.get("inverterStatus"),
            energy_on_grid=_as_float(live.get("acEnergyOnGrid_Wh")),
            energy_off_grid=_as_float(live.get("acEnergyOffGrid_Wh")),
            power_limit_percent=_as_float(live.get("powerLimit_percent")),
            isolation_resistance=_as_float(live.get("lastIsolationValue_KOhm")),
            residual_current=_as_float(live.get("iRcd_A")),
            last_measurement=_parse_utc(live.get("lastMeasurement")),
            model=basic.get("fullModel"),
            manufacturer=basic.get("manufacturer"),
            communication=basic.get("communication"),
            cpu_version=basic.get("cpuVersion"),
            dsp1_version=basic.get("dsp1Version"),
            dsp2_version=basic.get("dsp2Version"),
        )

    _LOGGER.debug("Decoded live data for %s inverters.", len(result))
    return result


def _temperature_celsius(temperature: Any) -> float | None:
    """Read a ``{"temperature": .., "temperatureUnit": ..}`` object as Celsius."""
    if not isinstance(temperature, dict):
        return None
    value = _as_float(temperature.get("temperature"))
    if value is None:
        return None
    unit = str(temperature.get("temperatureUnit") or "CELSIUS").upper()
    if unit == "FAHRENHEIT":
        return (value - 32.0) * 5.0 / 9.0
    if unit != "CELSIUS":
        _LOGGER.warning("Unknown temperature unit %r; assuming Celsius", unit)
    return value


def _decode_optimizer_temperatures(resp_json: dict[str, Any]) -> dict[str, float]:
    """Decode a by-inverter response into max temperatures keyed by optimizer serial."""
    temperatures: dict[str, float] = {}
    for inverter in resp_json.get("inverters", []):
        for optimizer in inverter.get("optimizers", []):
            serial = optimizer.get("serial")
            celsius = _temperature_celsius(optimizer.get("temperature"))
            if serial and celsius is not None:
                temperatures[serial] = celsius

    if not temperatures:
        # Expected on sites whose optimizers do not report temperature at all.
        _LOGGER.debug("No optimizer temperatures in the by-inverter response.")
    return temperatures


def _decode_inverter_energy_totals(resp_json: dict[str, Any]) -> dict[str, float]:
    """Decode an inverter energy response into Wh keyed by inverter serial."""
    entries = resp_json.get("inverterEnergyList", [])
    if not entries:
        _LOGGER.warning("No inverters returned in the inverter energy response.")
        return {}

    unit = str(resp_json.get("energyUnit") or "watt-hour").lower()
    factor = _ENERGY_UNIT_TO_WH.get(unit)
    if factor is None:
        _LOGGER.warning("Unknown energy unit %r; assuming watt-hour", unit)
        factor = 1.0

    totals: dict[str, float] = {}
    for entry in entries:
        serial = entry.get("inverterSerial")
        energy = _as_float(entry.get("inverterEnergy"), factor)
        if serial and energy is not None:
            totals[serial] = energy
    return totals


def _decode_inverter_power(resp_json: dict[str, Any], serials: list[str]) -> list[InverterPowerData]:
    """Decode a per-inverter power response, matching array positions to serials."""
    entries = resp_json.get("invertersDatedPowerList", [])
    if not entries:
        _LOGGER.warning("No measurements returned in the inverter power response.")
        return []

    result: list[InverterPowerData] = []
    for entry in entries:
        raw_time = entry.get("measurementTime")
        if not raw_time:
            continue
        try:
            # The offset is the site's, so dropping it yields site-local time.
            slot_time = _as_naive(datetime.fromisoformat(raw_time))
        except (TypeError, ValueError):
            _LOGGER.warning("Skipping inverter power entry with invalid time: %r", raw_time)
            continue

        values: dict[str, float] = {}
        powers = entry.get("inverterPowerArray") or []
        if len(powers) > len(serials):
            _LOGGER.warning(
                "Inverter power response has %s values but the layout has %s inverters; ignoring the extras",
                len(powers),
                len(serials),
            )
        # Lengths can disagree; the mismatch is reported above.
        for serial, raw_power in zip(serials, powers, strict=False):
            power = _as_float(raw_power)
            if power is not None:
                values[serial] = power
        result.append(InverterPowerData(start_time=slot_time, values=values))

    _LOGGER.debug("Decoded %s inverter power slots.", len(result))
    return result


def _decode_site_power(resp_json: dict[str, Any]) -> list[SitePowerData]:
    """Decode a site playback response into power slots."""
    measurements = resp_json.get("sitePowerMeasurements", [])
    if not measurements:
        _LOGGER.warning("No measurements returned in the site power response.")
        return []

    result: list[SitePowerData] = []
    for measurement in measurements:
        raw_time = measurement.get("measurementTime")
        if not raw_time:
            continue
        try:
            # The offset is the site's, so dropping it yields site-local time.
            slot_time = _as_naive(datetime.fromisoformat(raw_time))
        except (TypeError, ValueError):
            _LOGGER.warning("Skipping site power measurement with invalid time: %r", raw_time)
            continue
        result.append(SitePowerData(start_time=slot_time, power=_as_float(measurement.get("powerW"))))

    _LOGGER.debug("Decoded %s site power slots.", len(result))
    return result


def _decode_energy_graph(resp_json: dict[str, Any]) -> list[SiteEnergyData]:
    """Decode an energy-graph response into site energy slots."""
    energy_bars = resp_json.get("energyBars", [])
    if not energy_bars:
        _LOGGER.warning("No energy bars returned in the site energy response.")
        return []

    result: list[SiteEnergyData] = []
    for bar in energy_bars:
        raw_time = bar.get("measurementTime")
        if not raw_time:
            continue
        try:
            # The offset is the site's, so dropping it yields site-local time.
            slot_time = _as_naive(datetime.fromisoformat(raw_time))
        except (TypeError, ValueError):
            _LOGGER.warning("Skipping energy bar with invalid time: %r", raw_time)
            continue
        result.append(SiteEnergyData(start_time=slot_time, energy=_as_float(bar.get("energy"))))

    _LOGGER.debug("Decoded %s site energy slots.", len(result))
    return result


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
    "InverterData",
    "InverterPowerData",
    "LivePower",
    "OptimizerData",
    "SiteEnergyData",
    "SitePowerData",
    "SolarEdgeWeb",
]
