# solaredge-web

A python client library for SolarEdge Web.
Fetches energy data for each inverter/string/module via the web API and not via the official API which doesn't expose this data.

## Usage

```python
import aiohttp
from solaredge_web import SolarEdgeWeb

async with aiohttp.ClientSession() as session:
    client = SolarEdgeWeb(username, password, site_id, session)

    # --- site facts (all cached until the next login) ---
    equipment = await client.async_get_equipment()  # inverters, strings, optimizers
    components = await client.async_get_site_components()  # hasConsumptionAndGrid, hasStorage, ...
    information = await client.async_get_site_information()  # peakPower, siteTimeZone, ...
    availability = await client.async_get_data_availability()
    details = await client.async_get_site_details()
    summary = await client.async_get_site_equipment_summary()
    communication = await client.async_get_communication_status()

    # --- energy ---
    # Hourly per optimizer/string/inverter/site, derived from playback power.
    energy = await client.async_get_energy_data()
    # Measured totals per device over a date range, no time series.
    totals = await client.async_get_energy_totals()
    # Measured site energy per slot: hours, days, months or years.
    site_energy = await client.async_get_site_energy(resolution="days")
    site_total = await client.async_get_site_energy_total()
    # Measured energy per slot for specific optimizers (one request per optimizer).
    optimizer_energy = await client.async_get_optimizer_energy(["7A012345-CA"])
    inverter_energy = await client.async_get_inverter_energy_totals()
    comparative = await client.async_get_comparative_energy("monthly")

    # --- production and consumption ---
    # quarter-hours, hours, days, months or years.
    consumption = await client.async_get_consumption_data(resolution="hours")
    storage = await client.async_get_storage_energy_distribution()

    # --- power ---
    live = await client.async_get_live_power()
    site_power = await client.async_get_site_power()
    inverter_power = await client.async_get_inverter_power()

    # --- per-device diagnostics ---
    optimizers = await client.async_get_optimizer_data()  # live W, V, A per optimizer
    inverters = await client.async_get_inverter_data()  # live readings and firmware
    temperatures = await client.async_get_optimizer_temperatures()  # max degrees C

    # --- everything else ---
    weather = await client.async_get_weather()
    benefits = await client.async_get_environmental_benefits()
    alerts = await client.async_get_alerts()
```

All energy values are in Wh, power in W, temperatures in degrees Celsius.

Timestamps are naive `datetime`s in the site's local time, whose IANA name is
`siteTimeZone` from `async_get_site_information`. The one exception is
`last_measurement` on `OptimizerData` and `InverterData`, which is an aware UTC
datetime because those endpoints report a real UTC instant.

Consumption, grid import and grid export require a consumption meter: without
one (`hasConsumptionAndGrid` is false) those fields are `None` rather than zero.

Each resolution has its own range limit, and the API answers HTTP 400 beyond
it. The library warns before making such a request; see the method docstrings
for the limits.

Endpoints that report a payload with no fixed shape - site details, weather,
alerts and similar - are returned as-is rather than modelled, since their
fields vary by account type, storage and metering.

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
