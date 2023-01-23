import asyncio
import logging
import os

import azure.functions as func

from src.db.db import write_to_cosmos
from src.viirs.get_fire_data import get_fire_data

app = func.FunctionApp()

@app.function_name(name="mytimer")
@app.schedule(
    schedule="0 */10 * * * *",
    arg_name="mytimer",
    use_monitor=True,
) 
def load_cosmos(mytimer: func.TimerRequest) -> None:
    """Fetch the time"""
    logging.info("Python timer trigger function ran at --")
    write_to_cosmos(get_fire_data())

if __name__ == "__main__":
    write_to_cosmos(get_fire_data())