"""Standard API rates; each call retains its own model/rate snapshot."""
from decimal import Decimal,ROUND_CEILING

DEFAULT_MODEL='gpt-6-luna'
LEGACY_MODEL='gpt-5.4-mini'
RATES={
    'gpt-6-luna':{'input':.10,'cached':.01,'output':.50,'write_30m':.125,'write_12h':.25,
                  'long_threshold':272000,'long_input_multiplier':2,'long_output_multiplier':1.5},
    'gpt-5.4-mini':{'input':.75,'cached':.075,'output':4.50,'write_30m':.75,'write_12h':.75},
}


def prices(model):
    canonical=LEGACY_MODEL if model=='gpt-5.4-mini-2026-03-17' else model
    if canonical not in RATES:raise ValueError('비용 계산에 등록되지 않은 모델입니다.')
    return {'model':model,'service_tier':'default',**RATES[canonical]}


def effective(rates,tokens):
    rates=dict(rates)
    if rates.get('long_threshold') and tokens>rates['long_threshold']:
        for name in ('input','cached','write_30m','write_12h'):rates[name]*=rates['long_input_multiplier']
        rates['output']*=rates['long_output_multiplier']
    return rates


def rounded(value):return int(value.to_integral_value(rounding=ROUND_CEILING))


def reservation_cost(input_tokens,max_output_tokens,model=DEFAULT_MODEL):
    rates=effective(prices(model),input_tokens)
    # Implicit 30m writes may cost more than ordinary input. No cache discount
    # is assumed before a response, and output includes any reasoning tokens.
    return rounded(Decimal(input_tokens)*Decimal(str(max(rates['input'],rates['write_30m'])))+
                   Decimal(max_output_tokens)*Decimal(str(rates['output'])))+100


def usage_cost(usage,model=DEFAULT_MODEL,rates=None):
    rates=effective(rates or prices(model),usage.get('input_tokens',0))
    details=usage.get('input_tokens_details') or {}
    cached=details.get('cached_tokens',0);writes=details.get('cache_write_tokens',0)
    split=details.get('cache_write_tokens_details') or {}
    long_writes=split.get('ttl_12h_tokens',0)
    short_writes=split.get('ttl_30m_tokens',writes-long_writes)
    ordinary=usage.get('input_tokens',0)-cached-writes
    if min(ordinary,cached,short_writes,long_writes)<0 or short_writes+long_writes!=writes:
        raise ValueError('사용량의 캐시 토큰 합계가 올바르지 않습니다.')
    total=sum(Decimal(n)*Decimal(str(rates[key])) for n,key in [
        (ordinary,'input'),(cached,'cached'),(short_writes,'write_30m'),(long_writes,'write_12h'),
        (usage.get('output_tokens',0),'output')])
    return rounded(total)
