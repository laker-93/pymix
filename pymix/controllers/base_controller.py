from abc import abstractmethod, ABC


class BaseController(ABC):
    @abstractmethod
    def get_healthcheck(self) -> dict:
        """
        Gets healthcheck
        :return:
        """

    @abstractmethod
    async def store_row(self, ticker, bid, ask, src):
        """
        Stores the row in a database
        :return:
        """

    @abstractmethod
    async def get_bid_ask(self, ticker):
        """
        Retrieves the latest bid, ask for the ticker
        :return:
        """
