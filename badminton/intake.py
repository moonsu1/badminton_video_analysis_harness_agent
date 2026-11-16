"""Remember the user's video type and goal; clarify only what is actually missing."""
import json
import re
import time

from .store import encode

KINDS={'match','lesson','rally','unknown'}
FOCUSES={'gameplay','technique','both','unspecified'}


def normalize_profile(profile=None,legacy=False):
    p=profile or {}
    kind=p.get('video_kind','match' if legacy else 'unknown')
    focus=p.get('analysis_focus','gameplay' if legacy else 'unspecified')
    if kind not in KINDS or focus not in FOCUSES:raise ValueError('분석 종류 또는 목적이 올바르지 않습니다.')
    if focus=='gameplay':kind='match'
    elif kind in {'lesson','rally'}:focus='technique'
    topics=list(dict.fromkeys(str(t).strip()[:80] for t in p.get('techniques',[]) if str(t).strip()))[:8]
    if focus=='gameplay':topics=[]
    return {'video_kind':kind,'analysis_focus':focus,'techniques':topics}


def stored_context(store,user,chat,ids):
    result=[]
    for vid in ids:
        video=store.video(vid,user,chat)
        row=store.one('SELECT * FROM analysis_preferences WHERE user_id=? AND chat_id=? AND video_id=?',(user,chat,vid))
        saved=store.one('SELECT profile,report FROM reviews WHERE user_id=? AND chat_id=? AND video_id=? ORDER BY updated DESC LIMIT 1',(user,chat,vid))
        inherited=normalize_profile(json.loads(saved['profile']).get('analysis_profile'),legacy=True) if saved else None
        reviewed=bool(saved and saved['report'])
        result.append({'video_id':vid,'filename':video['filename'],'player':video['player'],
            'has_saved_review':reviewed,
            'profile':json.loads(row['profile']) if row else inherited,
            'pending':json.loads(row['pending']) if row and row['pending'] else None})
    return result


def selection_command(store,run):
    """Explicit menu numbers are commands only while that exact video's menu is pending."""
    ids=json.loads(run['video_ids'])
    if len(ids)!=1:return None
    context=stored_context(store,run['user_id'],run['chat_id'],ids)[0]
    pending=context['pending']
    if not pending:return None
    choice=re.sub(r'<attached_files>.*?</attached_files>','',run['question'],flags=re.S).strip()
    selected=pending.get('options',{}).get(choice)
    if not selected:return None
    return {'intent':'match','player_description':'',**selected}


def prompt_for(missing,profile):
    lines=[];options={}
    if 'video_kind' in missing:
        lines.append('어떤 영상이고 무엇을 보고 싶으신가요?\n'
            '1. 경기 운영·전술과 전체 플레이 리포트\n'
            '2. 경기의 기술·자세 — 스매시 타점처럼 궁금한 동작도 함께 적어 주세요.\n'
            '3. 개인 강습 — 배운 동작의 수행과 교정\n'
            '4. 난타·반복 훈련 — 일관성과 반복되는 실수')
        options={'1':{'video_kind':'match','analysis_focus':'gameplay','techniques':[]},
                 '2':{'video_kind':'match','analysis_focus':'technique','techniques':[]},
                 '3':{'video_kind':'lesson','analysis_focus':'technique','techniques':[]},
                 '4':{'video_kind':'rally','analysis_focus':'technique','techniques':[]}}
    elif 'analysis_focus' in missing:
        lines.append('이 경기에서 어느 쪽을 우선 분석할까요?\n'
            '1. 경기 운영·전술 — 전체 특징, 보이는 수준, 잘한 점·아쉬운 점, 개선 우선순위\n'
            '2. 기술·자세 — 스매시·드라이브·푸시·언더·리시브·서브리시브·수비 중 궁금한 동작\n'
            '둘 다 원하시면 관심 있는 기술도 함께 적어 주세요.')
        options={'1':{'video_kind':'match','analysis_focus':'gameplay','techniques':[]},
                 '2':{'video_kind':'match','analysis_focus':'technique','techniques':[]}}
    elif 'techniques' in missing:
        lines.append('어떤 동작을 자세히 볼까요? 스매시·드라이브·푸시·언더·리시브·서브리시브·수비 중 한두 가지나, 궁금한 자세·타점·그립을 알려 주세요.')
    if 'player' in missing:
        lines.append('분석할 선수를 옷 색과 코트 위치로 함께 알려 주세요.')
    lines.append('영상은 다시 올리지 않아도 됩니다. 이미 말씀하신 내용은 다시 묻지 않습니다.')
    return '\n\n'.join(lines),options


def prepare(store,run,route):
    if route['intent'] in {'general','explanation'}:return {'ready':True,'route':route}
    ids=json.loads(run['video_ids'])
    if not ids:
        return {'ready':False,'route':route,'question':'분석할 영상을 첨부하고, 경기 운영 또는 기술·자세 중 원하는 분석과 본인의 옷·위치를 알려 주세요.'}
    if len(ids)!=1:
        if route['intent']=='clip':return {'ready':True,'route':route}
        return {'ready':False,'route':route,'question':'전체 리포트는 영상1개씩 진행합니다. 분석할 영상을 선택하고 경기 운영 또는 기술·자세 중 원하는 목적을 알려 주세요.'}
    user,chat=run['user_id'],run['chat_id'];vid=ids[0]
    context=stored_context(store,user,chat,ids)[0]
    prior=context['profile'] or {}
    legacy='video_kind' not in route and route['intent']=='match'
    supplied=normalize_profile(route,legacy=legacy)
    profile=normalize_profile(prior)
    if supplied['video_kind']!='unknown' and supplied['video_kind']!=profile['video_kind']:
        profile=normalize_profile({'video_kind':supplied['video_kind']})
    for field,unknown in [('video_kind','unknown'),('analysis_focus','unspecified')]:
        if supplied[field]!=unknown:profile[field]=supplied[field]
    if supplied['techniques']:profile['techniques']=supplied['techniques']
    elif supplied['analysis_focus']=='gameplay':profile['techniques']=[]
    if profile['video_kind'] in {'lesson','rally'} and profile['analysis_focus']=='unspecified':
        profile['analysis_focus']='technique'
    if route['intent']=='clip' and profile['analysis_focus']=='unspecified':profile['analysis_focus']='technique'
    if route.get('player_description') and (not context['player'] or route.get('player_changed',True)):
        store.execute('UPDATE videos SET player=? WHERE id=? AND user_id=? AND chat_id=?',(route['player_description'][:500],vid,user,chat))
        context['player']=route['player_description'][:500]
    pending=context['pending']
    if pending:route['analysis_question']=pending['question']+'\n추가 설명: '+run['question']
    route['analysis_profile']=profile
    missing=[]
    if route['intent']!='clip':
        if profile['video_kind']=='unknown':missing.append('video_kind')
        if profile['analysis_focus']=='unspecified':missing.append('analysis_focus')
        if profile['video_kind']=='match' and profile['analysis_focus'] in {'technique','both'} and not profile['techniques']:
            missing.append('techniques')
        if not context['player']:missing.append('player')
    if missing:
        question,options=prompt_for(missing,profile)
        pending={'question':pending['question'] if pending else run['question'],'missing':missing,'options':options,'run_id':run['id']}
        ready=False
    else:
        pending=None;question=None;ready=True
        if route['intent']=='video':route['intent']='match'
    persistent_profile=normalize_profile(prior) if route['intent']=='clip' and prior and not context['pending'] else profile
    store.execute('INSERT INTO analysis_preferences(user_id,chat_id,video_id,profile,pending,updated) VALUES(?,?,?,?,?,?) '
        'ON CONFLICT(user_id,chat_id,video_id) DO UPDATE SET profile=excluded.profile,pending=excluded.pending,updated=excluded.updated',
        (user,chat,vid,encode(persistent_profile),encode(pending) if pending else None,time.time()))
    store.execute('UPDATE runs SET route=? WHERE id=?',(encode(route),run['id']))
    return {'ready':ready,'route':route,'question':question}


def describe(profile):
    p=normalize_profile(profile)
    kind={'match':'경기','lesson':'개인 강습','rally':'난타·반복 훈련','unknown':'영상'}[p['video_kind']]
    focus={'gameplay':'경기 운영·전술','technique':'기술·자세','both':'경기 운영과 선택 기술','unspecified':'전체 관찰'}[p['analysis_focus']]
    topics=' · 관심 동작: '+', '.join(p['techniques']) if p['techniques'] else ''
    return kind+' · '+focus+topics
