"""Read-only Delta fill pagination and strictly verified order accounting."""
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import pytest

from optopsy_delta.client import DeltaClient, DeltaError
from optopsy_delta.reconcile import ReconciliationError, order_fill_status


class FakeReply:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class Session:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeReply(self.responses.pop(0))


FILL_1 = {
    "id": "fill-1", "order_id": "123", "product_id": 44,
    "size": 1, "side": "buy", "price": "23.5",
}


def test_authenticated_fill_pagination_exact_query_and_product():
    http = Session([
        {"success": True, "result": [FILL_1], "meta": {"after": "next"}},
        {"success": True, "result": [{
            **FILL_1, "id": "fill-2"
        }], "meta": {"after": None}},
    ])
    client = DeltaClient(api_key="readkey", api_secret="readsecret", session=http)
    fills = client.fills_for_product(44, start_time_us=1000000, end_time_us=2000000)
    assert len(fills) == 2
    first, first_kwargs = http.calls[0]
    params = parse_qs(urlparse(first).query)
    assert first.startswith("https://api.india.delta.exchange/v2/fills?")
    assert params["product_ids"] == ["44"]
    assert params["start_time"] == ["1000000"]
    assert params["end_time"] == ["2000000"]
    assert "signature" in first_kwargs["headers"]
    assert "readsecret" not in repr(http.calls)
    assert parse_qs(urlparse(http.calls[1][0]).query)["after"] == ["next"]


def test_fills_fail_closed_on_duplicate_cursor_and_truncation():
    page = {"success": True, "result": [FILL_1], "meta": {"after": "next"}}
    a = DeltaClient(api_key="k", api_secret="s", session=Session([page, page]))
    with pytest.raises(DeltaError, match="Repeated"):
        a.fills_for_product(44, start_time_us=1, end_time_us=2)
    b = DeltaClient(api_key="k", api_secret="s", session=Session([page]))
    with pytest.raises(DeltaError, match="Incomplete"):
        b.fills_for_product(44, start_time_us=1, end_time_us=2, max_pages=1)


def test_fills_reject_wrong_product_duplicate_or_missing_meta():
    for payload in [
        {"success": True, "result": [{**FILL_1, "product_id": 45}], "meta": {}},
        {"success": True, "result": [FILL_1, FILL_1], "meta": {}},
        {"success": True, "result": [FILL_1]},
    ]:
        cli = DeltaClient(api_key="k", api_secret="s", session=Session([payload]))
        with pytest.raises(DeltaError):
            cli.fills_for_product(44, start_time_us=1, end_time_us=2)


def test_product_position_uses_realtime_endpoint():
    http = Session([{"success": True, "result": {"size": 2, "entry_price": "23"}}])
    pos = DeltaClient(api_key="k", api_secret="s", session=http).product_position(44)
    assert pos["size"] == 2
    assert parse_qs(urlparse(http.calls[0][0]).query)["product_id"] == ["44"]
    with pytest.raises(ValueError):
        DeltaClient().product_position(True)


def test_order_fill_match_never_claims_ownership_from_just_one_order():
    order = {"id": 123, "product_id": 44, "size": 2, "unfilled_size": 0,
             "side": "buy", "state": "closed"}
    result = order_fill_status(order, [FILL_1, {**FILL_1, "id": "fill-2"}],
                               {"size": 2})
    assert result["order_filled_quantity"] == 2
    assert result["position_size"] == "2"
    assert result["position_ownership_verified"] is False
    assert result["eligible_for_automatic_entry_or_exit"] is False


def test_order_fill_mismatches_rejected():
    order = {"id": 123, "product_id": 44, "size": 2, "unfilled_size": 1,
             "side": "buy", "state": "cancelled"}
    invalid = [
        ([FILL_1, {**FILL_1, "id": "fill-2"}], {"size": 1}),
        ([{**FILL_1, "order_id": "456"}], {"size": 1}),
        ([{**FILL_1, "side": "sell"}], {"size": 1}),
        ([{**FILL_1, "size": -1}], {"size": 1}),
        ([FILL_1], {"size": "NaN"}),
    ]
    for fills, pos in invalid:
        with pytest.raises(ReconciliationError):
            order_fill_status(order, fills, pos)


def test_broker_funds_zero_wont_produce_fake_fill():
    from optopsy_delta.live_gateway import OrderIntent, validate, LiveRejected

    intent = OrderIntent(signal_id="no-money", product_id=44, side="buy",
                         size=1, limit_price="20", contract_value="0.01",
                         usd_inr="85", ticker_timestamp=1000)
    with pytest.raises(LiveRejected, match="Insufficient funds"):
        validate(intent, now=1001, cash_inr=Decimal(0), tick_size="0.5",
                 observed_bid="19", observed_ask="20",
                 observed_depth="2", external_exposure=False)
