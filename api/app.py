import json
import logging
import os

import dotenv
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
    feature_collection = FeatureCollection(
       [point for point in entries]
    )

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "points": feature_collection,
            "data": entries.__len__()
        },
    )


# @api.get("/location/")
# async def get_fires_in_distance(
#     request: Request, location: str, distance: int=15000, format:str ="json",
#     ) -> dict[str, list[dict]]:
#     """Return the fires in a given distance from a given location"""
#     if not location: 
#         raise HTTPException(
#              detail="This HTTP triggered function executed successfully, but no location was passed.",
#              status_code=500,
#         )
        
#     if format not in ["json", "html"]:
#         raise HTTPException(
#              detail="Incorrect format specified. Please use either 'json' or 'html'.",
#              status_code=500,
#         )


#     try:
#         location = location_data.fetch(location)
#         logging.info(f"Found {location=}")
#         fire_points = db.check_location_radius(location, container, distance)

#     except Exception as e:
#         logging.error(e)
#         raise HTTPException(
#              detail=f"An Error occurred.{e}",
#              status_code=500,
#         )
    
    
#     if format=="json":
#         return fire_points
    
#     if format=="html":
#         fire_details = get_fire_stats(fire_points)
#         points = get_fire_map(fire_points)
        
#         return templates.TemplateResponse(
#             "map.html",
#             {
#                 "request": request,
#                 "query": location.name,
#                 "points": points,
#                 "base_point": location.geography['coordinates'],
#                 "fire_details": fire_details,
#             }
#         )

# # for Azure Functions
# async def main(req: func.HttpRequest, context: func.Context) -> func.HttpResponse:
#     logging.info(f"{req=}, {context=}")
#     return func.AsgiMiddleware(api).handle(req, context)