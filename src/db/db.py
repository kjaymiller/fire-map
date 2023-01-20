import typing
import logging
import os 
import asyncio

import dotenv
from azure.cosmos import PartitionKey

from src.db import async_client as client
from src.db import (
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