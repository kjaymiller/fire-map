import logging
from . import location_data
from . import check_db as db

from api import api
from fastapi import Request, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from more_itertools import flatten

import nest_asyncio
import azure.functions as func
import os

nest_asyncio.apply()

templates = Jinja2Templates(directory="templates")

# Load the environment variables from the .env file
cosmos_db = os.environ.get('COSMOS_DATABASE', None)
cosmos_cont = os.environ.get('COSMOS_CONTAINER', None)
conn_string = os.environ.get('COSMOS_CONNECTION_STRING', None)
container = db.get_cosmos_container(cosmos_db, cosmos_cont, conn_string)


def add_type(point:dict):
    point['type'] = 'Feature'
    return point

def get_fire_map(map_data):
    """return the fire map as geoJSON"""
    wrapper = {
        "type": "FeatureCollection",
        "features": [add_type(point) for point in flatten(map_data.values())]
    }
    return wrapper

@api.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Return the homepage of the application"""
    return templates.TemplateResponse("index.html", {"request": request})


@api.get("/location/")
async def get_fires_in_distance(
    request: Request, location: str, distances: str, format:str ="json",
    ) -> dict[str, list[dict]]:
    """Return the fires in a given distance from a given location"""
    if not location: 
        raise HTTPException(
             detail="This HTTP triggered function executed successfully, but no location was passed.",
             status_code=500,
        )
    
    if not distances:
        distances = "25000,75000,150000"
    
    if format not in ["json", "html"]:
        raise HTTPException(
             detail="Incorrect format specified. Please use either 'json' or 'html'.",
             status_code=500,
        )

    try:
        distances = [int(distance) for distance in distances.split(',')]

    except ValueError as error:
        logging.error(error)
        raise HTTPException(
            status_code=500,
            detail= f"Invalid distance. Please use meters. {error=}",
        )

    try:
        location = location_data.fetch(location)
        logging.info(f"Found {location=}")
        fire_points = db.check_location_radius(location, container, distances, max_count=10)

    except Exception as e:
        logging.error(e)
        raise HTTPException(
             detail=f"An Error occurred.{e}",
             status_code=500,
        )
    if format=="json":
        return fire_points
    
    if format=="html":
        points = get_fire_map(fire_points)
        return templates.TemplateResponse(
            "map.html",
            {
                "request": request,
                "query": location.name,
                "points": points
            }
        )

async def main(req: func.HttpRequest, context: func.Context) -> func.HttpResponse:
    logging.info(f"{req=}, {context=}")
    return func.AsgiMiddleware(api).handle(req, context)