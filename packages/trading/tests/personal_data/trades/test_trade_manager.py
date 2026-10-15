#  Drakkar-Software OctoBot
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
import pytest

from tests import event_loop
from tests.exchanges import simulated_exchange_manager, simulated_trader
from tests.personal_data.trades import create_trade

import octobot_trading.personal_data as personal_data
import octobot_trading.enums as enums


@pytest.fixture
def trade_manager_and_trader(simulated_trader):
    _, _, trader_instance = simulated_trader
    return personal_data.TradesManager(trader_instance), trader_instance


def test_has_closing_trade_with_exchange_order_id(trade_manager_and_trader):
    trade_manager, trader = trade_manager_and_trader
    assert trade_manager.has_closing_trade_with_exchange_order_id(None) is False
    assert trade_manager.has_closing_trade_with_exchange_order_id("None") is False
    trade = create_trade(trader, "id", False, "None")
    trade.exchange_order_id = "plop"
    trade_manager.trades["id"] = trade
    # trade is not closing order not has the right origin_order_id
    assert trade_manager.has_closing_trade_with_exchange_order_id("id") is False
    # trade does not have the right exchange_order_id
    trade.is_closing_order = True
    assert trade_manager.has_closing_trade_with_exchange_order_id("id2") is False
    assert trade_manager.has_closing_trade_with_exchange_order_id("id") is False
    trade.exchange_order_id = "id"
    # trade is closing this order
    assert trade_manager.has_closing_trade_with_exchange_order_id("id") is True


def test_upsert_trade_is_idempotent_across_history_reload(trade_manager_and_trader):
    trade_manager, trader = trade_manager_and_trader
    # raw trade as fetched from the exchange after fix_trades: no "id" key, identified by its exchange order id
    raw_trade = {
        "info": {},
        "exchange_id": "exch_order_1",
        "exchange_trade_id": "exch_trade_1",
        "timestamp": 1502962946216,
        "datetime": "2017-08-17 12:42:48.000",
        "symbol": "BTC/USDT",
        "type": "limit",
        "side": "buy",
        "takerOrMaker": "taker",
        "price": 0.06917684,
        "amount": 1.5,
        "cost": 0.10376526,
        "fee": {
            "cost": 0.0015,
            "currency": "BTC",
            "rate": 0.002
        }
    }
    assert trade_manager.upsert_trade("exch_order_1", dict(raw_trade)) is True
    assert len(trade_manager.trades) == 1
    # the trade is filed under the id it was inserted with
    assert trade_manager.get_trade("exch_order_1").trade_id == "exch_order_1"

    # simulate a restart: persist the history and reload it the way _load_trades_history does
    persisted_history = [trade.to_dict() for trade in trade_manager.trades.values()]
    reloaded = personal_data.TradesManager(trader)
    for trade_dict in persisted_history:
        reloaded.upsert_trade_instance(personal_data.Trade.from_dict(trader, trade_dict))
    assert len(reloaded.trades) == 1

    # re-fetching the same exchange trade after the restart must not create a duplicate
    assert reloaded.upsert_trade("exch_order_1", dict(raw_trade)) is False
    assert len(reloaded.trades) == 1


def test_get_completed_trades_pnl(trade_manager_and_trader):
    trade_manager, trader = trade_manager_and_trader
    # no trades
    assert trade_manager.get_completed_trades_pnl() == []
    # with trades
    for trade_order_id in (str(i) for i in range(1, 21)):
        trade_manager.trades[trade_order_id] = create_trade(
            trader,
            trade_order_id,
            False,
            trade_order_id,
        )
    # associate first 5 together
    for trade_order_id in range(1, 6):
        trade_manager.get_trade(str(trade_order_id)).associated_entry_ids = [str(trade_order_id + 1)]
    assert len(trade_manager.get_completed_trades_pnl()) == 5
    trade_manager.get_trade("2").associated_entry_ids.append("10")
    assert len(trade_manager.get_completed_trades_pnl()) == 6
    trade_manager.get_trade("6").associated_entry_ids = ["10"]
    assert len(trade_manager.get_completed_trades_pnl()) == 6
    trade_manager.get_trade("4").status = enums.OrderStatus.CANCELED    # will not be counted

    pnls = trade_manager.get_completed_trades_pnl()
    assert len(pnls) == 5
    assert all(isinstance(pnl, personal_data.TradePnl) for pnl in pnls)
    assert all(pnl.entries and pnl.closes for pnl in pnls)

    # with a trades_history argument
    trades = list(trade_manager.trades.values())[:2]
    assert len(trade_manager.get_completed_trades_pnl(trades)) == 1
    trades = list(trade_manager.trades.values())[:4]
    assert len(trade_manager.get_completed_trades_pnl(trades)) == 3

    # with a selected_trades argument
    trades = list(trade_manager.trades.values())[:4]
    assert len(trade_manager.get_completed_trades_pnl(trades, selected_trades=[trades[0]])) == 1

    # using trade_id
    assert trade_manager.get_completed_trade_pnl("1", None).entries == \
           trade_manager.get_completed_trades_pnl(trades, selected_trades=[trades[0]])[0].entries

    # using order_id
    assert trade_manager.get_completed_trade_pnl(None, "3").entries == \
           trade_manager.get_completed_trades_pnl(trades, selected_trades=[trades[2]])[0].entries

    # does not depend on trades_manager trades
    trade_manager.trades.clear()
    assert len(trade_manager.get_completed_trades_pnl(trades)) == 3
