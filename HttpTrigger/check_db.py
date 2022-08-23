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
    distance: int,
    max_count:int =10,
) -> dict[int, list[dict]]:
    """Check records in the cosmosdb container if an is is near the Location (distances in meters)"""
    fires_in_range = dict()

    results = container.query_items(
        query = f"SELECT  * FROM (SELECT c.id, c.geometry, (ST_DISTANCE(c.geometry, {location.geography})) as distance FROM c) as newTable where newTable.distance < {distance}",
        max_item_count=max_count,
        enable_cross_partition_query=True,
    )
    return list(results)


def load_samples(container: ContainerProxy) -> list:
    """
    Load all the samples from the cosmosdb container
    """

    results = container.query_items(
        query =f"SELECT c FROM c",
        enable_cross_partition_query=True,
    )
    return results.next()