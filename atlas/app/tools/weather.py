"""Weather via Open-Meteo (no key needed) with IP-based location fallback."""
from __future__ import annotations

import time

from . import ToolError, tool

WMO = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast", 45: "Fog", 48: "Freezing fog",
    51: "Light drizzle", 53: "Drizzle", 55: "Heavy drizzle", 56: "Freezing drizzle", 57: "Freezing drizzle",
    61: "Light rain", 63: "Rain", 65: "Heavy rain", 66: "Freezing rain", 67: "Freezing rain",
    71: "Light snow", 73: "Snow", 75: "Heavy snow", 77: "Snow grains", 80: "Rain showers",
    81: "Rain showers", 82: "Violent rain showers", 85: "Snow showers", 86: "Heavy snow showers",
    95: "Thunderstorm", 96: "Thunderstorm with hail", 99: "Severe thunderstorm with hail",
}
_GEO_CACHE: dict[str, dict] = {}
_WX_CACHE: dict[str, tuple[float, dict]] = {}


async def _ip_location(http) -> dict:
    for url in ("https://ipapi.co/json/", "https://ipwho.is/"):
        try:
            r = await http.get(url, timeout=8, headers={"User-Agent": "ATLAS-assistant/1.0"})
            d = r.json()
            if d.get("latitude") is not None:
                return {
                    "name": d.get("city") or "Current location",
                    "region": d.get("region") or "",
                    "country": d.get("country_name") or d.get("country") or "",
                    "latitude": d["latitude"],
                    "longitude": d["longitude"],
                }
        except Exception:
            continue
    raise ToolError("Couldn't determine your location. Set HOME_CITY in Settings.")


async def geocode(http, place: str) -> dict:
    key = place.lower().strip()
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    parts = [p.strip() for p in place.split(",") if p.strip()]
    r = await http.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": parts[0], "count": 10, "language": "en", "format": "json"},
        timeout=10,
    )
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        raise ToolError(f"Couldn't find a place called '{place}'.")
    best = results[0]
    if len(parts) > 1:
        hint = " ".join(parts[1:]).lower()
        for res in results:
            hay = " ".join(str(res.get(k, "")) for k in ("admin1", "country", "country_code", "admin2")).lower()
            if hint in hay or any(h in hay.split() for h in hint.split()):
                best = res
                break
    loc = {
        "name": best.get("name"),
        "region": best.get("admin1", ""),
        "country": best.get("country", ""),
        "latitude": best["latitude"],
        "longitude": best["longitude"],
    }
    _GEO_CACHE[key] = loc
    return loc


async def get_weather_data(ctx, location: str | None = None, days: int = 4) -> dict:
    place = (location or ctx.settings.home_city or "").strip()
    loc = await geocode(ctx.http, place) if place else await _ip_location(ctx.http)
    imperial = ctx.settings.units != "metric"
    cache_key = f"{loc['latitude']:.2f},{loc['longitude']:.2f},{imperial},{days}"
    cached = _WX_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < 600:
        return cached[1]
    r = await ctx.http.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m,is_day,precipitation",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset,uv_index_max",
            "timezone": "auto",
            "forecast_days": max(1, min(days, 7)),
            "temperature_unit": "fahrenheit" if imperial else "celsius",
            "wind_speed_unit": "mph" if imperial else "kmh",
        },
        timeout=12,
    )
    r.raise_for_status()
    d = r.json()
    cur = d.get("current", {})
    daily = d.get("daily", {})
    forecast = []
    for i, day in enumerate(daily.get("time", [])):
        forecast.append(
            {
                "date": day,
                "condition": WMO.get(daily["weather_code"][i], "Unknown"),
                "high": daily["temperature_2m_max"][i],
                "low": daily["temperature_2m_min"][i],
                "precip_chance": daily.get("precipitation_probability_max", [None] * (i + 1))[i],
                "uv_max": daily.get("uv_index_max", [None] * (i + 1))[i],
                "sunrise": daily.get("sunrise", [""] * (i + 1))[i][-5:],
                "sunset": daily.get("sunset", [""] * (i + 1))[i][-5:],
            }
        )
    out = {
        "location": ", ".join(x for x in (loc["name"], loc.get("region"), loc.get("country")) if x),
        "units": {"temp": "°F" if imperial else "°C", "wind": "mph" if imperial else "km/h"},
        "current": {
            "temperature": cur.get("temperature_2m"),
            "feels_like": cur.get("apparent_temperature"),
            "humidity": cur.get("relative_humidity_2m"),
            "wind": cur.get("wind_speed_10m"),
            "condition": WMO.get(cur.get("weather_code"), "Unknown"),
            "code": cur.get("weather_code"),
            "is_day": bool(cur.get("is_day", 1)),
        },
        "forecast": forecast,
    }
    _WX_CACHE[cache_key] = (time.time(), out)
    return out


@tool(
    "get_weather",
    "Current conditions and multi-day forecast. Omit location to use the user's home city (or IP location).",
    {
        "location": {"type": "string", "description": "City, optionally with region/country, e.g. 'Austin, Texas'"},
        "days": {"type": "integer", "description": "Forecast days 1-7. Default 4."},
    },
    label="Reading atmospheric data",
)
async def get_weather(ctx, location: str | None = None, days: int = 4):
    return await get_weather_data(ctx, location, int(days or 4))
