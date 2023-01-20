import typing
import logging
import os 
import asyncio

import dotenv
from azure.cosmos import PartitionKey

from src.db import async_client as client
from src.db import (
    async_client,
    database,
    COSMOS_DB,
    COSMOS_CONTAINER,
)

from src.viirs.get_fire_data import get_fire_data

dotenv.load_dotenv()


async def write_to_cosmos(
    data: typing.Generator[dict, None, None],
) -> bool:
    """Add the given JSON string to COSMOSDB"""
    logging.info("Connecting to CosmosDB")
    
    async with client:
        logging.info(f"Writing to CosmosDB {COSMOS_DB=} in {COSMOS_CONTAINER=}") 
        database = client.get_database_client(database=COSMOS_DB)
        container = database.get_container_client(container=COSMOS_CONTAINER)

        for record in data:
            await container.create_item(
                record,
                enable_automatic_id_generation=True,
                )
    return True


async def rebuild_container(
    database: database,
    container: str,
    ttl_seconds: int = 660,
):
    """
    Delete the existing container and create a new one with the same name.
    
    If ttl_seconds is not 0, the container's contents will delete themselves after the given number of seconds.
    """
    async with client:

        try:
            await database.delete_container(container=container)
        except Exception as e:
            pass

        await database.create_container(
            id=container,
            partition_key=PartitionKey(path="/id", kind="Hash"),
            default_ttl=ttl_seconds,
            )
        logging.warning(f"Finished cleaning {container=}")
        return database.get_container_client(container)


if __name__ == "__main__":
    asyncio.run(rebuild_container(database, os.environ.get("COSMOS_CONTAINER")))
    asyncio.run(write_to_cosmos(get_fire_data()))