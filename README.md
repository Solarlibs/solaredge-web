# solaredge-web

A python client library for SolarEdge Web.
Fetches energy data for each inverter/string/module via the web API and not via the official API which doesn't expose this data.

## Usage

```python
import aiohttp
from solaredge_web import SolarEdgeWeb

async with aiohttp.ClientSession() as session:
    client = SolarEdgeWeb(username, password, site_id, session)

    # Which features the site has, e.g. hasConsumptionAndGrid, hasStorage.
    components = await client.async_get_site_components()
    availability = await client.async_get_data_availability()

    # Inverters, strings and optimizers, keyed by serial.
    equipment = await client.async_get_equipment()

    # Hourly energy per optimizer/string/inverter/site, derived from playback power.
    energy = await client.async_get_energy_data()

    # Measured energy totals per device over a date range, no time series.
    totals = await client.async_get_energy_totals()

    # Site-level production and consumption. resolution is one of
    # quarter-hours, hours, days, months, years.
    consumption = await client.async_get_consumption_data(resolution="hours")

    # Measured site-level energy per slot: hours, days, months or years.
    site_energy = await client.async_get_site_energy(resolution="days")

    # Current power.
    live = await client.async_get_live_power()
```

All energy values are in Wh and all power values are in W. Timestamps are naive
`datetime`s in the site's local time.

Consumption, grid import and grid export require a consumption meter: without one
(`hasConsumptionAndGrid` is false) those fields are `None` rather than zero.

Each resolution has its own range limit, and the API answers HTTP 400 beyond it.
The library warns before making such a request; see the method docstrings for
the limits.

## Development environment

```sh
python3 -m venv .venv
source .venv/bin/activate
# for Windows CMD:
# .venv\Scripts\activate.bat
# for Windows PowerShell:
# .venv\Scripts\Activate.ps1

# Install dependencies
python -m pip install --upgrade pip
python -m pip install -e .

# Run pre-commit
python -m pip install pre-commit
pre-commit install
pre-commit run --all-files

# Run tests
python -m pip install -e ".[test]"
pytest

# Build package
python -m pip install build
python -m build
```
