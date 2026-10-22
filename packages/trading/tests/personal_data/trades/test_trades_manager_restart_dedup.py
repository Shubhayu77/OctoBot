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
"""
Regression tests for SCRUM-6: trades history duplicated re-downloaded exchange fills on every
restart, pushing the history past MAX_TRADES_COUNT until the oldest trades - the entries the
exits point at - were evicted, destroying the PNL pairs.

These tests drive the real production entry points rather than the manager internals:
  - ingestion goes through exchange_personal_data.handle_trade_update, the function the
    trades channel producer calls (personal_data/trades/channel/trades.py keys it by
    ExchangeConstantsOrderColumns.EXCHANGE_ID, i.e. the exchange *order* id)
  - a restart goes through TradesManager.reload_history, which resets and reloads from
    the trades storage
"""
import decimal

import pytest

from tests import event_loop
from tests.exchanges import simulated_exchange_manager, simulated_trader
from tests.personal_data.trades import TRADE_SYMBOL, TRADE_TIME, create_raw_exchange_trade

import octobot_trading.enums as enums
import octobot_trading.personal_data as personal_data


_COLUMNS = enums.ExchangeConstantsOrderColumns


def _octobot_trade(trader, order_id, associated_entry_ids=None):
    """A trade created by OctoBot itself when one of its own orders filled."""
    order = personal_data.create_order_instance(
        trader,
        order_type=enums.TraderOrderType.BUY_LIMIT,
        symbol=TRADE_SYMBOL,
        current_price=decimal.Decimal("100"),
        quantity=decimal.Decimal("1"),
        price=decimal.Decimal("100"),
        order_id=order_id,
        exchange_order_id=f"exchange-{order_id}",
        filled_price=decimal.Decimal("100"),
        quantity_filled=decimal.Decimal("1"),
        total_cost=decimal.Decimal("100"),
        timestamp=TRADE_TIME,
    )
    order.creation_time = TRADE_TIME
    order.executed_time = TRADE_TIME
    order.taker_or_maker = enums.ExchangeConstantsMarketPropertyColumns.TAKER.value
    order.fee = {
        enums.FeePropertyColumns.COST.value: decimal.Decimal("0.1"),
        enums.FeePropertyColumns.CURRENCY.value: "USDT",
    }
    order.associated_entry_ids = associated_entry_ids
    order.status = enums.OrderStatus.FILLED
    return personal_data.create_trade_from_order(order, executed_time=TRADE_TIME)


class _TradesDb:
    """
    In-memory stand-in for TradesStorage: holds the rows that survive a restart.
    Mirrors TradesStorage.store_history, which replaces the whole table with the
    current content of the manager.
    """

    def __init__(self):
        self.rows = []
        self.trades_manager = None

    async def get_history(self):
        return [dict(row) for row in self.rows]

    async def store_history(self):
        self.rows = [trade.to_dict() for trade in self.trades_manager.trades.values()]

    async def trigger_debounced_update_auth_data(self, *_, **__):
        pass

    async def stop(self):
        pass


@pytest.fixture
def trades_db(simulated_trader):
    _, exchange_manager, _ = simulated_trader
    db = _TradesDb()
    db.trades_manager = exchange_manager.exchange_personal_data.trades_manager
    exchange_manager.storage_manager.trades_storage = db
    return db


async def _ingest_exchange_fills(exchange_manager, fills):
    """Replay what TradesProducer.perform does for every re-downloaded fill."""
    results = []
    for fill in fills:
        results.append(
            await exchange_manager.exchange_personal_data.handle_trade_update(
                TRADE_SYMBOL,
                fill[_COLUMNS.EXCHANGE_ID.value],
                dict(fill),
                is_old_trade=True,
                should_notify=False,
            )
        )
    return results


async def _restart(exchange_manager, trades_db, fills):
    """
    One container restart: persist what we have, drop all in-memory state, reload the saved
    history from storage, then let the trades updater re-download the same recent fills.
    """
    await trades_db.store_history()
    await exchange_manager.exchange_personal_data.trades_manager.reload_history(False)
    await _ingest_exchange_fills(exchange_manager, fills)


@pytest.mark.asyncio
async def test_refetched_exchange_fills_do_not_accumulate_across_restarts(
    simulated_trader, trades_db
):
    _, exchange_manager, _ = simulated_trader
    trades_manager = exchange_manager.exchange_personal_data.trades_manager
    fills = [create_raw_exchange_trade(f"order-{i}", f"trade-{i}") for i in range(4)]

    assert await _ingest_exchange_fills(exchange_manager, fills) == [True] * len(fills)
    assert len(trades_manager.trades) == len(fills)

    for _ in range(6):
        await _restart(exchange_manager, trades_db, fills)
        # the same 4 exchange fills, no matter how many times they are reloaded from storage
        # and re-downloaded from the exchange
        assert len(trades_manager.trades) == len(fills)

    await trades_db.store_history()
    assert len(trades_db.rows) == len(fills)
    assert {row[_COLUMNS.EXCHANGE_TRADE_ID.value] for row in trades_db.rows} == {
        f"trade-{i}" for i in range(4)
    }


@pytest.mark.asyncio
async def test_pnl_pairs_survive_restarts_that_would_overflow_the_history(
    simulated_trader, trades_db
):
    _, exchange_manager, _ = simulated_trader
    trades_manager = exchange_manager.exchange_personal_data.trades_manager
    # keep the ceiling low so overflow is reachable within the test
    trades_manager.MAX_TRADES_COUNT = 20

    entry = _octobot_trade(exchange_manager.trader, "entry-1")
    close = _octobot_trade(exchange_manager.trader, "exit-1", associated_entry_ids=["entry-1"])
    assert trades_manager.upsert_trade_instance(entry) is True
    assert trades_manager.upsert_trade_instance(close) is True
    assert len(trades_manager.get_completed_trades_pnl()) == 1

    fills = [create_raw_exchange_trade(f"order-{i}", f"trade-{i}") for i in range(8)]
    await _ingest_exchange_fills(exchange_manager, fills)

    for _ in range(15):
        await _restart(exchange_manager, trades_db, fills)

    # the entry/exit pair is the oldest content of the history: it is the first thing
    # _remove_oldest_trades drops once duplicated fills push past MAX_TRADES_COUNT
    assert len(trades_manager.get_completed_trades_pnl()) == 1
    assert len(trades_manager.trades) == len(fills) + 2


@pytest.mark.asyncio
async def test_resent_identical_fill_is_not_reported_as_a_change(simulated_trader, trades_db):
    _, exchange_manager, _ = simulated_trader
    trades_manager = exchange_manager.exchange_personal_data.trades_manager
    fill = create_raw_exchange_trade("order-1", "trade-1")

    assert await _ingest_exchange_fills(exchange_manager, [fill]) == [True]
    # the exchange re-sends the very same fill: nothing changed
    assert await _ingest_exchange_fills(exchange_manager, [fill]) == [False]
    assert len(trades_manager.trades) == 1


@pytest.mark.asyncio
async def test_partial_fills_of_one_order_are_kept_apart(simulated_trader, trades_db):
    _, exchange_manager, _ = simulated_trader
    trades_manager = exchange_manager.exchange_personal_data.trades_manager
    # one order matched against two counterparties: same exchange order id, two trade ids
    fills = [
        create_raw_exchange_trade("order-1", "trade-a"),
        create_raw_exchange_trade("order-1", "trade-b"),
    ]

    await _ingest_exchange_fills(exchange_manager, fills)
    for _ in range(3):
        await _restart(exchange_manager, trades_db, fills)

    # deduplication must key on the fill, not on the order: collapsing these loses volume
    assert len({trade.exchange_trade_id for trade in trades_manager.trades.values()}) == 2


@pytest.mark.asyncio
async def test_evicted_fill_without_exchange_trade_id_can_be_stored_again(
    simulated_trader, trades_db
):
    """
    Some exchanges return no per-trade id. The identity then falls back to the exchange order
    id, so eviction must forget *that* key - otherwise the fill is refused forever and the
    identity set grows without bound while self.trades stays capped.
    """
    _, exchange_manager, _ = simulated_trader
    trades_manager = exchange_manager.exchange_personal_data.trades_manager
    fill = create_raw_exchange_trade("order-1", None)

    assert await _ingest_exchange_fills(exchange_manager, [fill]) == [True]
    assert len(trades_manager.trades) == 1

    trades_manager._remove_oldest_trades(1)
    assert len(trades_manager.trades) == 0

    # an evicted fill is gone from the history and must be storable again when re-downloaded
    assert await _ingest_exchange_fills(exchange_manager, [fill]) == [True]
    assert len(trades_manager.trades) == 1
