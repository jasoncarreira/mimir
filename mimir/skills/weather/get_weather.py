#!/usr/bin/env python3
"""Get weather forecast using OpenWeatherMap API.

Usage:
  python3 get_weather.py [location] [--json]

  location  City name (default: Victor,NY,US)
  --json    Output raw JSON instead of formatted text
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict

API_KEY = os.environ.get('OPENWEATHER_API_KEY')
if not API_KEY:
    print('Error: OPENWEATHER_API_KEY not set', file=sys.stderr)
    sys.exit(1)

args = sys.argv[1:]
output_json = '--json' in args
args = [a for a in args if a != '--json']
location = args[0] if args else 'Victor,NY,US'
# URL-encode the location so cities with spaces ("Victor, NY, US",
# "San Francisco,CA,US") don't break the OpenWeatherMap query string.
# OpenWeatherMap accepts both comma- and space-separated forms; the
# server tolerates either as long as the spaces are percent-encoded.
location_q = urllib.parse.quote(location, safe=',')

units = 'imperial'
unit_label = '°F'


def fetch(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.load(r)


base = 'https://api.openweathermap.org/data/2.5'

result = {}

# Current conditions
try:
    current = fetch(f'{base}/weather?q={location_q}&units={units}&appid={API_KEY}')
    result['current'] = {
        'location': current['name'],
        'description': current['weather'][0]['description'],
        'temp_f': round(current['main']['temp']),
        'feels_like_f': round(current['main']['feels_like']),
        'humidity_pct': current['main']['humidity'],
        'wind_mph': round(current['wind']['speed']),
    }
except Exception as e:
    print(f'Error fetching current conditions: {e}', file=sys.stderr)

# 5-day forecast
try:
    forecast = fetch(f'{base}/forecast?q={location_q}&units={units}&appid={API_KEY}')
    days = defaultdict(list)
    for item in forecast['list']:
        date = item['dt_txt'].split()[0]
        days[date].append(item)

    result['forecast'] = []
    for date, items in sorted(days.items()):
        temps = [i['main']['temp'] for i in items]
        desc = items[len(items) // 2]['weather'][0]['description']
        pop = max(i.get('pop', 0) for i in items)
        result['forecast'].append({
            'date': date,
            'high_f': round(max(temps)),
            'low_f': round(min(temps)),
            'description': desc,
            'precip_chance_pct': round(pop * 100),
        })
except Exception as e:
    print(f'Error fetching forecast: {e}', file=sys.stderr)

# Both fetches failed → nothing to report. Exit non-zero so the caller (and
# the agent) can distinguish a real failure from an empty result — previously
# this exited 0 and masked auth/network errors (chainlink #325).
if not result:
    print('weather: both current-conditions and forecast fetches failed',
          file=sys.stderr)
    sys.exit(1)

if output_json:
    print(json.dumps(result, indent=2))
else:
    if 'current' in result:
        c = result['current']
        print(f"Current: {c['location']}")
        print(f"  {c['description'].title()}, {c['temp_f']}{unit_label} (feels like {c['feels_like_f']}{unit_label})")
        print(f"  Humidity: {c['humidity_pct']}%  Wind: {c['wind_mph']} mph")
        print()
    if 'forecast' in result:
        print('5-Day Forecast:')
        for day in result['forecast']:
            print(f"  {day['date']}  High: {day['high_f']}{unit_label}  Low: {day['low_f']}{unit_label}  {day['description'].title()}  Precip: {day['precip_chance_pct']}%")
