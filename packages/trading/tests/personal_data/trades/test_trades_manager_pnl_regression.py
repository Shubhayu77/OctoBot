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
Regression coverage for SCRUM-6 (PNL data lost after restarts), covering the invariant the
deduplication bookkeeping has to hold rather than the symptom.

The symptom tests live in test_trade_manager.py and test_trades_manager_restart_dedup.py: a
re-downloaded exchange fill must not be stored twice, so it can no longer push the history past
MAX_TRADES_COUNT and evict the entry/exit pairs the PNL is computed from.

What is asserted here instead is that the two identity maps TradesManager keeps alongside
self.trades stay in step with it. self.trades is capped, so a leak in that bookkeeping is
invisible until it either eats memory or silently refuses a legitimately re-fetched fill
forever - the failure mode the fix's own risk notes call out as the one that could drift.
"""
import pytest

from tests import event_loop
from tests.exchanges import simulated_exchange_manager, simulated_trader
from tests.personal_data.trades import create_raw_exchange_trade

import octobot_trading.enums as enums
import octobot_trading.personal_data as personal_data


_COLUMNS = enums.ExchangeConstantsOrderColumns


@pytest.fixture
def manager_and_trader(simulated_trader):
    _, _, trader = simulated_trader
    return personal_data.TradesManager(trader), trader


def _restart(manager, trader, refetched_fills):
    """
    One container restart: persist the history the way TradesStorage does (_format_trade stores
    to_dict() and get_history hands it straight back to Trade.from_dict), drop every bit of
    in-memory state the way reload_history does, reload the saved rows, then let the trades
    updater push the same recent fills back in, keyed by exchange order id as the channel does.
    """
    saved_rows = [trade.to_dict() for trade in manager.trades.values()]
    manager._reset_trades()
    for row in saved_rows:
        manager.upsert_trade_instance(personal_data.Trade.from_dict(trader, row))
    for fill in refetched_fills:
        manager.upsert_trade(fill[_COLUMNS.EXCHANGE_ID.value], dict(fill))


def test_identity_bookkeeping_never_outgrows_the_history(manager_and_trader):
    manager, trader = manager_and_trader
    manager.MAX_TRADES_COUNT = 10
    # both identity flavours at once: fills the exchange gives a per-trade id, and fills it does
    # not (whose identity falls back to the exchange order id and cannot be recomputed from the
    # stored trade, since that one only carries a generated local id)
    with_trade_id = [create_raw_exchange_trade(f"order-{i}", f"trade-{i}") for i in range(6)]
    without_trade_id = [create_raw_exchange_trade(f"bare-order-{i}", None) for i in range(6)]

    for _ in range(10):
        _restart(manager, trader, with_trade_id + without_trade_id)
        # no identity is retained for a trade that is no longer in the history
        assert set(manager._trade_identity_by_key) <= set(manager.trades)
        # the two maps are exact inverses of each other
        assert {
            identity: key for key, identity in manager._trade_identity_by_key.items()
        } == manager._trade_key_by_identity
