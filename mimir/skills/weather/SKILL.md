---
name: weather
description: Get current conditions and 5-day forecast for a city via OpenWeatherMap. Requires `OPENWEATHER_API_KEY` in the environment. Use when the user asks about weather or when planning anything where weather is load-bearing (outdoor events, travel, watering schedules).
success_criteria:
  # The skill's job is to run get_weather and report. If we loaded
  # the SKILL.md but never invoked the script, the question wasn't
  # actually answered. The glob matches both the module-style
  # invocation (preferred — works from any cwd) and a direct script
  # invocation (legacy / advanced).
  any_of:
    - tool_call:
        name: Bash
        args:
          command_glob: "*mimir.skills.weather.get_weather*"
    - tool_call:
        name: Bash
        args:
          command_glob: "*get_weather.py*"
---

<!-- desc: Get current conditions and 5-day forecast for a city via OpenWeatherMap — requires OPENWEATHER_API_KEY. -->

# Weather

Wraps the OpenWeatherMap API. The container needs `OPENWEATHER_API_KEY`
exported (set it in `mimirbot/.env` if running there).

## Usage

Invoke via Python module syntax so the path works from any cwd —
mimir's bundled skills live inside the installed `mimir` package
(at `mimir/skills/weather/get_weather.py`), reachable via `-m`
regardless of where the venv lands or what directory the shell
tool spawns from.

```bash
# Default location (set inside the script — Victor, NY, US).
python3 -m mimir.skills.weather.get_weather

# Specific city. Comma-separated; ISO country codes for disambiguation.
python3 -m mimir.skills.weather.get_weather "London,UK"
python3 -m mimir.skills.weather.get_weather "San Francisco,CA,US"
```

Default output is plain text (current conditions + 5-day high/low/precip).
Add `--json` when you want to format the output yourself or pull specific
fields:

```bash
python3 -m mimir.skills.weather.get_weather "Tokyo,JP" --json
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
