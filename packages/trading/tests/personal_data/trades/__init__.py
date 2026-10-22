#  Drakkar-Software OctoBot-Trading
#  Copyright (c) Drakkar-Software, All rights reserved.
#
#  This library is free software; you can redistribute it and/or
#  modify it under the terms of the GNU Lesser General Public
#  License as published by the Free Software Foundation; either
#  version 3.0 of the License, or (at your option) any later version.
#
#  This library is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#  Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public
#  License along with this library.
import octobot_trading.personal_data as personal_data
import octobot_trading.enums


TRADE_SYMBOL = "BTC/USDT"
TRADE_TIME = 1502962946216


def create_raw_exchange_trade(exchange_order_id, exchange_trade_id):
    """
    A trade dict shaped the way one really arrives from an exchange: AbstractAdapter.fix_trades
    pops the ccxt "id" key into EXCHANGE_TRADE_ID, so there is never an ID key on this path and
    the local trade id is generated anew by the trade factory on every parse.
    """
    columns = octobot_trading.enums.ExchangeConstantsOrderColumns
    fee_columns = octobot_trading.enums.FeePropertyColumns
    return {
        columns.EXCHANGE_ID.value: exchange_order_id,
        columns.EXCHANGE_TRADE_ID.value: exchange_trade_id,
        columns.TIMESTAMP.value: TRADE_TIME,
        columns.SYMBOL.value: TRADE_SYMBOL,
        columns.TYPE.value: octobot_trading.enums.TradeOrderType.LIMIT.value,
        columns.SIDE.value: octobot_trading.enums.TradeOrderSide.BUY.value,
        columns.TAKER_OR_MAKER.value:
            octobot_trading.enums.ExchangeConstantsMarketPropertyColumns.TAKER.value,
        columns.PRICE.value: 100.0,
        columns.AMOUNT.value: 1.0,
        columns.COST.value: 100.0,
        columns.FEE.value: {
            fee_columns.COST.value: 0.1,
            fee_columns.CURRENCY.value: "USDT",
            fee_columns.RATE.value: 0.001,
        },
    }


def create_trade(trader, exchange_order_id, is_closing_order, origin_order_id):
    trade = personal_data.Trade(trader)
    trade.exchange_order_id = exchange_order_id
    trade.is_closing_order = is_closing_order
    trade.origin_order_id = origin_order_id
    return trade


def create_executed_trade(trader, side, executed_time, executed_quantity, executed_price, symbol, fee):
    trade = personal_data.Trade(trader)
    trade.executed_time = executed_time
    trade.executed_quantity = executed_quantity
    trade.executed_price = executed_price
    trade.symbol = symbol
    trade.fee = fee
    trade.side = side
    trade.trade_type = octobot_trading.enums.TraderOrderType.BUY_LIMIT
    trade.exchange_trade_type = octobot_trading.enums.TradeOrderType.LIMIT
    return trade
