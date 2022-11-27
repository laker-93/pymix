import datetime
import logging

from sqlalchemy import cast, Date, func

from pymix import constants
from pymix.controllers.base_controller import BaseController
from sqlalchemy.future import select

from pymix.db_model.Price import Price

logger = logging.getLogger(__name__)


class DbStoreController(BaseController):
    """
    Class to provide logic for storing a row to a database.
    There should only be one instance of this class.
    """

    def __init__(self, app_configuration, db_session):
        """
        Constructor of the controller.
        """
        self._db_session = db_session
        self._app_configuration = app_configuration
        self._stored_rows_utc_datetime = {}
        self._latest_stored_price = {}

    @property
    def db_session(self):
        """
        The getter for the db_session.
        """
        return self._db_session.session

    async def get_healthcheck(self) -> dict:
        """
        This service is healthy if:
         1) can connect to the db
         2) can consume from Kafka
        :return: dictionary containing controller health information
        """
        # guilty until proven innocent
        healthy = False
        reason = ''
        try:
            # test if we can connect to the db and get the first row
            first_row = await self._get_first_row()
            # test the first row contains the data we expect
            assert isinstance(first_row, Price), first_row

        except Exception as ex:
            logger.exception(ex)
            reason = 'unable to connect to get first row from db'
        else:
            healthy = True

        latest_updated_ticker = max(self._stored_rows_utc_datetime,
                                    key=self._stored_rows_utc_datetime.get) if self._stored_rows_utc_datetime else None
        earliest_updated_ticker = min(self._stored_rows_utc_datetime,
                                      key=self._stored_rows_utc_datetime.get) if self._stored_rows_utc_datetime else None

        max_n_rows_to_store = self._app_configuration[constants.db_settings][constants.max_n_rows_to_store]
        total_n_stored_entries = None
        try:
            total_n_stored_entries = await self._get_total_n_rows()
        except Exception as ex:
            logger.exception(ex)
            reason += ' unable to get total n rows from db'
        else:
            if total_n_stored_entries > max_n_rows_to_store:
                healthy = False
                reason = f"total number of stored entries {total_n_stored_entries} exceeds max threshold {max_n_rows_to_store}. Use /delete_intraday_prices endpoint to clear db"
        return {
            "is_healthy": healthy,
            "is_healthy_reason": reason,
            "n_tickers": len(self._stored_rows_utc_datetime),
            "total_n_stored_entries": total_n_stored_entries,
            "latest_stored_ticker": {
                latest_updated_ticker: self._stored_rows_utc_datetime.get(latest_updated_ticker)},
            "earliest_stored_ticker": {
                earliest_updated_ticker: self._stored_rows_utc_datetime.get(earliest_updated_ticker)},
        }


    async def update_or_store_row(self, ticker, bid, ask, src, last_updated=None):
        """
        Takes a ticker and updates the entry if present, else creates it
        :param ticker: The ticker
        :param bid: float of the bid price for the ticker
        :param ask: float of the ask price for the ticker
        :param src: string of the source of the data: bbg, reuters, etc
        :return:
        """
        date_added = last_updated if last_updated else datetime.datetime.utcnow()
        price = Price(ticker=ticker, bid=bid, ask=ask, source=src, last_updated_by="priceconsumer",
                      date_added=date_added)

        query = select(Price).where(
            Price.ticker == ticker).order_by(Price.date_added.desc())

        result_not_found_log_str = f"No price in db for {ticker}"
        result = await self._get_first_db_result(query, result_not_found_log_str)
        if result is None:
            return await self.store_row(ticker, bid, ask, src, last_updated)

        session = self.db_session
        async with session() as session:
            result.bid = bid
            result.ask = ask
            result.source = src
            result.date_added = last_updated
            async with session.begin():
                session.add(result)

            await session.commit()
        self._stored_rows_utc_datetime[ticker] = datetime.datetime.utcnow()
        self._latest_stored_price[ticker] = price


    async def store_row(self, ticker, bid, ask, src, last_updated=None):
        """
        Takes a row to store to the db
        :param ticker: The ticker
        :param bid: float of the bid price for the ticker
        :param ask: float of the ask price for the ticker
        :param src: string of the source of the data: bbg, reuters, etc
        :return:
        """
        date_added = last_updated if last_updated else datetime.datetime.utcnow()
        price = Price(ticker=ticker, bid=bid, ask=ask, source=src, last_updated_by="priceconsumer",
                      date_added=date_added)

        session = self.db_session
        async with session() as session:
            async with session.begin():
                session.add(price)

            await session.commit()
        self._stored_rows_utc_datetime[ticker] = datetime.datetime.utcnow()
        self._latest_stored_price[ticker] = price

    async def get_bid_ask(self, ticker):
        """
        Gets the latest bid/ask for this ticker from the local cache if present, else db lookup
        :param ticker:
        :return: The bid, ask
        """

        if ticker in self._latest_stored_price:
            return self._latest_stored_price.get(ticker)

        # Order with respect to time so the first is the most relevant.
        query = select(Price).where(Price.ticker == ticker and Price.is_active == True).order_by(
            Price.date_added.desc())
        result_not_found_log_str = f"No price in db for {ticker}"
        return await self._get_first_db_result(query, result_not_found_log_str)

    async def get_bid_ask_date(self, ticker, date):
        """
        Gets the bid/ask for this ticker at a given date from the database
        :param ticker:
        :return: The bid, ask
        """
        # Order with respect to time so the first is the most relevant.
        query = select(Price).where(
            Price.ticker == ticker and Price.is_active == True and cast(Price.date_added, Date) == date). \
            order_by(Price.date_added.desc())
        result_not_found_log_str = f"No price in db for {ticker} at {date}"
        return await self._get_first_db_result(query, result_not_found_log_str)

    async def get_all_consumed_from_source(self, date, source):
        query = select(Price).filter(Price.source == source).\
            filter(cast(Price.date_added, Date) == date).\
            order_by(Price.date_added.desc())

        return await self._get_all_db_result(query)

    async def delete_intraday_prices_older_than_n_days(self, n_days) -> int:
        """
        For each ticker deletes the intraday prices older than n_days.
        The close price of prices older than n_days will be kept.
        :param n_days:
        """

        session = self.db_session
        n_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=n_days)
        query = select(Price).where(Price.date_added < n_days_ago).order_by(Price.date_added.desc(), Price.ticker)
        tickers_seen = {} # (ticker, date_added)
        n_prices_deleted = 0
        async with session() as session:
            async with session.begin():
                results = await session.execute(query)
                for result in results:
                    entry = result[0]
                    date = entry.date_added.date()
                    ticker = entry.ticker
                    if (ticker, date) in tickers_seen:
                        logger.debug(f"removing entry: {entry}")
                        await session.delete(entry)
                        n_prices_deleted += 1
                    else:
                        logger.debug(f"keeping closing price for: {entry}")
                        tickers_seen[(ticker, date)] = True
        return n_prices_deleted

    async def _get_all_db_result(self, query):
        """
        Runs the query in the database. Returns the first element if there is one else returns none
        :param query:
        :param result_not_found_log_str:
        :return:
        """
        session = self.db_session
        async with session() as session:
            results = await session.execute(query)
        return results

    async def _get_first_db_result(self, query, result_not_found_log_str):
        """
        Runs the query in the database. Returns the first element if there is one else returns none
        :param query:
        :param result_not_found_log_str:
        :return:
        """
        result = None
        session = self.db_session
        async with session() as session:
            results = await session.execute(query)
            try:
                (result,) = results.first()
            except TypeError:
                logger.warning(result_not_found_log_str)
        return result


    async def _get_total_n_rows(self):
        """
        Gets the count for the number of rows in the db
        :return:
        """
        session = self.db_session
        query = func.count(Price.id)
        async with session() as session:
            result = await session.execute(query)
            n_rows = result.scalar()
        return n_rows


    async def _get_first_row(self):
        """
        Gets the first row from the prices db
        :return:
        """

        query = select(Price)
        result_not_found_log_str = "Database is empty - does not contain a first row"
        return await self._get_first_db_result(query, result_not_found_log_str)

