"""Apply final evidence checks to the acceptance cache, within its existing ledger."""
import argparse
import json
import os
import time
from pathlib import Path

from badminton.config import load_settings
from badminton.review import MatchReviewer,all_observations,extract_match_frames,render_report,validate_report
from badminton.review_store import Reviews
from badminton.store import Store,encode
from badminton.video import VideoService


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--data',required=True)
    parser.add_argument('--recheck',action='store_true')
    args=parser.parse_args()
    os.environ['BADMINTON_DATA_DIR']=str(Path(args.data).resolve())
    settings=load_settings().initialize();store=Store(settings.root/'agent.db')
    saved=store.one('SELECT * FROM reviews ORDER BY created DESC LIMIT 1')
    assert saved and saved['status']=='completed'
    assert not store.one("SELECT id FROM calls WHERE review_id=? AND status IN ('running','reserved','unknown')",(saved['id'],))
    reviews=Reviews(settings,store);run=store.run(saved['run_id'])
    reviewer=MatchReviewer(settings,store,VideoService(settings,store),run,saved['id'])
    observations=all_observations(store,saved['id'])
    frames=extract_match_frames(settings,reviewer.video)
    report_json=json.loads(saved['report_json'])
    if report_json.get('quality_recheck_units'):
        observations=json.loads((settings.root/'quality-rechecked-observations.json').read_text(encoding='utf-8'))
        report_json=validate_report(report_json,observations,reviewer.video['info']['duration'])
        report_json['quality_recheck_units']=8
    if not report_json.get('identity_checks'):
        (settings.root/'pre-identity-guard.md').write_text(saved['report'],encoding='utf-8')
        # Refresh the synthesis after removing the earlier 96-observation truncation;
        # never repeat the paid whole-video scans or reset their cost ledger.
        if not report_json.get('quality_recheck_units'):report_json=reviewer.synthesize(observations)
        report_json=reviewer.verify_report(report_json,observations,frames)
    elif args.recheck:
        report_json=reviewer.verify_report(report_json,observations,frames)
    fine=sum(json.loads(r['result']).get('frames',0) for r in store.all(
        "SELECT result FROM review_units WHERE review_id=? AND kind='refine' AND result IS NOT NULL",(saved['id'],)))
    status=reviews.status(saved['id'])
    report=render_report(reviewer.video,status,report_json,observations,fine)
    reviewer.update(status='completed',phase='done',report=report,report_json=encode(report_json))
    store.update_run(run['id'],'completed',answer=report)
    status=reviews.status(saved['id'])
    assert status['sampling_coverage']==1 and status['spent_usd']+status['reserved_usd']<=3.5
    assert len(report_json['scene_ids'])>=6
    assert report_json['strengths'] or report_json['improvements']
    acceptance=Path(os.environ['LOCALAPPDATA'])/'BadmintonAgent/logs/match-review-acceptance.json'
    record=json.loads(acceptance.read_text(encoding='utf-8'))
    record.update(review=status,answer=report,identity_guard=True,finalized=time.time())
    acceptance.write_text(encode(record),encoding='utf-8')
    print(encode({'review_id':saved['id'],'cost_usd':status['spent_usd'],'scenes':len(report_json['scene_ids']),
        'identity_checks':report_json['identity_checks'],'all_scan_observations':len(observations)}),flush=True)


if __name__=='__main__':main()
