import logging
from . import location_data
from . import check_db as db
import azure.functions as func
import os


# Load the environment variables from the .env file
cosmos_db = os.environ.get('COSMOS_DATABASE', None)
cosmos_cont = os.environ.get('COSMOS_CONTAINER', None)
conn_string = os.environ.get('COSMOS_CONNECTION_STRING', None)
container = db.get_cosmos_container(cosmos_db, cosmos_cont, conn_string)

def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    location = req.params.get('location', None)
    distances = [int(dist) for dist in req.params.get('distances', "15000,25000,50000").split(",")]
    
    if not location: 
        return func.HttpResponse(
             "This HTTP triggered function executed successfully, but no location was passed.",
             status_code=404,
        )

    try:
        location = location_data.fetch(location, distances)
        logging.info(f"{location=}")
        return db.check_location_radius(location, container, distances, max_count=10)

    except Exception as e:
        logging.error(e)
        return func.HttpResponse(
             f"An Error occurred.",
             status_code=500,
        )