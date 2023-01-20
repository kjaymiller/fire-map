"""
Fetches FireMap Data for the US using NASA's VIIRS data
"""

import csv
import datetime
import os

import httpx
import dotenv
from geojson import Feature, Point
from io import StringIO

dotenv.load_dotenv()

API_KEY = os.environ.get("NASA_API_KEY")
SOURCE = "VIIRS_NOAA20_NRT"
DAY_RANGE = 1
COUNTRY = "USA"

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


def to_geojson(data: dict[str, str|float]) -> Feature:
    """Convert CSV data to GeoJSON Feature"""
    return Feature(
        geometry=Point((float(data["longitude"]), float(data["latitude"]))),
        properties={
            'country_id': data['country_id'],
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


def get_url(date):
    return  f"https://firms.modaps.eosdis.nasa.gov/api/country/csv/{API_KEY}/{SOURCE}/{COUNTRY}/{DAY_RANGE}/{date.strftime('%Y-%m-%d')}"


def get_fire_data() -> csv.DictReader:
    f = httpx.get(get_url(datetime.datetime.now(datetime.timezone.utc)), timeout=30).text
    reader = csv.DictReader(StringIO(f), delimiter=",")

    if reader.line_num == 0:
        f = httpx.get(get_url(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)), timeout=30).text
        reader = csv.DictReader(StringIO(f), delimiter=",")

    for row in reader:
        yield to_geojson(row)
