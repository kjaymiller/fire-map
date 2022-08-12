from azure.cosmos import CosmosClient
from azure.cosmos.container import ContainerProxy
import os
import logging
import json
from .location_data import Location

connection_string = os.environ.get("COSMOS_CONNECTION_STRING", None)

def get_cosmos_container(database:str, container:str, connection_string:str) -> ContainerProxy:
    client = CosmosClient.from_connection_string(connection_string)
    logging.info(f"Writing to CosmosDB {database=} in {container=}") 
    return client.get_database_client(database=database).get_container_client(container=container)


def check_location_radius(
    location: Location,
    container: ContainerProxy,
    distances: list[int],
    max_count:int =10,
) -> dict[int, list[dict]]:
    """
    Check is near any of the points in the cosmosdb container distances: list of distances (in meters) to check
    """
    fires_in_range = dict()

    for distance in distances:
        results = container.query_items(
            query =f"SELECT c.id, c.geometry FROM c WHERE ST_DISTANCE(c.geometry, {location.geography}) < {distance}",
            max_item_count=max_count,
            enable_cross_partition_query=True,
        )
        fires_in_range[distance] = list(results)
        
    return json.dumps(fires_in_range)


def load_samples(container: ContainerProxy, count:int=1) -> list:
    """
    Load all the samples from the cosmosdb container
    """
    results = container.query_items(
        query =f"SELECT c.id, c.geometry FROM c",
        max_item_count=count,
        enable_cross_partition_query=True,
    )
    return results.next()