import os
import azure.functions as func
from . import process_gis_data
from . import update_db


async def main(reqTimer: func.TimerRequest) -> None:
    database=os.environ.get("COSMOS_DATABASE", None)
    container=os.environ.get("COSMOS_CONTAINER", None)
    await update_db.load_and_write(gis_id="b8f4033069f141729ffb298b7418b653", database=database, container=container)
