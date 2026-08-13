"""
Fetches global FireMap data using NASA's VIIRS data via the FIRMS "area" API.

NASA retired the old country-based CSV endpoint (`/api/country/csv/...`) --
it now returns 400 "Invalid API call" for every request, key or no key.
Its replacement, the area-based endpoint, takes a bounding box instead of a
country code. We use "world" so every reload covers the whole globe --
the area API doesn't return country attribution either way, so there's no
useful per-country filtering to be had without a separate reverse-geocoding
step.
"""

import csv
import datetime
import logging
import os
from collections.abc import Iterator

import dotenv
import httpx
from geojson import Feature, Point

logger = logging.getLogger(__name__)

dotenv.load_dotenv()

API_KEY = os.environ.get("NASA_FIRMS_API_KEY")
SOURCE = "VIIRS_NOAA20_NRT"
AREA = "world"
DAY_RANGE = 1

DAY_NIGHT = {
    'D': 'Day',
    'N': 'Night',
    }

CONFIDENCE = {
    'n': 'nominal',
    'l': 'low',
    'h': 'high',
}


def parse_datetime(acq_date: str, acq_time: str) -> datetime.datetime:
    """Parse the date and time into a datetime object"""
    return datetime.datetime.strptime(f"{acq_date} {acq_time.zfill(4)}", "%Y-%m-%d %H%M")


def to_geojson(data: dict[str, str]) -> Feature:
    """Convert CSV data to GeoJSON Feature.

    `data` is a row from `csv.DictReader`, so every value comes in as a
    plain string -- the numeric fields (bright_ti4, frp, ...) are parsed to
    float later, in `postgres.feature_to_row`, not here.
    """
    return Feature(
        geometry=Point((float(data["longitude"]), float(data["latitude"]))),
        properties={
            'bright_ti4': data['bright_ti4'],
            'scan': data['scan'],
            'track': data['track'],
            'datetime': parse_datetime(data['acq_date'], data['acq_time']).isoformat(),
            'satellite': data['satellite'],
            'instrument': data['instrument'],
            'confidence': CONFIDENCE[data['confidence']],
            'version': data['version'],
            'bright_ti5': data['bright_ti5'],
            'frp': data['frp'],
            'daynight': DAY_NIGHT[data['daynight']],
        },
    )


def get_url(date: datetime.datetime) -> str:
    return (
        f"https://firms.modaps.eosdis.nasa.gov/api/area/csv/{API_KEY}/{SOURCE}"
        f"/{AREA}/{DAY_RANGE}/{date.strftime('%Y-%m-%d')}"
    )


def get_fire_data() -> Iterator[Feature]:
    date = datetime.datetime.now(datetime.UTC)
    text = httpx.get(get_url(date), timeout=30).text
    lines = text.strip().splitlines()

    # A response with only a header row (or none at all) means there was no
    # data for that date -- fall back to the previous day.
    if len(lines) <= 1:
        new_date = date - datetime.timedelta(days=1)
        logger.warning(f"No data for {date}. Trying {new_date}")
        text = httpx.get(get_url(new_date), timeout=30).text
        date = new_date
        lines = text.strip().splitlines()

    reader = csv.DictReader(lines, delimiter=",")

    for row in reader:
        yield to_geojson(row)


if __name__ == "__main__":
    for row in get_fire_data():
        print(row)
