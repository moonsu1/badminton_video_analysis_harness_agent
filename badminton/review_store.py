"""Durable match review state and an atomic, conservative USD ledger."""
import hashlib
import json
import math
import time
import uuid
from decimal import Decimal, ROUND_CEILING

from .store import encode
from .pricing import prices,reservation_cost,usage_cost,LEGACY_MODEL

VERSION='match-v1-2fps1280'


class BudgetExceeded(ValueError):
    pass


def micros(value):
    return int((Decimal(str(value))*1000000).to_integral_value(rounding=ROUND_CEILING))


def windows(duration):
    return [(round(start,6),round(min(start+20,duration),6)) for start in range(0,math.ceil(duration),18)]


def fair_order(items,duration):
    groups=[[],[],[]]
    for item in items:
        group=min(2,int((item['start']+item['end'])/2/max(duration,1)*3))
        groups[group].append(item)
    return [group[index] for index in range(max(map(len,groups),default=0)) for group in groups if index<len(group)]


def merged_ranges(ranges):
    result=[]
    for start,end in sorted(ranges):
        if result and start<=result[-1][1]+.000001:result[-1][1]=max(end,result[-1][1])
        else:result.append([start,end])
    return result


class Reviews:
    def __init__(self,settings,store):
        self.settings,self.store=settings,store

    def get(self,review_id,user=None,chat=None):
        row=self.store.one('SELECT * FROM reviews WHERE id=?',(review_id,))
        if not row or (user is not None and (row['user_id']!=user or row['chat_id']!=chat)):
            raise PermissionError('이 대화에서 접근할 수 없는 리뷰입니다.')
        return row

    def ensure(self,run,video):
        fingerprint=[VERSION,run['user_id'],run['chat_id'],video['sha256'],video['player'],self.settings.model]
        key=hashlib.sha256(encode(fingerprint).encode()).hexdigest()
        now=time.time();review_id=uuid.uuid4().hex
        profile={'version':VERSION,'model':self.settings.model,'fps':2,'long_side':1280,'jpeg_quality':90,
                 'player':video['player'],'video_sha256':video['sha256'],'duration':video['info']['duration'],'prices':prices(self.settings.model),
                 'analysis_profile':json.loads(run.get('route') or '{}').get('analysis_profile') or
                     {'video_kind':'match','analysis_focus':'gameplay','techniques':[]}}
        with self.store.connect() as db:
            db.execute('INSERT OR IGNORE INTO reviews(id,cache_key,video_id,user_id,chat_id,run_id,status,phase,profile,cap_micros,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (review_id,key,video['id'],run['user_id'],run['chat_id'],run['id'],'queued','preparing',encode(profile),micros(self.settings.review_max_usd),now,now))
            row=dict(db.execute('SELECT * FROM reviews WHERE cache_key=?',(key,)).fetchone())
            for index,(start,end) in enumerate(windows(video['info']['duration'])):
                db.execute('INSERT OR IGNORE INTO review_units(id,review_id,kind,start,end,position,status) VALUES(?,?,?,?,?,?,?)',
                    (f"{row['id']}:S{index:04d}",row['id'],'scan',start,end,index,'pending'))
            db.execute('UPDATE runs SET review_id=?,scope=? WHERE id=?',(row['id'],'match',run['id']))
        return row

    def money(self,review_id):
        rows=self.store.all('SELECT status,reserved_micros,actual_micros FROM calls WHERE review_id=?',(review_id,))
        spent=sum(r['actual_micros'] or 0 for r in rows)
        held=sum(r['reserved_micros'] or 0 for r in rows if r['status'] in {'reserved','running','unknown'})
        return spent,held

    def reserve(self,review_id,run_id,stage,input_tokens,max_output_tokens):
        bound=reservation_cost(input_tokens,max_output_tokens,self.settings.model)
        safety=micros(self.settings.review_safety_reserve)
        reserve_summary=0 if stage in {'synthesis','verification'} else micros(self.settings.review_summary_reserve)
        reserve_coaching=micros(self.settings.review_coaching_reserve) if stage in {'scan','refine'} else 0
        call_id=uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT cap_micros,status,report FROM reviews WHERE id=?',(review_id,)).fetchone()
            if stage=='coaching' and row['status']=='completed' and row['report']:
                finished=db.execute("SELECT status FROM runs WHERE id=? AND review_id=?",(run_id,review_id)).fetchone()
                if finished and finished['status']=='completed':reserve_summary=0
            used=db.execute("SELECT COALESCE(SUM(actual_micros),0)+COALESCE(SUM(CASE WHEN status IN ('reserved','running','unknown') THEN reserved_micros ELSE 0 END),0) FROM calls WHERE review_id=?",(review_id,)).fetchone()[0]
            if used+bound+safety+reserve_summary+reserve_coaching>row['cap_micros']:
                raise BudgetExceeded('리뷰 비용 상한에 도달하여 새로운 판독을 보내지 않았습니다.')
            if stage=='refine':
                fine=db.execute("SELECT COALESCE(SUM(actual_micros),0)+COALESCE(SUM(CASE WHEN status IN ('reserved','running','unknown') THEN reserved_micros ELSE 0 END),0) FROM calls WHERE review_id=? AND stage='refine'",(review_id,)).fetchone()[0]
                if fine+bound>micros(self.settings.review_refine_max_usd):raise BudgetExceeded('정밀 보완 비용 한도입니다.')
            db.execute('INSERT INTO calls(id,run_id,purpose,status,created,review_id,stage,reserved_micros,model,pricing,service_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (call_id,run_id,'review_'+stage,'reserved',time.time(),review_id,stage,bound,self.settings.model,encode(prices(self.settings.model)),'default'))
        return call_id

    def settle(self,call_id):
        row=self.store.one('SELECT * FROM calls WHERE id=?',(call_id,))
        if row['usage']:
            # Historical rows predate per-call model tracking. Never reprice them
            # using the newly selected default model.
            review=self.store.one('SELECT profile FROM reviews WHERE id=?',(row['review_id'],)) if row['review_id'] else None
            model=row['model'] or (json.loads(review['profile']).get('model') if review else None) or LEGACY_MODEL
            rates=json.loads(row['pricing']) if row['pricing'] else prices(model)
            self.store.execute('UPDATE calls SET actual_micros=?,reserved_micros=0 WHERE id=?',(usage_cost(json.loads(row['usage']),rates=rates),call_id))
        elif row['status']=='failed':
            self.store.execute('UPDATE calls SET reserved_micros=0 WHERE id=?',(call_id,))

    def status(self,review_id,user=None,chat=None):
        row=self.get(review_id,user,chat);profile=json.loads(row['profile']);duration=profile['duration']
        units=self.store.all("SELECT * FROM review_units WHERE review_id=? AND kind='scan' ORDER BY start",(review_id,))
        covered=merged_ranges([(u['start'],u['end']) for u in units if u['status']=='completed'])
        missing=[];cursor=0
        for start,end in covered:
            if start>cursor:missing.append([cursor,start])
            cursor=max(cursor,end)
        if cursor<duration:missing.append([cursor,duration])
        confirmed=[];unconfirmed=[]
        for unit in units:
            if unit['status']=='completed' and unit['result']:
                confirmed += [(o['start'],o['end']) for o in json.loads(unit['result']).get('observations',[]) if o.get('player_visible')]
                observations=json.loads(unit['result']).get('observations',[])
                unconfirmed += [(o['start'],o['end']) for o in observations if not o.get('player_visible')]
                if not any(o.get('player_visible') for o in observations):unconfirmed.append((unit['start'],unit['end']))
        spent,held=self.money(review_id)
        return {'id':row['id'],'status':row['status'],'phase':row['phase'],'fps':profile['fps'],'long_side':profile['long_side'],
                'sampling_coverage':round(sum(b-a for a,b in covered)/duration,6) if duration else 0,
                'completed_units':sum(u['status']=='completed' for u in units),'total_units':len(units),
                'observed_ranges':covered,'missing_ranges':missing,'player_confirmed_ranges':merged_ranges(confirmed),
                'player_unconfirmed_ranges':merged_ranges(unconfirmed),
                'spent_usd':spent/1e6,'reserved_usd':held/1e6,'cap_usd':row['cap_micros']/1e6,
                'snapshot':row['snapshot'],'report':row['report'],'report_json':json.loads(row['report_json']) if row['report_json'] else None}

    def resume(self,review_id,user,chat,additional_usd=0,request_id=None):
        row=self.get(review_id,user,chat)
        amount=Decimal(str(additional_usd))
        if not amount.is_finite() or amount<0 or amount>100 or amount.as_tuple().exponent < -2:
            raise ValueError('추가 예산은 소수점2자리의 0~100달러로 지정해 주세요.')
        if row['status'] in {'running','queued'}:return row
        if row['status']=='completed':return row
        if not request_id:raise ValueError('재개 요청 식별자가 필요합니다.')
        spent,held=self.money(review_id)
        if not amount and row['status']=='paused_budget' and row['cap_micros']-spent-held<=micros(self.settings.review_summary_reserve+self.settings.review_safety_reserve)+5000:
            return row
        # Deduplicate monetary approval using the run's unique request ID in one transaction.
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT id FROM runs WHERE user_id=? AND chat_id=? AND request_id=?',(user,chat,request_id)).fetchone()
            if existing:return self.get(review_id,user,chat)
            video=self.store.video(row['video_id'],user,chat)
            run_id=uuid.uuid4().hex;now=time.time()
            db.execute('INSERT INTO runs(id,user_id,chat_id,request_id,question,video_ids,status,created,updated,analysis_version,scope,review_id,route) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (run_id,user,chat,request_id,'리뷰계속 '+str(amount),encode([video['id']]),'queued',now,now,3,'match',review_id,encode({'intent':'match','player_description':''})))
            db.execute("UPDATE reviews SET cap_micros=cap_micros+?,status='queued',phase='resuming',run_id=?,cancel=0,updated=? WHERE id=?",(micros(amount),run_id,now,review_id))
        return self.get(review_id,user,chat)
