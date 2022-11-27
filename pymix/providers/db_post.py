import logging

logger = logging.getLogger(__name__)

async def post_price(
        price_info,
        controller
):
    """
    Save the price info for this ticker as a row in the prices db
    :param price_info:
    :param controller:
    :return:
    """
    ticker = price_info["ticker"]
    last_updated = price_info.get("last_updated")
    bid = price_info["bid"]
    ask = price_info["ask"]
    src = price_info["src"]
    await controller.store_row(ticker, bid, ask, src, last_updated=last_updated)

async def update_or_store_price(
        price_info,
        controller
):
    """
    Save the price info for this ticker as a row in the prices db
    :param price_info:
    :param controller:
    :return:
    """
    ticker = price_info["ticker"]
    last_updated = price_info.get("last_updated")
    bid = price_info["bid"]
    ask = price_info["ask"]
    src = price_info["src"]
    await controller.update_or_store_row(ticker, bid, ask, src, last_updated=last_updated)
