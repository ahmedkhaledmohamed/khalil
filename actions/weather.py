"""Weather integration — current conditions, forecast, and summary via Open-Meteo.

Uses Open-Meteo free API (no API key required).
Coordinates default to Toronto, configurable via config.WEATHER_LAT / WEATHER_LON.
"""

import logging

import httpx

from config import TIMEZONE, WEATHER_LAT, WEATHER_LON

log = logging.getLogger("khalil.actions.weather")

_BASE_URL = "https://api.open-meteo.com/v1/forecast"


def _weather_code_to_text(code: int) -> str:
    """Map WMO weather code to human-readable text."""
    if code == 0:
        return "Clear"
    if code <= 3:
        return "Partly cloudy"
    if code in (45, 48):
        return "Foggy"
    if code in (51, 53, 55):
        return "Drizzle"
    if code in (56, 57):
        return "Freezing drizzle"
    if code in (61, 63, 65):
        return "Rain"
    if code in (66, 67):
        return "Freezing rain"
    if code in (71, 73, 75):
        return "Snow"
    if code == 77:
        return "Snow grains"
    if code in (80, 81, 82):
        return "Rain showers"
    if code in (85, 86):
        return "Snow showers"
    if code in (95, 96, 99):
        return "Thunderstorm"
    return f"Unknown ({code})"


async def get_current_weather() -> dict:
    """Fetch current weather conditions for configured location.

    Returns dict with keys: temp, feels_like, humidity, wind, condition.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            _BASE_URL,
            params={
                "latitude": WEATHER_LAT,
                "longitude": WEATHER_LON,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m,weather_code",
                "timezone": TIMEZONE,
            },
        )
        resp.raise_for_status()
        data = resp.json()["current"]
        return {
            "temp": data["temperature_2m"],
            "feels_like": data["apparent_temperature"],
            "humidity": data["relative_humidity_2m"],
            "wind": data["wind_speed_10m"],
            "condition": _weather_code_to_text(data["weather_code"]),
        }


async def get_forecast(days: int = 3) -> list[dict]:
    """Fetch daily forecast for configured location.

    Returns list of dicts with keys: date, high, low, condition, precipitation.
    """
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            _BASE_URL,
            params={
                "latitude": WEATHER_LAT,
                "longitude": WEATHER_LON,
                "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_sum",
                "timezone": TIMEZONE,
                "forecast_days": days,
            },
        )
        resp.raise_for_status()
        daily = resp.json()["daily"]
        return [
            {
                "date": daily["time"][i],
                "high": daily["temperature_2m_max"][i],
                "low": daily["temperature_2m_min"][i],
                "condition": _weather_code_to_text(daily["weather_code"][i]),
                "precipitation": daily["precipitation_sum"][i],
            }
            for i in range(len(daily["time"]))
        ]


async def get_weather_summary() -> str:
    """One-liner for morning brief — e.g. 'Toronto: 5°C (feels 2°C), partly cloudy. High 8°C today.'"""
    try:
        current, forecast = await _fetch_summary_data()
        today_high = forecast[0]["high"] if forecast else "?"
        return (
            f"Toronto: {current['temp']}°C (feels {current['feels_like']}°C), "
            f"{current['condition'].lower()}. High {today_high}°C today."
        )
    except Exception as e:
        log.warning("Weather summary failed: %s", e)
        return ""


async def _fetch_summary_data() -> tuple[dict, list[dict]]:
    """Fetch current + today's forecast in a single API call."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            _BASE_URL,
            params={
                "latitude": WEATHER_LAT,
                "longitude": WEATHER_LON,
                "current": "temperature_2m,apparent_temperature,weather_code",
                "daily": "temperature_2m_max",
                "timezone": TIMEZONE,
                "forecast_days": 1,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        current = {
            "temp": data["current"]["temperature_2m"],
            "feels_like": data["current"]["apparent_temperature"],
            "condition": _weather_code_to_text(data["current"]["weather_code"]),
        }
        forecast = [{"high": data["daily"]["temperature_2m_max"][0]}]
        return current, forecast
