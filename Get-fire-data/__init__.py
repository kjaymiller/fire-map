import datetime
import logging
import os
import asyncio
import azure.functions as func
from process_fire_data import load_and_write



async def main(mytimer: func.TimerRequest) -> None:
    utc_timestamp = datetime.datetime.utcnow().replace(
        tzinfo=datetime.timezone.utc).isoformat()

    if mytimer.past_due:
        database=os.environ.get("COSMOS_DATABASE", None)
        container=os.environ.get("COSMOS_CONTAINER", None)
        await load_and_write(database=database, container=container)
        logging.info('function completed')
    else:
        logging.info('Timer signalled prematurely')

if __name__ == "__main__":
    asyncio.run(main())