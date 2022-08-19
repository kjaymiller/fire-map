import os
import httpx
from collections import namedtuple
from pprint import pprint

url ='https://atlas.microsoft.com/search/address/json?'
api_key = os.environ.get("AZ_MAPS_KEY", None)        

Location = namedtuple("Location", ["name", "address", "geography"])

    
def fetch(query: str, url: str=url, api_key: str=api_key) -> str:
    """Given a Location, Perform a search using Azure Maps and return the top choice"""
    params = {
        "api-version": "1.0",
        "query": query,
        "subscription-key": api_key
    }
    
    r = httpx.get(url, params=params)
    base_data = r.json()['results'][0]
    base_address = base_data['address']
    if not 'municipality' in base_address:
        raise ValueError(f"No municipality found in {query}. Try a nearby town or city")
    
    location = Location(
        name=base_data['address']['freeformAddress'],
        address= ", ".join((base_address.get('municipality'), base_address['countrySubdivision'], base_address['country'])),
        geography = {
            "type": "Point",
            "coordinates": [base_data['position']['lon'], base_data['position']['lat']]
        }
    )

    return location
    


if __name__ == '__main__':
    pprint(fetch_location_data('Seattle, WA'))