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
import copy
import functools
import mock
import pytest
import pytest_asyncio
import decimal

import octobot_trading.enums as enums
import octobot_trading.personal_data as personal_data
import octobot_trading.personal_data.orders.orders_storage_operations as orders_storage_operations
import octobot_trading.storage.orders_storage as orders_storage
import octobot_trading.constants as constants

from tests import event_loop
from tests.exchanges import exchange_manager, simulated_exchange_manager
from tests.exchanges.traders import trader_simulator
from tests.exchanges.traders import trader

# All test coroutines will be treated as marked.
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
def initialized_mocked_order_storage(trader_simulator):
    config, exchange_manager_inst, trader_inst = trader_simulator
    mocked_order_storage = mock.Mock(
        stop=mock.AsyncMock(),
    )
    exchange_manager_inst.storage_manager.orders_storage = mocked_order_storage
    yield mocked_order_storage, exchange_manager_inst, trader_inst


GROUP_ID = "oco-1"
STOP_ORDER_ID = "stop-order-1"
TAKE_PROFIT_ORDER_ID = "take-profit-order-1"


def _build_grouped_virtual_order_document(
    exchange_manager, trader_inst, order_group, order_id, order_class, order_type, self_managed, is_active
):
    order = order_class(trader_inst)
    order.update(
        order_type=order_type,
        symbol="BTC/USDT",
        current_price=decimal.Decimal("70"),
        quantity=decimal.Decimal("10"),
        price=decimal.Decimal("70"),
        order_id=order_id,
        group=order_group,
    )
    document = orders_storage._format_order(order, exchange_manager)
    origin = document[constants.STORAGE_ORIGIN_VALUE]
    # force virtual order attributes: on a SPOT exchange, a DailyTradingMode stop loss is self-managed and
    # its OCO take profit is inactive, therefore neither of them is fetched from exchange on startup
    origin[enums.ExchangeConstantsOrderColumns.SELF_MANAGED.value] = self_managed
    origin[enums.ExchangeConstantsOrderColumns.IS_ACTIVE.value] = is_active
    # self managed and inactive orders have no exchange order id
    origin[enums.ExchangeConstantsOrderColumns.EXCHANGE_ID.value] = None
    return document


def _build_oco_group_virtual_order_documents(exchange_manager, trader_inst):
    """
    :return: the stored documents of a self-managed stop loss and its inactive take profit, both within the
    same OneCancelsTheOtherOrderGroup. No order of this group is fetched from exchange on startup.
    """
    order_group = exchange_manager.exchange_personal_data.orders_manager.create_group(
        personal_data.OneCancelsTheOtherOrderGroup,
        group_name=GROUP_ID,
        active_order_swap_strategy=personal_data.StopFirstActiveOrderSwapStrategy(123),
    )
    stop_document = _build_grouped_virtual_order_document(
        exchange_manager, trader_inst, order_group, STOP_ORDER_ID,
        personal_data.StopLossOrder, enums.TraderOrderType.STOP_LOSS,
        self_managed=True, is_active=True,
    )
    take_profit_document = _build_grouped_virtual_order_document(
        exchange_manager, trader_inst, order_group, TAKE_PROFIT_ORDER_ID,
        personal_data.SellLimitOrder, enums.TraderOrderType.SELL_LIMIT,
        self_managed=False, is_active=False,
    )
    # simulate a restart: the group only exists in storage from now on
    exchange_manager.exchange_personal_data.orders_manager.order_groups.clear()
    return stop_document, take_profit_document


def _mock_virtual_startup_orders(mocked_order_storage, documents):
    mocked_order_storage.get_all_virtual_startup_orders = mock.Mock(return_value=documents)
    mocked_order_storage.get_startup_virtual_orders_details_from_group = mock.Mock(
        side_effect=lambda group_id: [
            document
            for document in documents
            if document.get(enums.StoredOrdersAttr.GROUP.value, {}).get(
                enums.StoredOrdersAttr.GROUP_ID.value, None
            ) == group_id
        ]
    )


async def test_create_required_virtual_orders_restores_grouped_virtual_orders(initialized_mocked_order_storage):
    mocked_order_storage, exchange_manager_inst, trader_inst = initialized_mocked_order_storage
    orders_manager = exchange_manager_inst.exchange_personal_data.orders_manager
    documents = _build_oco_group_virtual_order_documents(exchange_manager_inst, trader_inst)
    _mock_virtual_startup_orders(mocked_order_storage, list(documents))

    pending_groups = {}
    await orders_storage_operations.create_required_virtual_orders(pending_groups, exchange_manager_inst)
    # the order group is identified from stored data alone: no exchange order refers to it
    assert list(pending_groups) == [GROUP_ID]
    await personal_data.create_missing_virtual_orders_from_storage_order_groups(
        pending_groups, exchange_manager_inst
    )

    # both legs of the group are restored
    assert orders_manager.has_order(STOP_ORDER_ID)
    assert orders_manager.has_order(TAKE_PROFIT_ORDER_ID)
    restored_stop = orders_manager.get_order(STOP_ORDER_ID)
    restored_take_profit = orders_manager.get_order(TAKE_PROFIT_ORDER_ID)
    # both legs share the same restored group instance
    assert isinstance(restored_stop.order_group, personal_data.OneCancelsTheOtherOrderGroup)
    assert restored_stop.order_group is restored_take_profit.order_group
    assert restored_stop.order_group is orders_manager.order_groups[GROUP_ID]
    # the inactive leg is still inactive: it must not become a live exchange order
    assert restored_take_profit.is_active is False
    assert restored_stop.is_active is True


async def test_create_required_virtual_orders_does_not_duplicate_already_restored_group_orders(
    initialized_mocked_order_storage
):
    mocked_order_storage, exchange_manager_inst, trader_inst = initialized_mocked_order_storage
    orders_manager = exchange_manager_inst.exchange_personal_data.orders_manager
    stop_document, take_profit_document = _build_oco_group_virtual_order_documents(
        exchange_manager_inst, trader_inst
    )
    _mock_virtual_startup_orders(mocked_order_storage, [stop_document, take_profit_document])

    # the stop loss has already been restored (ex: by a previous restore pass)
    already_restored_stop = personal_data.create_order_from_dict(
        trader_inst, stop_document[constants.STORAGE_ORIGIN_VALUE]
    )
    #  can't use ._add_order() as it is cythonized as private
    orders_manager.orders[already_restored_stop.order_id] = already_restored_stop
    assert len(orders_manager.orders) == 1

    pending_groups = {}
    real_create_order_from_storage_data = orders_storage_operations.create_order_from_storage_data
    with mock.patch.object(
        orders_storage_operations, "create_order_from_storage_data",
        mock.AsyncMock(side_effect=real_create_order_from_storage_data)
    ) as create_order_from_storage_data_mock:
        await orders_storage_operations.create_required_virtual_orders(pending_groups, exchange_manager_inst)
        assert list(pending_groups) == [GROUP_ID]
        await personal_data.create_missing_virtual_orders_from_storage_order_groups(
            pending_groups, exchange_manager_inst
        )
        # the already known stop loss is skipped before any Order is built from it: only the missing
        # take profit is created
        created_order_ids = [
            call.args[0][constants.STORAGE_ORIGIN_VALUE][enums.ExchangeConstantsOrderColumns.ID.value]
            for call in create_order_from_storage_data_mock.call_args_list
        ]
        assert created_order_ids == [TAKE_PROFIT_ORDER_ID]

    # the already restored stop loss instance is left untouched and only one order has been added
    assert len(orders_manager.orders) == 2
    assert orders_manager.get_order(STOP_ORDER_ID) is already_restored_stop
    assert orders_manager.has_order(TAKE_PROFIT_ORDER_ID)


def _as_real_startup_orders_storage(mocked_order_storage, documents):
    """
    Bind the real OrdersStorage virtual orders selectors to mocked_order_storage so that the
    real storage filtering logic is used, from documents as they are read back from database.
    """
    mocked_order_storage.startup_orders = {
        document[constants.STORAGE_ORIGIN_VALUE][enums.ExchangeConstantsOrderColumns.ID.value]:
            orders_storage.from_order_document(copy.deepcopy(document))
        for document in documents
    }
    mocked_order_storage.get_all_virtual_startup_orders = functools.partial(
        orders_storage.OrdersStorage.get_all_virtual_startup_orders, mocked_order_storage
    )
    mocked_order_storage.get_startup_virtual_orders_details_from_group = functools.partial(
        orders_storage.OrdersStorage.get_startup_virtual_orders_details_from_group, mocked_order_storage
    )


async def test_restore_required_virtual_orders_restores_fully_virtual_group_on_startup(
    initialized_mocked_order_storage
):
    """
    SCRUM-5 regression: after a restart, a DailyTradingMode spot stop loss / take profit pair is
    stored as a fully virtual OCO group (self-managed stop + inactive take profit) and no fetched
    exchange order refers to that group. The startup restore must still rebuild both legs.
    This drives the actual startup entry point: OrdersProducer._restore_required_virtual_orders.
    """
    mocked_order_storage, exchange_manager_inst, trader_inst = initialized_mocked_order_storage
    orders_manager = exchange_manager_inst.exchange_personal_data.orders_manager
    documents = _build_oco_group_virtual_order_documents(exchange_manager_inst, trader_inst)
    _as_real_startup_orders_storage(mocked_order_storage, documents)
    # nothing is restored yet and the group is gone: this is a fresh start
    assert not orders_manager.orders
    assert not orders_manager.order_groups

    channel = mock.Mock(exchange_manager=exchange_manager_inst)
    producer = personal_data.OrdersProducer(channel)
    await producer._restore_required_virtual_orders()

    # both legs of the fully virtual group are restored
    assert sorted(orders_manager.orders) == sorted([STOP_ORDER_ID, TAKE_PROFIT_ORDER_ID])
    restored_stop = orders_manager.get_order(STOP_ORDER_ID)
    restored_take_profit = orders_manager.get_order(TAKE_PROFIT_ORDER_ID)
    # both legs are bound to the same restored group instance, otherwise the OCO link is broken
    assert isinstance(restored_stop.order_group, personal_data.OneCancelsTheOtherOrderGroup)
    assert restored_stop.order_group is restored_take_profit.order_group
    assert restored_stop.order_group is orders_manager.order_groups[GROUP_ID]
    # the inactive leg must not be turned into a live exchange order
    assert restored_take_profit.is_active is False
    assert restored_stop.is_active is True
    assert restored_stop.is_self_managed() is True


async def test_restore_required_virtual_orders_is_idempotent(initialized_mocked_order_storage):
    """
    SCRUM-5 regression: the startup restore now also runs on the pending groups it collects itself,
    it must therefore not duplicate orders that are already known to orders_manager.
    """
    mocked_order_storage, exchange_manager_inst, trader_inst = initialized_mocked_order_storage
    orders_manager = exchange_manager_inst.exchange_personal_data.orders_manager
    documents = _build_oco_group_virtual_order_documents(exchange_manager_inst, trader_inst)
    _as_real_startup_orders_storage(mocked_order_storage, documents)

    channel = mock.Mock(exchange_manager=exchange_manager_inst)
    producer = personal_data.OrdersProducer(channel)
    await producer._restore_required_virtual_orders()
    assert sorted(orders_manager.orders) == sorted([STOP_ORDER_ID, TAKE_PROFIT_ORDER_ID])
    first_restored_orders = dict(orders_manager.orders)

    # a 2nd restore pass does not create any additional or replacement order
    await producer._restore_required_virtual_orders()
    assert orders_manager.orders == first_restored_orders
    assert orders_manager.get_order(STOP_ORDER_ID) is first_restored_orders[STOP_ORDER_ID]
    assert orders_manager.get_order(TAKE_PROFIT_ORDER_ID) is first_restored_orders[TAKE_PROFIT_ORDER_ID]


async def test_apply_order_storage_details_if_any(initialized_mocked_order_storage):
    mocked_order_storage, exchange_manager_inst, trader_inst = initialized_mocked_order_storage
    mocked_order_storage.get_startup_order_details = mock.AsyncMock(return_value={})
    mocked_order_storage.should_store_data = mock.Mock(return_value=False)

    order = personal_data.BuyLimitOrder(trader_inst)
    order.update(order_type=enums.TraderOrderType.BUY_LIMIT,
                 symbol="BTC/USDT",
                 current_price=decimal.Decimal("70"),
                 quantity=decimal.Decimal("10"),
                 price=decimal.Decimal("70"),
                 exchange_order_id="plop exchange_id")
    await personal_data.apply_order_storage_details_if_any(order, exchange_manager_inst, {})
    # disabled in trader simulator
    mocked_order_storage.get_startup_order_details.assert_not_awaited()

    mocked_order_storage.should_store_data = mock.Mock(return_value=True)
    await personal_data.apply_order_storage_details_if_any(order, exchange_manager_inst, {})
    mocked_order_storage.get_startup_order_details.assert_awaited_once_with("plop exchange_id")

    # ensure no crash with not well formatted order_details
    mocked_order_storage.get_startup_order_details = mock.AsyncMock(return_value={"hello": "hi there"})
    await personal_data.apply_order_storage_details_if_any(order, exchange_manager_inst, {})
    mocked_order_storage.get_startup_order_details.assert_awaited_once_with("plop exchange_id")

    # ensure order update is done
    assert order.order_id != "new id 123"
    assert order.exchange_order_id != "new exchange id 123"
    mocked_order_storage.get_startup_order_details = mock.AsyncMock(return_value={
        constants.STORAGE_ORIGIN_VALUE: {
            enums.ExchangeConstantsOrderColumns.ID.value: "new id 123",
            enums.ExchangeConstantsOrderColumns.EXCHANGE_ID.value: "new exchange id 123"
        }
    })
    await personal_data.apply_order_storage_details_if_any(order, exchange_manager_inst, {})
    mocked_order_storage.get_startup_order_details.assert_awaited_once_with("plop exchange_id")
    assert order.order_id == "new id 123"
    assert order.exchange_order_id == "new exchange id 123"
