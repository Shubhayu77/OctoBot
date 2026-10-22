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
import mock
import pytest

from tests import event_loop
from tests.exchanges import simulated_exchange_manager, simulated_trader
from tests.personal_data.trades import create_trade

import octobot_trading.personal_data as personal_data
import octobot_trading.enums as enums
import octobot_trading.constants as constants


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


@pytest.mark.asyncio
async def test_upsert_trade_restores_entry_ids_from_stored_order(trade_manager_and_trader):
    trade_manager, trader = trade_manager_and_trader
    # the entry trade as known locally: it kept this OctoBot's local order id
    entry_trade = create_trade(trader, "EXCH-ENTRY", False, "local-entry")
    trade_manager.upsert_trade_instance(entry_trade)

    # the exit filled while OctoBot was down: it comes back from the exchange trades feed carrying
    # exchange ids only, without any associated_entry_ids
    raw_exit_trade = {
        enums.ExchangeConstantsOrderColumns.ID.value: "EXCH-EXIT-ORDER",
        enums.ExchangeConstantsOrderColumns.EXCHANGE_ID.value: "EXCH-EXIT",
        enums.ExchangeConstantsOrderColumns.EXCHANGE_TRADE_ID.value: "EXCH-EXIT-TRADE",
        enums.ExchangeConstantsOrderColumns.TIMESTAMP.value: 1502962946216,
        enums.ExchangeConstantsOrderColumns.SYMBOL.value: "BTC/USDT",
        enums.ExchangeConstantsOrderColumns.TYPE.value: "limit",
        enums.ExchangeConstantsOrderColumns.SIDE.value: "sell",
        enums.ExchangeConstantsOrderColumns.STATUS.value: "closed",
        enums.ExchangeConstantsOrderColumns.PRICE.value: 100,
        enums.ExchangeConstantsOrderColumns.AMOUNT.value: 1,
        enums.ExchangeConstantsOrderColumns.COST.value: 100,
    }
    # the order details stored before the previous shutdown still carry the local identity
    orders_storage = mock.Mock(
        should_store_data=mock.Mock(return_value=True),
        get_startup_order_details=mock.AsyncMock(return_value={
            constants.STORAGE_ORIGIN_VALUE: {
                enums.ExchangeConstantsOrderColumns.ID.value: "local-exit",
                enums.ExchangeConstantsOrderColumns.EXCHANGE_ID.value: "EXCH-EXIT",
            },
            enums.StoredOrdersAttr.ENTRIES.value: ["local-entry"],
        }),
    )
    with mock.patch.object(trader.exchange_manager.storage_manager, "orders_storage", orders_storage):
        assert await trade_manager.upsert_trade("EXCH-EXIT", raw_exit_trade) is True
    orders_storage.get_startup_order_details.assert_awaited_once_with("EXCH-EXIT")

    exit_trade = trade_manager.get_trade("EXCH-EXIT")
    # without the storage restore, those would be the raw exchange values and the exit would be
    # unpairable: associated_entry_ids None and origin_order_id "EXCH-EXIT-ORDER"
    assert exit_trade.associated_entry_ids == ["local-entry"]
    assert exit_trade.origin_order_id == "local-exit"
    assert exit_trade.trade_id == "local-exit"

    # the exit is now paired with its entry
    pnls = trade_manager.get_completed_trades_pnl()
    assert len(pnls) == 1
    assert pnls[0].entries == [entry_trade]
    assert pnls[0].closes == [exit_trade]


@pytest.mark.asyncio
async def test_upsert_trade_without_stored_order_details(trade_manager_and_trader):
    trade_manager, trader = trade_manager_and_trader
    raw_trade = {
        enums.ExchangeConstantsOrderColumns.ID.value: "EXCH-ORDER",
        enums.ExchangeConstantsOrderColumns.EXCHANGE_ID.value: "EXCH-ID",
        enums.ExchangeConstantsOrderColumns.TIMESTAMP.value: 1502962946216,
        enums.ExchangeConstantsOrderColumns.SYMBOL.value: "BTC/USDT",
        enums.ExchangeConstantsOrderColumns.TYPE.value: "limit",
        enums.ExchangeConstantsOrderColumns.SIDE.value: "buy",
        enums.ExchangeConstantsOrderColumns.STATUS.value: "closed",
        enums.ExchangeConstantsOrderColumns.PRICE.value: 100,
        enums.ExchangeConstantsOrderColumns.AMOUNT.value: 1,
        enums.ExchangeConstantsOrderColumns.COST.value: 100,
    }
    # a storage miss leaves the trade untouched and still registers it
    orders_storage = mock.Mock(
        should_store_data=mock.Mock(return_value=True),
        get_startup_order_details=mock.AsyncMock(return_value=None),
    )
    with mock.patch.object(trader.exchange_manager.storage_manager, "orders_storage", orders_storage):
        assert await trade_manager.upsert_trade("EXCH-ID", raw_trade) is True
    trade = trade_manager.get_trade("EXCH-ID")
    assert trade.associated_entry_ids is None
    assert trade.origin_order_id == "EXCH-ORDER"


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
