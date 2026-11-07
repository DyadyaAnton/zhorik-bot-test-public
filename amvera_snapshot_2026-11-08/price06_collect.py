"""Bounded, resumable candidate collection using existing VkusVill/Magnit probes.

No network calls inside SQLite transactions. Unconfirmed region remains unknown;
candidate observations never become verified market quotes automatically.
"""
import json

from price_b_catalog import seeds
from price02_pipeline import ensure, stage

PAGE_SIZE = 5
TRACK = 'price06_candidate_batch'


def queries():
    return tuple(name for _,name,_,_ in seeds())


def cursor(db, retailer='vkusvill'):
    """Resume from last completed page; an unavailable schema means page zero."""
    if retailer not in ('vkusvill','magnit'):raise ValueError('Unsupported retailer')
    exists=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pb_refresh_runs'").fetchone()
    if not exists:return 0
    result=db.execute('''SELECT report_json FROM pb_refresh_runs
        WHERE trigger_type='manual' AND category=? AND retailer=? AND status='completed'
        ORDER BY id DESC LIMIT 1''',(TRACK,retailer)).fetchone()
    if not result:return 0
    try:
        offset=json.loads(result[0])['next_offset']
    except (TypeError,KeyError,ValueError):return 0
    return offset if type(offset) is int and 0 <= offset <= len(queries()) else 0


def page(start):
    total=len(queries())
    if type(start) is not int or not 0<=start<=total:
        raise ValueError('Invalid collection cursor')
    return queries()[start:start+PAGE_SIZE]


def save_batch(db,start,results,*,retailer='vkusvill',store_code=None):
    """Commit all candidate rows and the progress marker atomically.

    `results`: list of (query, {retailer: rows-or-exception}) produced outside DB.
    Do not advance the cursor if every attempted source has failed.
    """
    expected=page(start)
    if retailer not in ('vkusvill','magnit'):
        raise ValueError('Unsupported retailer')
    if retailer=='magnit' and not store_code:
        raise ValueError('Magnit store code is required')
    if not expected or len(results)!=len(expected):
        raise ValueError('Incomplete price collection page')
    if tuple(query for query,_ in results)!=expected:
        raise ValueError('Queries do not match scheduled canonical page')
    ensure(db)
    report={'offset':start,'next_offset':start,'queries':len(expected),
            'successful_sources':0,'failed_sources':0,'observations':0,
            'review_required':0}
    db.execute('SAVEPOINT price06_page')
    try:
        for query,sources in results:
            if tuple(sources)!=(retailer,):
                raise ValueError('Price batch must contain exactly one chosen retailer')
            for source,rows in sources.items():
                if isinstance(rows,Exception):
                    report['failed_sources']+=1
                    continue
                outcome=stage(db,source,query,rows,
                              store_code=store_code if source=='magnit' else None)
                report['successful_sources']+=1
                report['observations']+=outcome['staged']
                report['review_required']+=outcome['review_required']
        if report['successful_sources'] and not report['failed_sources']:
            report['next_offset']=min(len(queries()),start+len(expected))
            status='completed'
        else:
            status='partial' if report['successful_sources'] else 'failed'
        db.execute('''INSERT INTO pb_refresh_runs
            (trigger_type,retailer,category,status,finished_at,report_json)
            VALUES('manual',?,?,?,CURRENT_TIMESTAMP,?)''',
            (retailer,TRACK,status,json.dumps(report,ensure_ascii=False)))
        db.execute('RELEASE SAVEPOINT price06_page')
        return report
    except BaseException:
        db.execute('ROLLBACK TO SAVEPOINT price06_page')
        db.execute('RELEASE SAVEPOINT price06_page')
        raise
