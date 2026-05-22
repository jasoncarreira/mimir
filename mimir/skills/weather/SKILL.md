---
name: weather
description: Get current conditions and 5-day forecast for a city via OpenWeatherMap. Requires `OPENWEATHER_API_KEY` in the environment. Use when the user asks about weather or when planning anything where weather is load-bearing (outdoor events, travel, watering schedules).
subagent: true
allowed-tools:
  - Bash
params:
  type: object
  properties:
    city:
      type: string
      description: City name, optionally with country code (e.g. "Charlotte, NC, US" or "London, GB"). OpenWeatherMap accepts both city alone and city+region.
    days:
      type: integer
      description: Forecast horizon in days (1-5). Omit for "current conditions only".
      minimum: 1
      maximum: 5
  required: [city]
returns:
  title: weather_result
  description: Final structured weather payload for the parent agent. Includes current conditions and an optional daily forecast.
  type: object
  properties:
    city:
      type: string
      description: Normalized city name as returned by OpenWeatherMap.
    current:
      type: object
      description: Current conditions (temp, humidity, description).
      properties:
        temp_c: {type: number}
        humidity_pct: {type: number}
        description: {type: string}
    forecast:
      type: array
      description: Daily forecast entries. Empty when `days` was omitted from params.
      items:
        type: object
        properties:
          date: {type: string, description: "ISO date YYYY-MM-DD"}
          high_c: {type: number}
          low_c: {type: number}
          description: {type: string}
  required: [city, current]
---

# Weather

Wraps the OpenWeatherMap API. The container needs `OPENWEATHER_API_KEY`
exported (set it in `mimirbot/.env` if running there).

## Usage

```bash
# Default location (set inside the script — Victor, NY, US).
python3 skills/weather/get_weather.py

# Specific city. Comma-separated; ISO country codes for disambiguation.
python3 skills/weather/get_weather.py "London,UK"
python3 skills/weather/get_weather.py "San Francisco,CA,US"
```

Default output is plain text (current conditions + 5-day high/low/precip).
Add `--json` when you want to format the output yourself or pull specific
fields:

```bash
python3 skills/weather/get_weather.py "Tokyo,JP" --json
```

The JSON shape:

```json
{
  "current": {
    "location": "...",
    "description": "...",
    "temp_f": 72,
    "feels_like_f": 70,
    "humidity_pct": 55,
    "wind_mph": 8
  },
  "forecast": [
    {"date": "2026-05-04", "high_f": 75, "low_f": 58, "description": "...", "precip_chance_pct": 20},
    ...
  ]
}
```

## When to use

- User asks about weather in a specific place ("what's it like in Boulder
  right now?", "any rain this week in NYC?").
- Planning anything outdoor — give the high/low and precip chance for the
  relevant days, not the full 5-day spread.
- Following up on an earlier weather mention ("you said it'd be hot today,
  what's the forecast for tomorrow?").

## When NOT to use

- The user's already on a weather app and just venting about it.
- Hyperlocal questions (microclimates, exact rainfall) — the API is at
  city-resolution; defer to a more specialized source.
- If `OPENWEATHER_API_KEY` is unset the script exits with a clear error;
  surface that to the operator rather than guessing.
