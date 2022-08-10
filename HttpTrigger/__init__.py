import logging

import azure.functions as func
import httpx


def main(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function processed a request.')

    location = req.params.get('location', None)
    if location:
        url ='https://atlas.microsoft.com/search/address/json?',
        params = {
            "api-version": "1.0",
            "query": location,
            "subscription-key": os.environ.get("AZ_MAPS_KEY", None),
        }
        
        r = httpx.get(url, params=params)
        return func.HttpResponse(r.json, status_code=200)
    else:
        return func.HttpResponse(
             "This HTTP triggered function executed successfully, but no location was passed.",
             status_code=404,
        )
