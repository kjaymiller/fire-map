import os
import dotenv
import logging
import asyncio


dotenv.load_dotenv()


from azure.cosmos import PartitionKey

from src.db import async_client as client

from src.db import (
    COSMOS_DB,
    COSMOS_CONTAINER,
)
from src.db.db import write_to_cosmos
from src.viirs.get_fire_data import get_fire_data


COUNTRY = "USA"

async def rebuild_container(
    database: str = COSMOS_DB,
    container: str = COSMOS_CONTAINER,
    ttl_seconds: int = 3600,
):
    """Delete the existing container and create a new one with the same name.
    
    If ttl_seconds is not 0, the container's contents will delete themselves after the given number of seconds.
    """
    async with client:
        logging.warning("Deleting container")
        db = client.get_database_client(database=database)
        await db.delete_container(container=container)

        logging.warning("Creating container")
        await db.create_container(
            id=container,
            partition_key=PartitionKey(path="/id", kind="Hash"),
            default_ttl=ttl_seconds,
            )
        logging.warning(f"Finished cleaning {container=}")
        return db.get_container_client(container)

if __name__ == "__main__":
    asyncio.run(rebuild_container())
    write_to_cosmos(get_fire_data(country=COUNTRY))