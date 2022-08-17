# HTTPTrigger - Python

The `HTTP Trigger` function is the entry point for the service.
## How it works

The `HTTP Trigger` is accessible using the functions URL:
<https://aka.ms/azfunctions/python/firemap>

and add the following query string parameters:
- location=\<A Town or City in the World\> 
  
> Note: *use a country along with the city to get the most accurate results*. So San Diego, USA is more accurate than just San Diego.

- distances = \<A string of distances in meters, separated by a semicolon> (Default: 25000, 75000, 150000)

The more distances you specify, the longer it takes to run the script.

## Learn more
Visit <https://aka.ms/azfunctions/httptrigger> for more information.

