from unittest.mock import MagicMock, AsyncMock, create_autospec

from pymix import constants
from pymix.controllers.db_store_controller import DbStoreController
from pymix.factories.db_session_factory import DbSession
from pymix.db_model.Price import Price

import asyncio
import asynctest
import datetime


class TestDbStoreController(asynctest.TestCase):

    def create_async_db_session_context(self):
        """
        The DbSession class has a 'session' attribute that when called creates a db session that can be called as an
        asyncio context.
        :return: the mock for the DbSession class
        """
        # TODO crazy madness could be improved by writing an adapter for the SQLAlchemy async sessions
        mock_db_session_maker = create_autospec(DbSession)
        mock_db_session_context = AsyncMock()
        mock_db_session_context.__aenter__.return_value = mock_db_session_context
        mock_session = MagicMock(return_value=mock_db_session_context)
        mock_db_session_maker.session = mock_session
        return mock_db_session_maker

    def setUp(self):
        self.app_config = {
            constants.kafka_settings: {
                "conn_string": "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/prices",
                "sql_alchemy_logging": "False",
            },
            constants.db_settings: {
                constants.max_n_rows_to_store: 10
            }
        }


    async def test_get_healthcheck_healthy_single(self):

        mock_db_session_maker = self.create_async_db_session_context()
        mock_db_session_context = mock_db_session_maker.session()
        mock_db_session_context.begin = asynctest.MagicMock()

        controller = DbStoreController(self.app_config, mock_db_session_maker)

        test_ticker = "foo"
        test_bid = 1
        test_ask = 2
        test_src = "bbg"

        await controller.store_row(test_ticker, test_bid, test_ask, test_src)
        test_ticker_update_time = datetime.datetime.utcnow().replace(microsecond=0)

        total_n_stored_entries = 2
        query_result = MagicMock()
        query_result.first = MagicMock(return_value=(Price(ticker=test_ticker, bid=test_bid, ask=test_ask, source=test_src),))
        query_result.scalar = MagicMock(return_value=total_n_stored_entries)
        mock_db_session_context.execute.return_value = query_result

        healthcheck = await controller.get_healthcheck()
        expected = {
            "is_healthy": True,
            "is_healthy_reason": "",
            "total_n_stored_entries": total_n_stored_entries,
            "n_tickers": 1,
            "latest_stored_ticker": {test_ticker: test_ticker_update_time},
            "earliest_stored_ticker": {test_ticker: test_ticker_update_time},
        }
        self.assertEqual(len(expected.keys()), len(healthcheck.keys()))
        self.assertEqual(healthcheck["is_healthy"], expected["is_healthy"])
        self.assertEqual(healthcheck["latest_stored_ticker"][test_ticker].replace(microsecond=0), expected["latest_stored_ticker"][test_ticker])
        self.assertEqual(healthcheck["earliest_stored_ticker"][test_ticker].replace(microsecond=0), expected["earliest_stored_ticker"][test_ticker])
        self.assertEqual(healthcheck["n_tickers"], expected["n_tickers"])

    async def test_get_healthcheck_healthy_multiple_tickers(self):

        mock_db_session_maker = self.create_async_db_session_context()
        mock_db_session_context = mock_db_session_maker.session()
        mock_db_session_context.begin = asynctest.MagicMock()

        controller = DbStoreController(self.app_config, mock_db_session_maker)

        test_ticker = "foo"
        test_bid = 1
        test_ask = 2
        test_src = "bbg"

        await controller.store_row(test_ticker, test_bid, test_ask, test_src)
        test_ticker_1_update_time = datetime.datetime.utcnow().replace(microsecond=0)

        await asyncio.sleep(1)

        test_ticker_2 = "bar"
        test_bid_2 = 2
        test_ask_2 = 3
        test_src_2 = "bbg"

        await controller.store_row(test_ticker_2, test_bid_2, test_ask_2, test_src_2)
        test_ticker_2_update_time = datetime.datetime.utcnow().replace(microsecond=0)
        total_n_stored_entries = 2

        query_result = MagicMock()
        query_result.first = MagicMock(return_value=(Price(ticker=test_ticker, bid=test_bid, ask=test_ask, source=test_src),))
        query_result.scalar = MagicMock(return_value=total_n_stored_entries)
        mock_db_session_context.execute.return_value = query_result

        healthcheck = await controller.get_healthcheck()
        expected = {
            "is_healthy": True,
            "is_healthy_reason": "",
            "total_n_stored_entries": total_n_stored_entries,
            "n_tickers": 2,
            "latest_stored_ticker": {test_ticker_2: test_ticker_2_update_time},
            "earliest_stored_ticker": {test_ticker: test_ticker_1_update_time},
        }
        self.assertEqual(len(expected.keys()), len(healthcheck.keys()))
        self.assertEqual(healthcheck["is_healthy"], expected["is_healthy"])
        self.assertEqual(healthcheck["latest_stored_ticker"][test_ticker_2].replace(microsecond=0), expected["latest_stored_ticker"][test_ticker_2])
        self.assertEqual(healthcheck["earliest_stored_ticker"][test_ticker].replace(microsecond=0), expected["earliest_stored_ticker"][test_ticker])
        self.assertEqual(healthcheck["n_tickers"], expected["n_tickers"])


    async def test_get_healthcheck_unhealthy_sauron_down(self):
        mock_db_session_maker = self.create_async_db_session_context()
        mock_db_session_context = mock_db_session_maker.session()
        mock_db_session_context.execute.side_effect = Exception()

        total_n_stored_entries = 2
        controller = DbStoreController(self.app_config, mock_db_session_maker)

        healthcheck = await controller.get_healthcheck()

        expected = {
            "is_healthy": False,
            "is_healthy_reason": "unable to connect to get first row from db unable to get total n rows from db",
            "total_n_stored_entries": None,
            "n_tickers": 0,
            "latest_stored_ticker": {None: None},
            "earliest_stored_ticker": {None: None},
        }
        self.assertDictEqual(healthcheck, expected)

    async def test_get_price_local_cache(self):
        mock_db_session_maker = self.create_async_db_session_context()
        mock_db_session_context = mock_db_session_maker.session()
        mock_db_session_context.begin = asynctest.MagicMock()
        mock_db_session_context.execute = MagicMock()

        controller = DbStoreController(self.app_config, mock_db_session_maker)

        test_ticker = "foo"
        test_bid = 1
        test_ask = 2
        test_src = "bbg"
        date_added = datetime.datetime.utcnow()
        expected_price = Price(ticker=test_ticker, bid=test_bid, ask=test_ask, source=test_src, last_updated_by="priceconsumer", date_added=date_added)

        await controller.store_row(test_ticker, test_bid, test_ask, test_src, last_updated=date_added)

        actual_price = await controller.get_bid_ask(test_ticker)
        self.assertDictEqual(expected_price.as_dict(), actual_price.as_dict())
        # No call to execute a command should be made since this price should be locally cached so assert the call count is 0.
        self.assertEqual(mock_db_session_context.execute.call_count, 0)

    async def test_repeat_price(self):
        """
        Test that the same price posted twice to the controller results in two prices being stored in the db
        :return:
        """
        mock_db_session_maker = self.create_async_db_session_context()
        mock_db_session_context = mock_db_session_maker.session()
        mock_db_session_context.begin = asynctest.MagicMock()
        mock_db_session_context.add = MagicMock()

        controller = DbStoreController(self.app_config, mock_db_session_maker)

        test_ticker = "foo"
        test_bid = 1
        test_ask = 2
        test_src = "bbg"

        await controller.store_row(test_ticker, test_bid, test_ask, test_src)

        await controller.store_row(test_ticker, test_bid, test_ask, test_src)
        # Only 1 call to add the price to the db
        self.assertEqual(mock_db_session_context.add.call_count, 2)
