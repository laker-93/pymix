# Fundamental bot

Fundamental bot swagger home page:

todo

## install

Install postgres

brew install postgresql

check binaries are in path with:

"which pg_config"

if not in path, find postgres  and run commands

export PG_HOME=/Library/PostgreSQL/12
export PATH=$PATH:$PG_HOME/bin

First install psycopg-binary:

pip install psycopg2-binary --force-reinstall --no-cache-dir

Then install psycopg2:

pip install psycopg2 --force-reinstall --no-cache-dir

Now try running app.

If have error:

ImportError: dlopen(/Users/lajp/workspace/python/FundamentalBot/venv/lib/python3.9/site-packages/psycopg2/_psycopg.cpython-39-darwin.so, 2): Library not loaded: libssl.1.1.dylib
  Referenced from: /Users/lajp/workspace/python/FundamentalBot/venv/lib/python3.9/site-packages/psycopg2/_psycopg.cpython-39-darwin.so
  Reason: image not found

May need to install openssl

brew install openssl@1.1

Find where the library is then add to path with:

export DYD_LIBRARY_PATH=$DYD_LIBRARY_PATH:/usr/local/opt/openssl\@1.1/lib/


## application logic

1. download portfolio (IB, HL, VGRD, COINBASE etc)
   1. portfolio = list of tickers and their weights
2. return portfolio
3. run portfolio through analyzer (fundamentals, sharpe ratio, etc)
   1. stock calculator
      1. get_sector()
      2. get_region()
      3. get_covar()
   2. port calculator
      1. get_sharpe_ratio()
      2. get_frontier()
4. display results
5. Have scheduled job that does the above nightly
6. can trigger emails on certain events (cross threshold, release of earnings reports etc)

## Healthcheck

The healthcheck GET API can be run via the app's swagger page:

`http://<app_host>:<app_port>/docs#`

Or via the following API:

`http://ecs-fargate-services-1022434680.eu-west-2.elb.amazonaws.com:81/healthcheck`

The service is healthy if it can connect to the prices db, its kafka consumer has not encountered an error and it can produce and consume a test message to kafka.
If these conditions are satisfied, the returned json from the healthcheck will contain:

`"is_healthy": true`

The healthcheck json also contains the timestamp of the earliest and latest ticker that has been consumed from kafka and
saved to the db. If the system is operating correctly then these timestamps should be no more than 2 minutes apart from
each other and within the last two minutes of the current UTC time during market open hours. Outside of marker open hours,
the kafka consumer will continue to consume prices however the db controller will not store a price in the db if it is the
same as the last stored price for that ticker. This avoids storing duplicate prices in the db outside of market hours.