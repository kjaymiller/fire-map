# HTTPTrigger - Python

The `HTTP Trigger` function is the entry point for the service.

**url: <https://jm-func-us-fire-notify.azurewebsites.net>**
## How it works

The `HTTP Trigger` has two routes:

* the root route `/` returns the html of the landing page.
  `/location` checks a `location` for fire entries within the requested `distance` and return the results in `http` or `json` format.

- location=\<A Town or City in the World\> 
- distance = distance in meters (Default: 150000)
- format = json or http (Default: json) 

How this function is prepared:
This function uses FastAPI to process the routes passed from Azure Functions. This is how we're able to generate a templated html page with the results of the fire data.

## Learn more
* Function Docs - https://jm-func-us-fire-notify.azurewebsites.net/docs
* HTTPTrigger | Azure Functions documentation
Visit <https://aka.ms/azfunctions/httptrigger> for more information.

## License and Usage
> **Warning**
> This is for education/demo/noncommercial purposes only and should not be used for fire prevention/avoidance.
