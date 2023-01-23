import json
import logging
import os

import dotenv
from more_itertools import bucket
from azure.cosmos import CosmosClient

from geojson import FeatureCollection
import fastapi
from fastapi import Request, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse

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

    by_confidence = list(
            bucket(
                entries,
                key=lambda x: x['properties'].get('confidence')
            )
        )
    
    print(list(by_confidence['low']))


    feature_collection = FeatureCollection(
       [point for point in entries]
    )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "points": feature_collection,
            "data": entries.__len__(),
            "confidence_counts": confidence_counts,
        },
    )