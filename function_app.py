import logging
import os

import azure.functions as func

# from src.db.update_db import write_to_cosmos
# from src.viirs.get_fire_data import get_fire_data

app = func.FunctionApp()

@app.function_name(name="mytimer")
@app.schedule(
    schedule="0 */5 * * * *",
    arg_name="mytimer",
    run_on_startup=True,
    use_monitor=False,
) 
def load_cosmos(mytimer: func.TimerRequest) -> None:
    """Fetch the time"""
    logging.info('Yo - Python timer trigger function ran at')
    # write_to_cosmos(
    #         data = get_fire_data(),
    #         database=os.environ.get('COSMOS_DATABASE'),
    #         container=os.environ.get('COSMOS_CONTAINER'),
    # )