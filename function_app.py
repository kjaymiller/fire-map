import logging
import azure.functions as func
import datetime

app = func.FunctionApp()

@app.function_name(name="TimerTrigger")
@app.schedule(arg_name="TimerTrigger", schedule="0 */5 * * * *")
def test_function(mytimer: func.TimerRequest) -> None:
    utc_timestamp = datetime.datetime.utcnow().replace(
        tzinfo=datetime.timezone.utc).isoformat()

    if mytimer.past_due:
        logging.info('The timer is past due!')

    logging.info('Python timer trigger function ran at %s', utc_timestamp)
