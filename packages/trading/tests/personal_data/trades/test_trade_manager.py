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
import decimal

import pytest

from tests import event_loop
from tests.exchanges import simulated_exchange_manager, simulated_trader
from tests.personal_data.trades import create_trade

import octobot_trading.personal_data as personal_data
import octobot_trading.enums as enums


_ORDER_COLUMNS = enums.ExchangeConstantsOrderColumns
_TRADE_TIME = 1502962946216


@pytest.fixture
def trade_manager_and_trader(simulated_trader):
    _, _, trader_instance = simulated_trader
    return personal_data.TradesManager(trader_instance), trader_instance


def _raw_exchange_trade(exchange_order_id, exchange_trade_id):
    """
    A trade dict shaped like the ones coming from the exchange: fix_trades drops the "id" key,
    so the local trade id is generated anew on each parse and only the exchange ids are stable.
    """
    return {
        _ORDER_COLUMNS.EXCHANGE_ID.value: exchange_order_id,
        _ORDER_COLUMNS.EXCHANGE_TRADE_ID.value: exchange_trade_id,
        _ORDER_COLUMNS.TIMESTAMP.value: _TRADE_TIME,
        _ORDER_COLUMNS.SYMBOL.value: "BTC/USDT",
        _ORDER_COLUMNS.TYPE.value: "limit",
        _ORDER_COLUMNS.SIDE.value: "buy",
        _ORDER_COLUMNS.TAKER_OR_MAKER.value: "taker",
        _ORDER_COLUMNS.PRICE.value: 100.0,
        _ORDER_COLUMNS.AMOUNT.value: 1.0,
        _ORDER_COLUMNS.COST.value: 100.0,
        _ORDER_COLUMNS.FEE.value: {
            enums.FeePropertyColumns.COST.value: 0.1,
            enums.FeePropertyColumns.CURRENCY.value: "USDT",
            enums.FeePropertyColumns.RATE.value: 0.001,
        },
    }


def _filled_trade(trader, origin_order_id, exchange_trade_id, associated_entry_ids=None):
    trade = personal_data.Trade(trader)
    trade.trade_id = origin_order_id
    trade.origin_order_id = origin_order_id
    trade.exchange_order_id = f"exchange-{origin_order_id}"
    trade.exchange_trade_id = exchange_trade_id
    trade.symbol = "BTC/USDT"
    trade.currency = "BTC"
    trade.market = "USDT"
    trade.quantity_currency = "BTC"
    trade.status = enums.OrderStatus.FILLED
    trade.side = enums.TradeOrderSide.BUY
    trade.trade_type = enums.TraderOrderType.BUY_LIMIT
    trade.exchange_trade_type = enums.TradeOrderType.LIMIT
    trade.taker_or_maker = "taker"
    trade.creation_time = _TRADE_TIME
    trade.executed_time = _TRADE_TIME
    trade.executed_quantity = decimal.Decimal("1")
    trade.executed_price = decimal.Decimal("100")
    trade.total_cost = decimal.Decimal("100")
    trade.fee = {
        enums.FeePropertyColumns.COST.value: decimal.Decimal("0.1"),
        enums.FeePropertyColumns.CURRENCY.value: "USDT",
    }
    trade.associated_entry_ids = associated_entry_ids
    return trade


def _replay_restart(trade_manager, trader, raw_trades):
    """
    Replay one container restart: the saved history is reloaded from storage, then the same
    fills are re-downloaded from the exchange.
    """
    stored_history = [trade.to_dict() for trade in trade_manager.trades.values()]
    trade_manager._reset_trades()
    for trade_dict in stored_history:
        trade_manager.upsert_trade_instance(personal_data.Trade.from_dict(trader, trade_dict))
    for raw_trade in raw_trades:
        trade_manager.upsert_trade(raw_trade[_ORDER_COLUMNS.EXCHANGE_ID.value], raw_trade)


def test_refetched_exchange_trade_is_not_duplicated_after_reload(trade_manager_and_trader):
    trade_manager, trader = trade_manager_and_trader
    raw_trade = _raw_exchange_trade("exch-order-1", "exch-trade-1")

    assert trade_manager.upsert_trade("exch-order-1", raw_trade) is True
    assert len(trade_manager.trades) == 1

    # the reloaded trade is keyed by its generated local id while the re-fetched twin is looked
    # up by exchange order id: both are the same exchange fill and must be stored only once
    _replay_restart(trade_manager, trader, [raw_trade])
    assert len(trade_manager.trades) == 1


def test_refetched_exchange_trade_upsert_reports_no_change(trade_manager_and_trader):
    trade_manager, _ = trade_manager_and_trader
    raw_trade = _raw_exchange_trade("exch-order-1", "exch-trade-1")

    assert trade_manager.upsert_trade("exch-order-1", raw_trade) is True
    # same fill offered again: nothing changed, so no update is reported
    assert trade_manager.upsert_trade("exch-order-1", raw_trade) is False
    assert len(trade_manager.trades) == 1


def test_evicted_trade_can_be_stored_again(trade_manager_and_trader):
    trade_manager, _ = trade_manager_and_trader
    trade_manager.MAX_TRADES_COUNT = 10
    raw_trade = _raw_exchange_trade("exch-order-1", "exch-trade-1")
    assert trade_manager.upsert_trade("exch-order-1", raw_trade) is True

    # evicting a trade must also forget its identity, otherwise it could never be re-fetched
    trade_manager._remove_oldest_trades(1)
    assert len(trade_manager.trades) == 0
    assert trade_manager.upsert_trade("exch-order-1", raw_trade) is True
    assert len(trade_manager.trades) == 1


def test_evicted_trade_without_exchange_trade_id_can_be_stored_again(trade_manager_and_trader):
    trade_manager, _ = trade_manager_and_trader
    trade_manager.MAX_TRADES_COUNT = 10
    # some exchanges return no per-trade id: the identity then falls back to the exchange order
    # id, which the parsed trade does not carry (it gets a generated local id instead). The
    # identity has to be forgotten as it was registered and never recomputed from the stored
    # trade, otherwise an evicted fill would be refused forever and identities would pile up.
    raw_trade = _raw_exchange_trade("exch-order-1", None)
    assert trade_manager.upsert_trade("exch-order-1", raw_trade) is True

    trade_manager._remove_oldest_trades(1)
    assert len(trade_manager.trades) == 0
    # no identity is kept around for a trade that is no longer in the history
    assert trade_manager._trade_identity_by_key == {}
    assert trade_manager._trade_key_by_identity == {}

    assert trade_manager.upsert_trade("exch-order-1", raw_trade) is True
    assert len(trade_manager.trades) == 1


def test_upsert_trade_instance_does_not_serialize_the_trade(trade_manager_and_trader):
    trade_manager, trader = trade_manager_and_trader
    # identities are read from the trade id attributes: serializing every reloaded trade just to
    # derive a key is wasteful on a full history and raises on a partially initialized trade
    trade = _filled_trade(trader, "entry-order-1", "entry-trade-1")
    trade.exchange_trade_type = None
    assert trade_manager.upsert_trade_instance(trade) is True
    assert len(trade_manager.trades) == 1
    # the very same trade offered again is still recognized
    assert trade_manager.upsert_trade_instance(trade) is False
    assert len(trade_manager.trades) == 1


def test_pnl_survives_repeated_restarts(trade_manager_and_trader):
    trade_manager, trader = trade_manager_and_trader
    trade_manager.MAX_TRADES_COUNT = 10
    entry = _filled_trade(trader, "entry-order-1", "entry-trade-1")
    closing = _filled_trade(
        trader, "exit-order-1", "exit-trade-1", associated_entry_ids=["entry-order-1"]
    )
    assert trade_manager.upsert_trade_instance(entry) is True
    assert trade_manager.upsert_trade_instance(closing) is True
    assert len(trade_manager.get_completed_trades_pnl()) == 1

    raw_trades = [
        _raw_exchange_trade(f"exch-order-{i}", f"exch-trade-{i}")
        for i in range(5)
    ]
    for _ in range(20):
        _replay_restart(trade_manager, trader, raw_trades)

    # without deduplication, re-fetched copies grow past MAX_TRADES_COUNT on every restart and
    # evict the oldest trades, which are the entry/exit pair the pnl is computed from
    assert len(trade_manager.get_completed_trades_pnl()) == 1
    assert len(trade_manager.trades) == len(raw_trades) + 2


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
