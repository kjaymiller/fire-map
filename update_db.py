import json
import logging
import os   
import asyncio
import typing

from azure.cosmos.aio import CosmosClient
from azure.cosmos import PartitionKey


async def rebuild_container(
    database: str,
    container: str,
    ttl_seconds: int = 660,
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
    data: typing.Generator[dict, None, None],
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

        for record in data:
            await container.create_item(
                record,
                enable_automatic_id_generation=True,
                )
        logging.info(f"{len(data)} records added to {container=}")
    return True

if __name__ == "__main__":
    database=os.environ.get("COSMOS_DATABASE", None)
    container=os.environ.get("COSMOS_CONTAINER", None)