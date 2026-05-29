"""Weather forecast for the morning briefing.

Uses Open-Meteo — free, no API key required, no rate limits for our usage.
Returns a single dict for today's outlook in Arlington, VA.
"""

from __future__ import annotations

from typing import Optional

import requests

# Arlington, VA — home base
LAT = 38.8895
LON = -77.0907

# WMO weather codes → (emoji, short label)
_CODE_MAP: dict[int, tuple[str, str]] = {
    0: ("☀️", "Clear"),
    1: ("🌤", "Mostly clear"),
    2: ("⛅", "Partly cloudy"),
    3: ("☁️", "Overcast"),
    45: ("🌫", "Fog"),
    48: ("🌫", "Fog"),
    51: ("🌦", "Light drizzle"),
    53: ("🌦", "Drizzle"),
    55: ("🌧", "Heavy drizzle"),
    61: ("🌧", "Light rain"),
    63: ("🌧", "Rain"),
    65: ("🌧", "Heavy rain"),
    71: ("🌨", "Light snow"),
    73: ("🌨", "Snow"),
    75: ("🌨", "Heavy snow"),
    80: ("🌦", "Showers"),
    81: ("🌧", "Showers"),
    82: ("🌧", "Heavy showers"),
    95: ("⛈", "Thunderstorm"),
    96: ("⛈", "Thunderstorm w/ hail"),
    99: ("⛈", "Severe storm"),
}


def fetch_today_forecast() -> Optional[dict]:
    """Return today's forecast or None on failure (so email still sends)."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={LAT}&longitude={LON}"
        "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
        "&temperature_unit=fahrenheit"
        "&timezone=America/New_York"
        "&forecast_days=1"
    )
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        daily = resp.json().get("daily", {})

        code = daily.get("weather_code", [0])[0]
        emoji, label = _CODE_MAP.get(code, ("🌡", "—"))

        return {
            "high_f": round(daily["temperature_2m_max"][0]),
            "low_f": round(daily["temperature_2m_min"][0]),
            "precip_pct": daily["precipitation_probability_max"][0] or 0,
            "emoji": emoji,
            "label": label,
        }
    except Exception as e:
        print(f"  [weather] fetch failed: {e}")
        return None


def format_strip(forecast: dict) -> str:
    """One-line text version for plain-text fallback or logging."""
    return (
        f"{forecast['emoji']} {forecast['label']} · "
        f"{forecast['high_f']}°F / {forecast['low_f']}°F · "
        f"{forecast['precip_pct']}% rain"
    )


if __name__ == "__main__":
    fc = fetch_today_forecast()
    if fc:
        print(format_strip(fc))
    else:
        print("Could not fetch forecast.")
