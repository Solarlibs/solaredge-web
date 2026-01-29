"""API for SolarEdge Web."""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from datetime import datetime
from enum import IntEnum
from typing import TYPE_CHECKING, Any, cast

import aiohttp

if TYPE_CHECKING:
    from http.cookies import Morsel

_LOGGER = logging.getLogger(__name__)


class TimeUnit(IntEnum):
    """How long ago to return energy data."""

    # Only DAY and WEEK actually return data.
    # ALL = 0
    # MINUTE = 1
    # QUARTER_OF_AN_HOUR = 2
    # HOUR = 3
    DAY = 4
    WEEK = 5
    # MONTH = 6
    # YEAR = 7
    # QUARTER_OF_A_YEAR = 8


@dataclasses.dataclass
class EnergyData:
    """Data as reported by the API for a single timestamp."""

    start_time: datetime
    values: dict[int, float]  # Dict from equipment ID to production energy in Wh


class SolarEdgeWeb:
    """SolarEdge Web client."""

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
        self._equipment: dict[int, dict[str, Any]] = {}
        self._last_login_time = 0.0
        self._smart_home_visited = False

    async def async_login(self) -> None:
        """Login to the SolarEdge Web."""
        sso_cookie = self._find_cookie("SolarEdge_SSO-1.4")
        if sso_cookie is not None and time.time() - self._last_login_time < int(sso_cookie["max-age"]) - 10 * 60:
            _LOGGER.debug("Skipping login. Using existing valid SSO cookie")
            return
        self._equipment = {}
        self._smart_home_visited = False
        url = "https://monitoring.solaredge.com/solaredge-apigw/api/login"
        try:
            resp = await self.session.post(
                url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"j_username": self.username, "j_password": self.password},
                timeout=self.timeout,
            )
            _LOGGER.debug("Got %s from %s", resp.status, url)
            resp.raise_for_status()
        except aiohttp.ClientError:
            _LOGGER.exception("Error during SolarEdge login")
            raise
        self._last_login_time = time.time()

    async def async_get_equipment(self) -> dict[int, dict[str, Any]]:
        """Get the equipment of the SolarEdge installation.

        Returns a dict of {equipment_id: equipment_data}
        """
        _LOGGER.debug("Fetching equipment for site: %s", self.site_id)
        await self.async_login()
        if self._equipment:
            _LOGGER.debug(
                "Using cached %s equipment for site: %s",
                len(self._equipment),
                self.site_id,
            )
            return self._equipment
        url = f"https://monitoring.solaredge.com/solaredge-apigw/api/sites/{self.site_id}/layout/logical"
        try:
            resp = await self.session.get(url, timeout=self.timeout)
            _LOGGER.debug("Got %s from %s", resp.status, url)
            resp.raise_for_status()
        except aiohttp.ClientError:
            _LOGGER.exception("Error fetching equipment from %s", url)
            raise
        resp_json = await resp.json()

        def extract_nested_data(node: dict[Any, Any], data_dict: dict[int, dict[str, Any]]) -> None:
            item_data = node["data"]
            data_dict[item_data["id"]] = item_data
            for child_node in node["children"]:
                extract_nested_data(child_node, data_dict)

        self._equipment = {}
        for top_level_child in resp_json["logicalTree"]["children"]:
            extract_nested_data(top_level_child, self._equipment)
        _LOGGER.debug("Found %s equipment for site: %s", len(self._equipment), self.site_id)
        return self._equipment

    async def _async_ensure_smart_home_session(self) -> None:
        """Ensure that the smart home session is established.

        This is required before accessing home automation APIs.
        Login is performed and the Smart Home page is visited to set up session state if needed.
        """
        await self.async_login()

        # Visit the Smart Home page first to establish session state
        # This is required for the API to return device data
        if not self._smart_home_visited:
            smart_home_url = f"https://monitoring.solaredge.com/solaredge-web/p/site/{self.site_id}/#/smart-home"
            _LOGGER.debug("Visiting Smart Home page to establish session: %s", smart_home_url)
            try:
                resp = await self.session.get(smart_home_url, timeout=self.timeout)
                _LOGGER.debug("Smart Home page returned %s", resp.status)
                resp.raise_for_status()
                self._smart_home_visited = True
            except aiohttp.ClientError:
                _LOGGER.exception("Error visiting Smart Home page %s", smart_home_url)
                raise

    async def async_get_home_automation_devices(self) -> dict[str, Any]:
        """Get home automation devices from the SolarEdge Web API.

        Returns device information from the home automation API endpoint.
        """
        _LOGGER.debug("Fetching home automation devices for site: %s", self.site_id)

        await self._async_ensure_smart_home_session()

        url = f"https://monitoring.solaredge.com/services/api/homeautomation/v1.0/sites/{self.site_id}/devices"

        try:
            resp = await self.session.get(url, timeout=self.timeout)
            _LOGGER.debug("Got %s from %s", resp.status, url)
            resp.raise_for_status()
        except aiohttp.ClientError:
            _LOGGER.exception("Error fetching home automation devices from %s", url)
            raise
        resp_json = await resp.json()
        _LOGGER.debug("Found home automation devices for site: %s", self.site_id)
        return cast("dict[str, Any]", resp_json)

    async def async_get_energy_data(self, time_unit: TimeUnit = TimeUnit.WEEK) -> list[EnergyData]:
        """Get energy data from the SolarEdge Web API.

        Energy data is aggregated by 15 minutes, with energy values in Wh.
        """
        _LOGGER.debug("Fetching energy data for site: %s", self.site_id)
        await self.async_login()
        csrf_token_cookie = self._find_cookie("CSRF-TOKEN")
        if csrf_token_cookie is None or not csrf_token_cookie.value:
            _LOGGER.error("CSRF-TOKEN not found in cookies")
            raise aiohttp.ClientError("CSRF-TOKEN not found in cookies")
        url = "https://monitoring.solaredge.com/solaredge-web/p/playbackData"
        try:
            resp = await self.session.post(
                url,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-CSRF-TOKEN": csrf_token_cookie.value,
                },
                data={"fieldId": self.site_id, "timeUnit": time_unit.value},
                timeout=self.timeout,
            )
            _LOGGER.debug("Got %s from %s", resp.status, url)
            resp.raise_for_status()
        except aiohttp.ClientError:
            _LOGGER.exception("Error fetching energy data from %s", url)
            raise
        resp_text = await resp.text()
        # The API returns a JavaScript object string, convert it to strict JSON.
        # An alternative would be to use JSON5 but it's too slow
        resp_text = (
            resp_text.replace("'", '"')
            .replace("timeUnit:", '"timeUnit":')
            .replace("fieldData:", '"fieldData":')
            .replace("fieldDataArray:", '"fieldDataArray":')
            .replace("reportersData:", '"reportersData":')
            .replace("key:", '"key":')
            .replace("value:", '"value":')
        )
        resp_json = json.loads(resp_text)
        energy_data = [
            EnergyData(
                start_time=datetime.strptime(date_str, "%a %b %d %H:%M:%S GMT %Y"),
                values={int(entry["key"]): float(entry["value"]) for entries_list in d.values() for entry in entries_list},
            )
            for date_str, d in resp_json["reportersData"].items()
        ]
        _LOGGER.debug("Found %s energy data for site: %s", len(energy_data), self.site_id)
        return energy_data

    async def async_set_ev_charging_state(self, device_id: str, level: int) -> None:
        """Set the EV charging state to the specified level (0-100).

        device_id: The reporterId of the EV charger device obtained from async_get_home_automation_devices
        level: The charging level to set (0-100)

        Anything below 100 will disable charging if excess PV is not supported.
        """
        _LOGGER.debug("Setting EV charging state for device: %s to level: %s", device_id, level)

        await self._async_ensure_smart_home_session()

        cookie = self._find_cookie("SPRING_SECURITY_REMEMBER_ME_COOKIE")
        if cookie is None or not cookie.value:
            _LOGGER.error("SPRING_SECURITY_REMEMBER_ME_COOKIE cookie not found")
            raise aiohttp.ClientError("SPRING_SECURITY_REMEMBER_ME_COOKIE cookie not found")

        url = f"https://monitoring.solaredge.com/services/m/api/homeautomation/v1.0/{self.site_id}/devices/{device_id}/activationState"
        try:
            resp = await self.session.put(url, json={"mode": "MANUAL", "level": level, "duration": None}, timeout=self.timeout)
            if resp.status != 200:
                _LOGGER.error("Failed to set charging state: %s", resp.status)
                resp.raise_for_status()
        except aiohttp.ClientError:
            _LOGGER.exception("Error setting EV charging state at %s", url)
            raise

        resp_json = await resp.json()
        if resp_json.get("status") == "PASSED":
            _LOGGER.debug("Successfully set charging state to level %s", level)
            return

        _LOGGER.error("API failed with status: %s", resp_json.get("status"))
        raise aiohttp.ClientError(f"API failed with status: {resp_json.get('status')}")

    def _find_cookie(self, name: str) -> Morsel[str] | None:
        """Find a cookie by name."""
        for cookie in self.session.cookie_jar:
            if cookie["domain"] == "monitoring.solaredge.com" and cookie.key == name:
                return cookie
        return None
