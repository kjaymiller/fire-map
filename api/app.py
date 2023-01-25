
import dotenv
from more_itertools import bucket

from geojson import FeatureCollection
import fastapi
from fastapi import Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse

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
    high_low_conf = container.query_items("SELECT * FROM c WHERE c.properties.confidence != 'nominal'", enable_cross_partition_query=True)

    confidence_batches = bucket(
                entries,
                key=lambda x: x['properties']['confidence'],
            )
    
    confidences = dict()

    for key in list(confidence_batches):
        confidences[key] = len(list(confidence_batches[key]))


    print(confidences)




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
        },
    )