"""Five-part reports with purpose-specific rubrics and evidence-bounded level estimates."""
from .intake import normalize_profile,describe

VERSION='five-part-v1'
TIERS=['novice','beginner','developing','intermediate','advanced_intermediate','advanced']
TIER_NAMES=dict(zip(TIERS,['입문','초급','초중급','중급','중상급','상급']))


def obj(fields):
    return {'type':'object','additionalProperties':False,'properties':fields,'required':list(fields)}


def section_schema():
    text={'type':'string'};refs={'type':'array','items':text}
    return {'overview':obj({'summary':text,'evidence_ids':refs}),
            'level_assessment':obj({
                'lower_tier':{'type':'string','enum':TIERS+['unrated']},
                'upper_tier':{'type':'string','enum':TIERS+['unrated']},
                'confidence':{'type':'string','enum':['low','medium','high']},
                'summary':text,'evidence_ids':refs,
                'criteria':{'type':'array','items':obj({'name':text,'assessment':text,'evidence_ids':refs})},
                'limitations':text})}


def validate_sections(draft,known,duration,independent):
    profile=normalize_profile(draft.get('analysis_profile'),legacy=True)
    overview=draft.get('overview') or {};ids=list(dict.fromkeys(overview.get('evidence_ids',[])))
    overview={'summary':overview.get('summary',''),'evidence_ids':ids} if ids and all(i in known for i in ids) else {'summary':'','evidence_ids':[]}
    raw=draft.get('level_assessment') or {};criteria=[]
    for c in raw.get('criteria',[]):
        refs=list(dict.fromkeys(c.get('evidence_ids',[])))
        if refs and all(i in known for i in refs):criteria.append({**c,'evidence_ids':refs})
    ids=list(dict.fromkeys(raw.get('evidence_ids',[])+[i for c in criteria for i in c['evidence_ids']]))
    sources=[known[i] for i in ids if i in known]
    independent_sources=any(independent(a,b) for n,a in enumerate(sources) for b in sources[n+1:])
    lower,upper=raw.get('lower_tier'),raw.get('upper_tier')
    rated=(len(criteria)>=2 and ids and all(i in known for i in ids) and independent_sources
           and lower in TIERS and upper in TIERS and TIERS.index(lower)<=TIERS.index(upper))
    if profile['video_kind']!='match' and raw.get('summary') and criteria and all(i in known for i in ids):
        level={**raw,'criteria':criteria,'evidence_ids':ids,'confidence':'low','status':'performance'}
    elif rated:
        from .review_store import merged_ranges
        ranges=merged_ranges([(o['unit_start'],min(o['unit_end'],duration or o['unit_end'])) for o in known.values()])
        represented=sum(b-a for a,b in ranges)/max(duration or 1,1)
        thirds={min(2,int(o['focus']/max(duration or 1,1)*3)) for o in sources}
        confidence=raw.get('confidence','low')
        if len(criteria)<3 or len(thirds)<3 or represented<.5:confidence='low'
        elif confidence=='high':confidence='medium'
        level={**raw,'criteria':criteria,'evidence_ids':ids,'confidence':confidence,'status':'estimated','evidence_time_coverage':round(represented,4)}
    else:
        level={'lower_tier':'unrated','upper_tier':'unrated','confidence':'low','summary':'',
               'criteria':criteria,'evidence_ids':[i for i in ids if i in known],'status':'limited',
               'limitations':raw.get('limitations','')}
    return {'overview':overview,'level_assessment':level,'analysis_profile':profile,
            'report_version':VERSION}


def guidance(profile):
    p=normalize_profile(profile,legacy=True)
    if p['video_kind']=='lesson':
        return ('개인 강습 리포트다. 배운 동작의 영상상 목표, 현재 수행, 잘 따라한 점, 교정할 동작,다음 강습 전 과제를 설명한다. '
                '경기 승패/전술/전체 경기급수로 평가하지 않는다. 오디오를 제공하지 않았으므로 코치가 실제로 한 말을 듣거나 인용했다고 주장하지 않는다.')
    if p['video_kind']=='rally':
        return ('난타/반복 훈련 리포트다. 반복 동작의 리듬·재현성·라켓 준비·연속 처리·복귀와 흔들리는 패턴을 본다. '
                '경기 승패/상대 전술이나 전체 경기급수를 판정하지 않는다.')
    if p['analysis_focus']=='technique':
        return ('선택 기술/자세 리포트다. 관심 동작 '+(', '.join(p['techniques']) or '자세·동작')+
                '의 준비·이동·스윙·타점·복귀를 중심으로 좋은 수행과 교정할 점을 본다. 해당 기술의 수행 수준을 전체 선수 실력으로 확대하지 않는다.')
    return ('경기 운영/전술 리포트다. 경기 전체 흐름,위치 선택,공수 전환,파트너와의 공간 분담,압박 상황의 판단과 대응을 우선 본다. '
            '수많은 기술을 전부 분류하지 말고 경기 양상에 영향을 준 플레이에 집중한다. '
            +('관심 기술 '+', '.join(p['techniques'])+'도 함께 본다.' if p['analysis_focus']=='both' else ''))


def render(video,status,report,observations,fine_count=0):
    import re
    from .review import timestamp
    known={o['id']:o for o in observations};complete=status['sampling_coverage']>=.999999
    profile=normalize_profile(report.get('analysis_profile'),legacy=True)
    if profile['video_kind']=='lesson':
        title='개인 강습 분석 리포트'
        headings=['훈련 동작과 목표','현재 동작 수행 상태','잘 따라한 점','교정할 동작','다음 강습 전 연습 과제']
    elif profile['video_kind']=='rally':
        title='난타·반복 훈련 리포트'
        headings=['훈련 패턴과 전체 흐름','반복 동작의 안정성','유지할 좋은 동작','흔들리는 패턴과 개선 후보','반복 훈련 계획']
    elif profile['analysis_focus']=='technique':
        title='기술·자세 분석 리포트'
        headings=['분석한 기술과 플레이 맥락','선택 기술의 수행 수준','잘한 기술 동작','교정할 동작과 개선 후보','기술 훈련 우선순위']
    else:
        title='경기 분석 리포트'
        headings=['이 경기의 전반적인 특징','이번 경기에서 보이는 나의 수준','이 경기에서 잘한 점','아쉬운 점과 개선 후보','개선 우선순위와 연습 계획']
    if not complete:title='부분 경기 리뷰 — 미확인 구간 있음' if profile['video_kind']=='match' else '부분 '+title
    lines=['## '+title,video['filename']+' · '+describe(profile)]
    coaching=report.get('scene_coaching',[])
    time_by_frame={f['frame_id']:f['time'] for p in coaching for f in p['frames']}
    def text(value):
        value=re.sub(r'F\d+',lambda m:timestamp(time_by_frame[m.group()]) if m.group() in time_by_frame else '해당 장면',str(value))
        short={i.split(':',1)[-1]:timestamp(o['focus']) for i,o in known.items() if ':' in i}
        return re.sub(r'\bS\d{4}:O\d+\b',lambda m:short.get(m.group(),'해당 근거'),value)
    def source_refs(ids,details=False):
        for eid in ids:
            if eid not in known:continue
            o=known[eid]
            lines.append(f"- [{timestamp(o['start'])}–{timestamp(o['end'])}] "+text(o['observation']) if details
                         else f"- 근거 장면: [{timestamp(o['start'])}–{timestamp(o['end'])}]")
    def claims(key):
        for c in report.get(key,[])[:3]:
            lines.append('**'+text(c['text'])+'**');source_refs(c['evidence_ids'],details=True)
            if c.get('counterexample_ids'):
                lines.append('다른 양상이 보인 시각: '+', '.join(timestamp(known[i]['focus']) for i in c['counterexample_ids'] if i in known))
    def scene(point):
        label={'observed_issue':'이 장면의 개선점','training_experiment':'다음 경기에서 비교할 조정','strength':'유지할 행동'}[point['assessment']]
        lines.append(f"**[{timestamp(point['start'])}–{timestamp(point['end'])}] {label}**")
        observation=text(point['observation'])
        if '임팩트' in point.get('uncertainty','') or '접촉' in point.get('uncertainty',''):
            observation=observation.replace('임팩트 순간','스윙 구간')
        lines.extend(['관찰: '+observation,'플레이에 미칠 수 있는 영향: '+point['possible_effect'],'바꿀 행동: '+point['action']])
    lines.append('### 1. '+headings[0])
    overview=report.get('overview') or {}
    if overview.get('summary'):
        lines.append(text(overview['summary']));source_refs(overview.get('evidence_ids',[])[:3])
    claims('tendencies')
    if not overview.get('summary') and not report.get('tendencies'):
        lines.append('아래 시각의 준비·이동·타구·복귀와 연결되는 플레이를 중심으로 정리했습니다.')
    lines.append('### 2. '+headings[1])
    level=report.get('level_assessment') or {}
    if level.get('status') in {'estimated','performance'}:
        if profile['video_kind']=='match':
            lower,upper=TIER_NAMES[level['lower_tier']],TIER_NAMES[level['upper_tier']]
            label=lower if lower==upper else lower+'~'+upper
            lines.append('**영상 기준 추정: '+label+' · '+{'low':'잠정 평가','medium':'중간 신뢰','high':'중간 신뢰'}[level['confidence']]+'**')
            lines.append('이번 영상에 드러난 수행 기준이며, 동호인 대회 급수나 공식 등급 판정과는 구분합니다.')
        lines.append(text(level.get('summary','')))
        for c in level.get('criteria',[])[:5]:
            lines.append('**'+c['name']+':** '+c['assessment']);source_refs(c['evidence_ids'][:2])
        if level.get('limitations'):lines.append('평가 범위: '+level['limitations'])
    else:
        for c in level.get('criteria',[])[:5]:
            lines.append('**'+c['name']+':** '+c['assessment']);source_refs(c['evidence_ids'][:2])
        if not level.get('criteria'):lines.append('현재는 아래 장면별 수행을 평가했으며, 전체 수준 평가는 추가 생성 대상입니다.')
    lines.append('### 3. '+headings[2]);claims('strengths')
    positives=[p for p in coaching if p['assessment']=='strength']
    for point in positives[:3]:scene(point)
    if not positives and not report.get('strengths'):lines.append('선택한 장면의 좋은 수행은 아래 관찰 사례와 함께 확인합니다.')
    lines.append('### 4. '+headings[3]);claims('improvements')
    adjustments=[p for p in coaching if p['assessment']!='strength']
    for point in adjustments[:3]:scene(point)
    if adjustments:lines.append('개선 후보는 원인이 입증된 결함과 구분하며, 다음 영상에서 행동을 바꾼 효과를 비교합니다.')
    if not adjustments and not report.get('improvements'):
        lines.append('아직 장면별 개선 코칭을 생성하지 못했습니다. 이를 개선점이 없다는 뜻이나 코칭 완료로 처리하지 않습니다.')
    lines.append('### 5. '+headings[4])
    ranked=sorted(coaching,key=lambda p:({'observed_issue':0,'training_experiment':1,'strength':2}[p['assessment']],p['start']))
    priorities=[];sources=set();topics=set()
    for p in ranked:
        if p['source_observation_id'] not in sources and p['topic'] not in topics:
            priorities.append(p);sources.add(p['source_observation_id']);topics.add(p['topic'])
        if len(priorities)==3:break
    for n,p in enumerate(priorities,1):
        lines.extend([f"**{n}. [{timestamp(p['start'])}–{timestamp(p['end'])}] {p['action']}**",
                      '연습: '+p['drill'],'다음 영상에서 확인할 기준: '+p['success_check']])
    if not priorities:
        for n,c in enumerate(report.get('improvements',[])[:3],1):lines.append(f"{n}. "+c.get('drill',''))
    cited={i for key in ('tendencies','strengths','improvements') for c in report.get(key,[]) for i in c['evidence_ids']}
    cited.update(p['source_observation_id'] for p in coaching)
    extra=[i for i in report.get('scene_ids',[]) if i not in cited]
    if extra:lines.append('### 추가 관찰 장면');source_refs(extra[:6],details=True)
    lines.append('### 판독 범위와 한계')
    lines.append(('전체 시간대 판독 완료' if complete else '미확인 시간대가 남은 부분 판독')+
        f" · 2fps 전체 코트 · {status['sampling_coverage']*100:.1f}% · {status['completed_units']}/{status['total_units']}구간 · 원본 정밀 보완 {fine_count}장")
    if status['missing_ranges']:lines.append('미확인: '+', '.join(timestamp(a)+'–'+timestamp(b) for a,b in status['missing_ranges']))
    lines.append('표본 판독의 시간 범위이며, 모든 타격·실수의 검출이나 속도 계측을 뜻하지 않습니다.')
    lines.extend('- '+str(x) for x in list(dict.fromkeys(report.get('limitations',[])))[:3] if x)
    lines.append(f"API 사용 USD {status['spent_usd']:.4f} · 미정산 예약 USD {status['reserved_usd']:.4f} · 상한 USD {status['cap_usd']:.2f}")
    if not complete:lines.append('기존 예산으로 리뷰계속, 추가 예산을 승인하려면 리뷰계속 1.00처럼 금액을 입력해 주세요.')
    return '\n\n'.join(lines)
