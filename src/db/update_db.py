import logging

from azure.cosmos import PartitionKey
from azure.cosmos.aio import CosmosClient

async def rebuild_container(
    database: str,
    container: str,
    ttl_seconds: int = 660,
):
    """Delete the existing container and create a new one with the same name.
    
    If ttl_seconds is not 0, the container's contents will delete themselves after the given number of seconds.
    """
    async with CosmosClient as client:
        try:
            db = client.get_database_client(database=database)
            await db.delete_container(container=container)
        except Exception as e:
            pass

        await database.create_container(
            id=container,
            partition_key=PartitionKey(path="/id", kind="Hash"),
            default_ttl=ttl_seconds,
            )
        logging.warning(f"Finished cleaning {container=}")
        return db.get_container_client(container)