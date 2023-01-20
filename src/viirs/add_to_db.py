import logging
import typing

from db import container

async def write_to_cosmos(
    data: typing.Generator[dict, None, None],
    container: container,
) -> bool:
    """Add the given JSON string to COSMOSDB"""
    logging.info("Connecting to CosmosDB")
    
    async with container:
        for record in data:
            await container.create_item(
                record,
                enable_automatic_id_generation=True,
            )
    return True
