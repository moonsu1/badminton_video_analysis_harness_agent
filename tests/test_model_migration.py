import json

import httpx
import pytest

from badminton import agent
from badminton.pricing import prices,reservation_cost,usage_cost
from badminton.review_store import Reviews
from badminton.store import encode
from tests.test_match_review import setup_review


def test_luna_counts_cache_reads_and_both_write_lifetimes_once():
    usage={'input_tokens':12000,'input_tokens_details':{'cached_tokens':4000,'cache_write_tokens':7000,
        'cache_write_tokens_details':{'ttl_30m_tokens':3000,'ttl_12h_tokens':4000}},'output_tokens':1000}
    assert usage_cost(usage)==2015  # 100 + 40 + 375 + 1000 + 500 microUSD.
    assert reservation_cost(12000,1000)>usage_cost({'input_tokens':12000,'output_tokens':1000,
        'input_tokens_details':{'cache_write_tokens':12000}})


def test_long_context_pricing_and_output_reasoning_are_not_omitted():
    usage={'input_tokens':300000,'output_tokens':1000,'output_tokens_details':{'reasoning_tokens':800}}
    assert usage_cost(usage)==60750
    assert reservation_cost(300000,1000)==75850
    with pytest.raises(ValueError):prices('unknown-model')


def test_historical_call_is_not_repriced_when_default_changes(system,clip):
    settings,store,_=system
    settings.model='gpt-5.4-mini'
    reviews,review,run,_=setup_review(system,clip)
    old=reviews.reserve(review['id'],run['id'],'scan',10000,1000)
    usage={'input_tokens':10000,'output_tokens':1000}
    store.execute("UPDATE calls SET status='completed',usage=?,model=NULL,pricing=NULL WHERE id=?",(encode(usage),old))
    settings.model='gpt-6-luna'
    reviews.settle(old)
    assert store.one('SELECT actual_micros FROM calls WHERE id=?',(old,))['actual_micros']==12000
    new=reviews.reserve(review['id'],run['id'],'scan',10000,1000)
    store.execute("UPDATE calls SET status='completed',usage=? WHERE id=?",(encode(usage),new))
    reviews.settle(new)
    assert store.one('SELECT actual_micros FROM calls WHERE id=?',(new,))['actual_micros']==1500
    assert json.loads(reviews.get(review['id'])['profile'])['model']=='gpt-5.4-mini'


def test_provider_uses_luna_for_tools_and_records_actual_model_usage(system,monkeypatch):
    settings,store,_=system
    assert settings.model=='gpt-6-luna'
    monkeypatch.setattr(agent,'api_key',lambda:'test-secret')
    sent=[]
    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200,json={'status':'completed','model':'gpt-6-luna','service_tier':'default',
            'usage':{'input_tokens':2000,'output_tokens':100,'input_tokens_details':{'cache_write_tokens':1000}},
            'output':[{'type':'function_call','name':'complete_analysis','call_id':'call1','arguments':'{}'}]})
    real=httpx.Client
    monkeypatch.setattr(agent.httpx,'Client',lambda **kw:real(transport=httpx.MockTransport(handle),**kw))
    result=agent.Provider(settings,store,'test').create('agent',input='test',max_output_tokens=500)
    assert result.model=='gpt-6-luna' and len(sent)==1
    assert sent[0]['model']=='gpt-6-luna' and sent[0]['reasoning']=={'effort':'none'}
    assert sent[0]['service_tier']=='default' and sent[0]['prompt_cache_options']['ttl']=='30m'
    saved=store.one('SELECT * FROM calls')
    assert saved['model']==saved['response_model']=='gpt-6-luna'
    assert saved['actual_micros']==275 and saved['reserved_micros']==0
