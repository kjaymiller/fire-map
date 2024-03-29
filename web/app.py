
import os
import dotenv
from more_itertools import bucket

from geojson import FeatureCollection
import fastapi
from fastapi import Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse

from azure.cosmos import PartitionKey

from src.db import (
    container
)

dotenv.load_dotenv()

api = fastapi.FastAPI(
    title="VIIRS Fire Detection API",
    version="0.1.0",
    verbose=True,
    )

templates = Jinja2Templates(directory="templates")

# Load the environment variables from the .env file


def get_fire_stats(fire_points: list[dict]) -> dict :
    """
    Get the fire stats from the fire points
    """
    if fire_points:
        closest_point = min(fire_points, key=lambda x: x['distance'])
    
    else:
        closest_point = None

    stats = {
        "closest": closest_point,
        "fire_count": len(fire_points),
    }
    return stats

@api.get("/", response_class=HTMLResponse)
def index(request: Request):
    """Return the homepage of the application"""

    entries = list(container.read_all_items())
    high_low_conf = container.query_items("SELECT * FROM c WHERE c.properties.confidence != 'low'", enable_cross_partition_query=True)

    confidence_batches = bucket(
                entries,
                key=lambda x: x['properties']['confidence'],
            )
    
    confidences = dict()

    for key in list(confidence_batches):
        confidences[key] = len(list(confidence_batches[key]))

    feature_collection = FeatureCollection(
        [point for point in high_low_conf]
    )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "points": feature_collection,
            "data": entries.__len__(),
            "confidences": confidences,
            "AZ_SUBSCRIPTION_KEY": os.environ.get("AZ_SUBSCRIPTION_KEY", None),
        },
    )

@api.get("/points/by_country/{country_id}")
def get_points_by_country(country_id: str):
    entries = container.query_items(
        query="SELECT * FROM c WHERE c.properties.country_id = @country_id",
        parameters=[{"name": "@country_id", "value": country_id}],
    )

    return [point for point in entries]

@api.get("/points/{item_id}")
def near_location(item_id:str):
    point = container.read_item(
        item_id,
        partition_key=item_id
    )

    points_around=list(
        container.query_items(
                f"SELECT * FROM firemap c WHERE ST_DISTANCE(c.geometry, {point['geometry']}) < 300"),
                partition_key="id",
                )
    point['properties']['points_around'] = points_around

    return point