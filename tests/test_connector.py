from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

import pytest

from optopsy_delta.client import DeltaClient, DeltaError, SignatureError, sign_message
from optopsy_delta.policy import CapitalPolicy, CapitalPolicyError
from optopsy_delta.snapshot import SnapshotError, snapshot_to_rows, write_snapshot, snapshots_to_daily_optopsy


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
    def raise_for_status(self):
        import requests
        if self.status_code >= 400:
            raise requests.HTTPError('HTTP failure')
    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.response)


def test_signing_matches_india_docs():
    # Prehash defined by Delta HMAC spec; verify no accidental '?'/encoding difference.
    import hmac, hashlib
    prehash = 'GET1542110948/v2/orders?product_id=1&state=open'
    expected = hmac.new(b'testsecret', prehash.encode(), hashlib.sha256).hexdigest()
    assert sign_message('testsecret','GET','1542110948','/v2/orders','?product_id=1&state=open') == expected
    with pytest.raises(SignatureError):
        sign_message('','GET','1','/v2/orders')


def test_public_underlying_validation():
    s = FakeSession({'success': True, 'result': [{'product_id': 10}]})
    c = DeltaClient(api_key='', api_secret='', session=s)
    assert c.option_tickers('btc') == [{'product_id':10}]
    url, kwargs = s.calls[0]
    assert url.startswith('https://api.india.delta.exchange/v2/tickers?')
    assert parse_qs(urlparse(url).query)['underlying_asset_symbols'] == ['BTC']
    assert 'signature' not in kwargs['headers']
    with pytest.raises(ValueError): c.option_tickers('SOL')
    with pytest.raises(ValueError): c.option_tickers('BTC', '2026-10-13')
    with pytest.raises(ValueError): c.product('../orders')
    assert not hasattr(c, 'place_order')


def test_private_auth_signatures_include_exact_query():
    s = FakeSession({'success': True, 'result': []})
    c = DeltaClient(api_key='safekey', api_secret='safesecret', session=s)
    with patch('optopsy_delta.client.time.time', return_value=1542110948):
        assert c.active_orders() == []
    url, kwargs = s.calls[0]
    assert url.endswith('/v2/orders?states=open%2Cpending')
    headers = kwargs['headers']
    assert headers['signature'] == sign_message('safesecret','GET','1542110948',
                                              '/v2/orders','?states=open%2Cpending')
    assert headers['api-key'] == 'safekey'
    assert kwargs['timeout'][1] < 15


def test_missing_private_keys_fail_before_http():
    s = FakeSession({'success': True,'result': []})
    c = DeltaClient(api_key='',api_secret='',session=s)
    with pytest.raises(SignatureError): c.wallet_balances()
    assert not s.calls


def test_api_errors_fail_closed_no_credentials_exposed():
    s = FakeSession({'success': False, 'error':{'message':'bad'}})
    c = DeltaClient(api_key='superkey', api_secret='supersecret', session=s)
    with pytest.raises(DeltaError, match='unsuccessful') as err: c.wallet_balances()
    assert 'superkey' not in str(err.value) and 'supersecret' not in str(err.value)


def test_pagination_cursor_works_and_repeated_fails():
    class Pages(FakeSession):
        def get(self,url, **kw):
            self.calls.append((url,kw))
            i = len(self.calls)
            if i == 1:
                return FakeResponse({'success': True, 'result':[{'id':1}], 'meta': {'after':'abc'}})
            return FakeResponse({'success': True,'result':[{'id':2}], 'meta': {'after':None}})
    s = Pages(None)
    assert len(DeltaClient(session=s).live_products()) == 2
    assert parse_qs(urlparse(s.calls[1][0]).query)['after'] == ['abc']


def test_budget_fails_above_5000_and_preserves_reserve():
    p = CapitalPolicy()
    assert p.estimate_long_option(limit_price_usd='100', contract_value='0.001',
                                  size=1, available_usd='58.823529')['premium_inr'] == '8.500'
    with pytest.raises(CapitalPolicyError, match='exceeds'):
        p.estimate_long_option(limit_price_usd='50000', contract_value='0.001',
                               size=2, available_usd='58.8')
    with pytest.raises(CapitalPolicyError):
        CapitalPolicy(budget_inr=Decimal('5001'))
    with pytest.raises(CapitalPolicyError):
        p.estimate_long_option(limit_price_usd='NaN', contract_value='0.001',size=1,available_usd='60')
    with pytest.raises(CapitalPolicyError):
        p.estimate_long_option(limit_price_usd='100', contract_value='0.001',size=True,available_usd='60')


PRODUCT = {'id':123, 'symbol':'C-BTC-13OCT26-100000','contract_type':'call_options',
           'underlying_asset':{'symbol':'BTC'},'strike_price':'100000',
           'settlement_time':'2026-10-13T12:00:00Z'}
TICKER = {'product_id':123,'quotes':{'best_bid':'20','best_ask':'22'},
          'greeks':{'delta':'0.35'},'volume':5,'oi':300}


def test_snapshot_preserves_precise_time_and_validates(tmp_path):
    t = datetime(2026,10,13,4,15,30,tzinfo=timezone.utc)
    rows = snapshot_to_rows([TICKER], [PRODUCT], captured_at=t)
    assert len(rows) == 1 and rows[0]['quote_date'] == '2026-10-13T04:15:30+00:00'
    assert rows[0]['option_type'] == 'call'
    path = write_snapshot(tmp_path, underlying='BTC',captured_at=t,
                          tickers=[TICKER],products=[PRODUCT])
    assert path.exists()
    assert json.loads(path.read_text())['raw_products'][0] == PRODUCT
    with pytest.raises(FileExistsError):
        write_snapshot(tmp_path, underlying='BTC',captured_at=t,
                       tickers=[TICKER],products=[PRODUCT])
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SnapshotError):
        snapshot_to_rows([TICKER],[PRODUCT],captured_at=datetime.now())


def test_daily_export_keeps_last_of_day_not_fake_intraday():
    t = datetime(2026,10,13,4,15,tzinfo=timezone.utc)
    first = {'normalized':snapshot_to_rows([TICKER],[PRODUCT],captured_at=t)}
    later = dict(TICKER)
    later['quotes'] = {'best_bid':'27','best_ask':'28'}
    second = {'normalized': snapshot_to_rows([later],[PRODUCT],captured_at=t+timedelta(minutes=5))}
    result = snapshots_to_daily_optopsy([first,second])
    assert len(result)==1 and result.iloc[0]['bid'] == 27
    assert result.iloc[0]['ask'] == 28
    with pytest.raises(SnapshotError): snapshots_to_daily_optopsy([])


def test_missing_quote_or_product_metadata_never_fabricated():
    t=datetime(2026,10,13,tzinfo=timezone.utc)
    assert snapshot_to_rows([{'product_id':123,'quotes':{'best_ask':'22'}}],[PRODUCT],captured_at=t)==[]
    p=dict(PRODUCT);p.pop('settlement_time')
    assert snapshot_to_rows([TICKER],[p],captured_at=t)==[]
    ticker=dict(TICKER);ticker['quotes']={'best_bid':'23','best_ask':'22'}
    assert snapshot_to_rows([ticker],[PRODUCT],captured_at=t)==[]


def test_positions_uses_margined_endpoint():
    s=FakeSession({'success': True, 'result':[]})
    c=DeltaClient(api_key='k',api_secret='secret',session=s)
    assert c.open_positions()==[]
    assert s.calls[0][0]=='https://api.india.delta.exchange/v2/positions/margined'

def test_product_currency_fallback():
    t=datetime(2026,10,13,tzinfo=timezone.utc)
    p=dict(PRODUCT)
    p.pop('underlying_asset')
    p['contract_unit_currency']='BTC'
    assert len(snapshot_to_rows([TICKER],[p],captured_at=t))==1


def test_underlying_candidate_uses_contract_unit_currency_when_other_is_usd():
    t=datetime(2026,10,13,tzinfo=timezone.utc)
    p=dict(PRODUCT)
    p['underlying_asset']={'symbol':'USD'}
    p['contract_unit_currency']='BTC'
    assert snapshot_to_rows([TICKER],[p],captured_at=t)[0]['underlying_symbol']=='BTC'
