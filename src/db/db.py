import typing
import logging
from azure.cosmos import PartitionKey

from src.db import (
    container,
)

from src.viirs.get_fire_data import get_fire_data


def write_to_cosmos(
    data: typing.Generator[dict, None, None],
) -> bool:
    """Add the given JSON string to COSMOSDB"""
    logging.info("Connecting to CosmosDB")
    
    for record in data:
        container.create_item(
            record,
            enable_automatic_id_generation=True,
            )

    return True