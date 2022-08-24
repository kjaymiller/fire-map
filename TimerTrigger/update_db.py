import json
import logging
import os   
import asyncio

from azure.cosmos.aio import CosmosClient
from azure.cosmos import PartitionKey
from .process_gis_data import write_new_file_data


async def rebuild_container(
    database: str,
    container: str,
    ttl_seconds: int = 1800,
):
    """Delete the existing container and create a new one with the same name.
    
    If ttl_seconds is not 0, the container's contents will delete themselves after the given number of seconds.
    """
    async with CosmosClient.from_connection_string(
        os.environ.get("COSMOS_CONNECTION_STRING", None)
    ) as client:
        database = "recent-fire"
        logging.info(f"Cleaning {container=}")

        try:
            await client.get_database_client(database).delete_container(container=container)
        except Exception as e:
            pass

        await client.get_database_client(database).create_container(
            id=container,
            partition_key=PartitionKey(path="/id", kind="Hash"),
            default_ttl=ttl_seconds,
            )
        logging.info(f"Finished cleaning {container=}")
        return await client.get_database_client(database).get_container_client(container)

async def write_to_cosmos(
    data: dict,
    database: str,
    container: str
) -> bool:
    """Add the given JSON string to COSMOSDB"""
    logging.info("Connecting to CosmosDB")
    
    async with CosmosClient.from_connection_string(
        os.environ.get("COSMOS_CONNECTION_STRING", None)
    ) as client:
        logging.info(f"Writing to CosmosDB {database=} in {container=}") 
        container: str = client.get_database_client(database=database).get_container_client(container=container)

        logging.info("Writing to CosmosDB Container")
        logging.info(f"Writing {len(data)} records to {container=}")
        
        for record in data:
            await container.create_item(
                record,
                enable_automatic_id_generation=True,
                )
        logging.info(f"Finished writing {len(data)} records to {container=}")
    return True


async def load_and_write(gis_id:str, database: str, container: str):
    """Load the data from arcgis and write to cosmos"""
    gis_data = write_new_file_data(gis_id=gis_id)
    geojson_string = json.loads(gis_data.to_geojson)
    await write_to_cosmos(geojson_string['features'], database=database, container=container)


if __name__ == "__main__":
    database=os.environ.get("COSMOS_DATABASE", None)
    container=os.environ.get("COSMOS_CONTAINER", None)
    asyncio.run(load_and_write(gis_id="b8f4033069f141729ffb298b7418b653", database=database, container=container))