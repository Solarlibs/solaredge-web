"""CLI interface to test and exercise the SolarEdge Web API.

Run with::

    python -m solaredge_web -u <email> -p <password> -s <site_id> [-v]

Credentials may also come from a ``.env`` file in the working directory or from
the SOLAREDGE_USERNAME / SOLAREDGE_PASSWORD / SOLAREDGE_SITE_ID environment
variables, so they need not be passed on the command line.

It will:
1. Authenticate via the OAuth2 PKCE flow.
2. Fetch the equipment layout (inverters, strings, optimizers).
3. Fetch hourly playback energy data for the last 7 days.
4. Fetch consumption, measured energy totals, site energy and live power.
5. Print a summary that you can compare against the SolarEdge web UI.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import aiohttp

try:
    from .solaredge import ConsumptionData, EnergyData, LivePower, SiteEnergyData, SolarEdgeWeb, _device_id
except ImportError:
    from solaredge import (  # type: ignore[no-redef,import-not-found]
        ConsumptionData,
        EnergyData,
        LivePower,
        SiteEnergyData,
        SolarEdgeWeb,
        _device_id,
    )


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Exercise the SolarEdge Web API client.")
    parser.add_argument("-u", "--username", help="SolarEdge username/email")
    parser.add_argument("-p", "--password", help="SolarEdge password")
    parser.add_argument("-s", "--site-id", help="SolarEdge site ID")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args()


def _load_dotenv(path: Path = Path(".env")) -> dict[str, str]:
    """Read simple KEY=VALUE lines from a .env file, if one exists."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _get_credentials(args: argparse.Namespace) -> tuple[str, str, str]:
    """Resolve credentials from CLI args, then .env, then environment, then prompt."""
    dotenv = _load_dotenv()

    def resolve(arg: str | None, key: str, prompt: str, secret: bool = False) -> str:
        value = arg or dotenv.get(key) or os.environ.get(key)
        if value:
            return value.strip()
        return (getpass.getpass(prompt) if secret else input(prompt)).strip()

    username = resolve(args.username, "SOLAREDGE_USERNAME", "SolarEdge Username: ")
    password = resolve(args.password, "SOLAREDGE_PASSWORD", "SolarEdge Password: ", secret=True)
    site_id = resolve(args.site_id, "SOLAREDGE_SITE_ID", "SolarEdge Site ID: ")
    return username, password, site_id


def _format_time(dt: datetime) -> str:
    """Format a slot time as the site's local wall-clock time."""
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M")


def _print_data(energy_data: list[EnergyData], first_optimizer: str, site_name: str, site_key: str) -> None:
    """Print hourly data for the first optimizer and the whole site."""
    print(f"\n=== First Optimizer: {first_optimizer} ===")
    print(f"{'Time (site local)':<20} {'Energy (Wh)':>12}")
    print("-" * 34)
    for ed in energy_data:
        value = ed.values.get(first_optimizer, 0.0)
        print(f"{_format_time(ed.start_time):<20} {value:>12.1f}")

    print(f"\n=== Site Total: {site_name} ===")
    print(f"{'Time (site local)':<20} {'Energy (Wh)':>12}")
    print("-" * 34)
    for ed in energy_data:
        value = ed.values.get(site_key, 0.0)
        print(f"{_format_time(ed.start_time):<20} {value:>12.1f}")


def _format_value(value: float | None) -> str:
    """Format a measurement, showing unmeasured values as a dash."""
    return "-" if value is None else f"{value:.1f}"


def _print_consumption(data: list[ConsumptionData], has_meter: bool) -> None:
    """Print the most recent hourly production/consumption slots."""
    print("\n=== Consumption (last 7 days, hourly) ===")
    print(f"Retrieved {len(data)} hourly entries.")
    if not has_meter:
        print("This site has no consumption meter, so only production is measured.")
    print(f"{'Time (site local)':<20} {'Production':>12} {'Consumption':>12} {'Import':>10} {'Export':>10}")
    print("-" * 68)
    for cd in data[-24:]:
        print(
            f"{_format_time(cd.start_time):<20} {_format_value(cd.production):>12} "
            f"{_format_value(cd.consumption):>12} {_format_value(cd.imported):>10} "
            f"{_format_value(cd.exported):>10}"
        )


def _print_totals(
    totals: dict[str, float],
    equipment: dict[str, dict[str, Any]],
    site_key: str,
    playback_total: float,
) -> None:
    """Print measured energy totals next to the playback-derived total."""
    print("\n=== Measured Energy Totals (today, Wh) ===")
    for eq_id, value in sorted(totals.items(), key=lambda item: -item[1]):
        eq_type = "SITE" if eq_id == site_key else equipment.get(eq_id, {}).get("type", "?")
        print(f"  [{eq_type:<9}] {eq_id:<40} {value:>12.1f}")
    measured = totals.get(site_key)
    if measured is not None:
        print(f"\nSite total: measured {measured:.1f} Wh vs playback-derived {playback_total:.1f} Wh")


def _print_site_energy(data: list[SiteEnergyData]) -> None:
    """Print today's hourly site energy."""
    print("\n=== Site Energy (today, hourly) ===")
    print(f"{'Time (site local)':<20} {'Energy (Wh)':>12}")
    print("-" * 34)
    for entry in data:
        print(f"{_format_time(entry.start_time):<20} {_format_value(entry.energy):>12}")


def _print_live_power(live: LivePower) -> None:
    """Print the current site power."""
    print("\n=== Live Power ===")
    print(f"Current: {_format_value(live.current_power)} W of {_format_value(live.max_power)} W")
    print(f"Communicating: {live.is_communicating} | Last update: {live.last_update_time}")


async def async_main() -> None:
    """Run API exercises."""
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    username, password, site_id = _get_credentials(args)

    if not username or not password or not site_id:
        print("Error: Username, password, and site ID are required.", file=sys.stderr)
        sys.exit(1)

    async with aiohttp.ClientSession() as session:
        client = SolarEdgeWeb(
            username=username,
            password=password,
            site_id=site_id,
            session=session,
        )

        try:
            print("\n=== Fetching Equipment ===")
            equipment = await client.async_get_equipment()
            print(f"Found {len(equipment)} equipment item(s)")
            for eq_id, eq_data in equipment.items():
                name = eq_data.get("name", "N/A")
                eq_type = eq_data.get("type", "N/A")
                print(f"  - [{eq_type}] {name} | ID: {eq_id}")

            print("\n=== Fetching Energy Data (last 7 days) ===")
            energy_data: list[EnergyData] = await client.async_get_energy_data()
            print(f"Retrieved {len(energy_data)} hourly entries.")
            if energy_data:
                first = energy_data[0]
                last = energy_data[-1]
                print(f"  Start: {_format_time(first.start_time)} | End: {_format_time(last.start_time)}")

                # Find the first optimizer (full serial) and the site device ID.
                first_optimizer = next(eq_id for eq_id, data in equipment.items() if data.get("type") == "OPTIMIZER")
                # Site aggregation is keyed by the site's device_id (identifier/uuid).
                site_node = client._site_structure
                site_key = _device_id(site_node) or site_id
                site_name = site_node.get("name", site_id)

                _print_data(energy_data, first_optimizer, site_name, site_key)

            print("\n=== Site Capabilities ===")
            components = await client.async_get_site_components()
            has_meter = bool(components.get("hasConsumptionAndGrid"))
            print(f"  Type: {components.get('siteType')} | Inverters: {components.get('inverterCount')}")
            print(f"  Consumption meter: {has_meter} | Storage: {components.get('hasStorage')}")
            print(f"  Data available: {await client.async_get_data_availability()}")

            _print_consumption(await client.async_get_consumption_data(), has_meter)

            site_node = client._site_structure
            site_key = _device_id(site_node) or site_id
            playback_total = sum(
                ed.values.get(site_key, 0.0) for ed in energy_data if ed.start_time.date() == datetime.now().date()
            )
            _print_totals(await client.async_get_energy_totals(), equipment, site_key, playback_total)

            _print_site_energy(await client.async_get_site_energy())

            _print_live_power(await client.async_get_live_power())

        except aiohttp.ClientError as err:
            print(f"\nAPI Error: {err}", file=sys.stderr)
            sys.exit(1)


def main() -> None:
    """CLI entry point."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nAborted by user.")


if __name__ == "__main__":
    main()
