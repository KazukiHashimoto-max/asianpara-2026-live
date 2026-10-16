#!/usr/bin/env python3
"""Official-source-only Asian Para Games 2026 snapshot updater.

Strict boundaries:
- Official news and official venue session tables may be fetched automatically.
- No scraping of unverified Bornan results APIs; no automatic confirmation of gold.
- News that might mention Japanese gold is merely a review candidate, NEVER a result.
- On source failure keep last successful data and record failure state.
"""
from __future__ import annotations
import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
import requests
from bs4 import BeautifulSoup
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_PATH = ROOT / 'data/live.json'
VENUES_PATH = ROOT / 'data/venues.json'
JST = ZoneInfo('Asia/Tokyo')
BASE = 'https://www.asianparagames-2026.org'
SOURCES = [
    BASE + '/wp-json/wp/v2/posts?per_page=30&_fields=id,date,date_gmt,modified,link,title,excerpt',
    BASE + '/ja/wp-json/wp/v2/posts?per_page=30&_fields=id,date,date_gmt,modified,link,title,excerpt',
]
RSS_URL = BASE + '/ja/feed/'
ARCHIVE_URL = BASE + '/ja/2026/'
TIMEOUT = 12
UA = 'AsianPara-2026-SNS-Newsroom/3.0 (+https://www.asianparagames-2026.org/ja/; official-public-content-only)'


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def text_of(raw):
    return ' '.join(BeautifulSoup(html.unescape(str(raw or '')), 'html.parser').stripped_strings).strip()


def allowed_official(raw):
    try:
        u = urlsplit(str(raw))
        return u.scheme == 'https' and u.hostname in ('www.asianparagames-2026.org', 'asianparagames-2026.org')
    except Exception:
        return False


def request_text(session, url):
    if not allowed_official(url):
        raise ValueError('非公式URLを拒否')
    r = session.get(url, timeout=TIMEOUT, headers={'User-Agent': UA, 'Accept': 'application/json,application/rss+xml,text/html;q=0.9,*/*;q=0.8'})
    r.raise_for_status()
    if len(r.content) > 4_000_000:
        raise ValueError('取得サイズが大きすぎます')
    return r.text


def wp_posts(items):
    if not isinstance(items, list):
        raise ValueError('WordPress配列ではありません')
    out=[]
    for p in items:
        url = p.get('link')
        if not allowed_official(url): continue
        title = text_of(p.get('title', {}).get('rendered', ''))
        if not title: continue
        stamp = p.get('date_gmt') or p.get('date') or ''
        if p.get('date_gmt'): stamp += 'Z'
        out.append({'id':str(p.get('id') or url),'url':url,'title':title,'date':stamp,'modified':p.get('modified',''),'summary':text_of(p.get('excerpt',{}).get('rendered',''))[:700]})
    return out


def rss_posts(xml):
    root = ET.fromstring(xml)
    out=[]
    for item in root.findall('.//item')[:40]:
        def field(tag): return (item.findtext(tag) or '').strip()
        url=field('link')
        if not allowed_official(url): continue
        title=text_of(field('title'))
        if title:
            out.append({'id':url,'url':url,'title':title,'date':field('pubDate'),'modified':'','summary':text_of(field('description'))[:700]})
    return out


def archive_posts(html_document):
    soup=BeautifulSoup(html_document,'html.parser')
    out=[]
    for a in soup.select('article a[href], h2 a[href], h3 a[href]'):
        href=a.get('href','')
        title=text_of(a.get_text(' ',strip=True))
        if len(title)<8 or not allowed_official(href):continue
        if not re.search(r'/ja/news-\d+/',href):continue
        out.append({'id':href,'url':href,'title':title,'date':'','modified':'','summary':''})
    if not out:raise ValueError('記事リンクが見つかりません')
    return out


def fetch_news(session):
    errors=[]
    for url in SOURCES:
        try:
            value=wp_posts(json.loads(request_text(session,url)))
            if value:return value, 'WordPress REST'
            errors.append('WPは0件')
        except Exception as ex:errors.append(f'WP {type(ex).__name__}: {str(ex)[:85]}')
    try:
        value=rss_posts(request_text(session,RSS_URL))
        if value:return value,'RSS'
        errors.append('RSSは0件')
    except Exception as ex:errors.append(f'RSS {type(ex).__name__}: {str(ex)[:85]}')
    try:
        value=archive_posts(request_text(session,ARCHIVE_URL))
        if value:return value,'公式記事一覧'
        errors.append('公式記事一覧は0件')
    except Exception as ex:errors.append(f'一覧 {type(ex).__name__}: {str(ex)[:85]}')
    raise RuntimeError('; '.join(errors))


def parse_venue_sessions(raw_html,sport,venue,url):
    """Parse only dated ticket-session rows from official Japanese venue tables.
    Respect HTML rowspans by inheriting date when later rows omit it.
    Some venue pages contain multiple tables, and a schedule may change format.
    """
    soup=BeautifulSoup(raw_html, 'html.parser')
    results=[]
    for table in soup.find_all('table'):
        table_text=table.get_text(' ',strip=True)
        if '競技開始時間' not in table_text and 'Competition Starts' not in table_text:
            continue
        last_day=None
        for row in table.find_all('tr'):
            cells=[text_of(c.get_text(' ',strip=True)) for c in row.find_all(['td','th'],recursive=False)]
            if not cells:continue
            joined=' / '.join(cells)
            daym=re.search(r'10\s*/\s*(1[6-9]|2[0-4])(?:\D|$)',joined)
            if daym: last_day=int(daym.group(1))
            else:
                m=re.search(r'Oct\.?\s*(1[6-9]|2[0-4])\b',joined,re.I)
                if m:last_day=int(m.group(1))
            if not last_day:continue
            codes=re.findall(r'\b[A-Z]{2,4}\d{2}\b',joined)
            if not codes:continue
            times=re.findall(r'(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)',joined)
            # Opening, competition start, estimated end. If table format differs, mark unknown.
            opening=times[-3] if len(times)>=3 else ''
            start=times[-2] if len(times)>=2 else ''
            end=times[-1] if len(times)>=3 else ''
            for code in codes:
                results.append({'id':code,'date':f'2026-10-{last_day:02d}','sport':sport,'venue':venue,'ticket':code,'open':opening,'start':start,'end':end,'source_url':url,'source_asof':'公式会場ページ（ページ上の注記を参照）'})
    uniq={x['id']:x for x in results}
    return list(uniq.values())


def fetch_schedule(session,venues):
    parsed=[];fail=[];ok=0
    def one(source):
        body=request_text(session,source['url'])
        got=parse_venue_sessions(body,source['sport'],source['venue'],source['url'])
        if not got: raise ValueError('会場ページに競技時刻の行が見つかりません')
        return got
    # 18 pages are queried in parallel, avoiding missed runs from serial timeouts.
    with ThreadPoolExecutor(max_workers=6) as pool:
        tasks={pool.submit(one,source):source for source in venues}
        for task in as_completed(tasks):
            source=tasks[task]
            try:
                parsed.extend(task.result());ok+=1
            except Exception as ex:
                fail.append(f"{source['venue']}: {type(ex).__name__} ({str(ex)[:70]})")
    return parsed,ok,sorted(fail)


def candidate_news(posts):
    candidate=[]
    for n in posts:
        headline=n['title']; data=headline+' '+n.get('summary','')
        if not re.search(r'金メダル|金獲得|優勝|アジアの頂点',data): continue
        if not re.search(r'日本|JPN|日本代表|日本勢',data): continue
        if re.search(r'金メダル(?:候補|を目指|を狙|への挑戦)',data): continue
        # Candidate only, not person extraction, no automatic winner certification.
        candidate.append({'id':n['url'],'title':headline,'source_url':n['url'],'published_at':n.get('date',''),'status':'review_required','reason':'公式記事に日本勢と優勝／金メダルに関係する表現があります。結果・選手名・種目を要照合。'})
    return candidate


def source_status(previous, success, attempted_at, method=None, error='', partial=False):
    old=previous or {}
    return {'state':('partial' if partial else 'ok') if success else 'error', 'last_attempt':attempted_at,'last_success':attempted_at if success else old.get('last_success'), 'method':method or old.get('method',''), 'error':error[:1500]}


def schedule_diff(old,new,ts):
    # Keep only the latest 100 changes; first successful import is baseline, not a change.
    if not old: return []
    m={x['id']:x for x in old}; out=[]
    for s in new:
        p=m.get(s['id'])
        if p is None:
            continue # New tickets are shown as new sessions, not assumed to be reschedules.
        fields=('date','open','start','end','venue')
        ch=[key for key in fields if p.get(key)!=s.get(key)]
        if ch:out.append({'id':s['id'],'changed_at_utc':ts,'sport':s['sport'],'changes':{f:{'old':p.get(f,''),'new':s.get(f,'')} for f in ch},'source_url':s['source_url']})
    return out


def run(path=SNAPSHOT_PATH, now=None,session=None,force=False):
    now=now or datetime.now(timezone.utc)
    at=now.isoformat(timespec='seconds').replace('+00:00','Z')
    local=now.astimezone(JST)
    # Calls 00:00 JST cover 24:00 of the previous day.
    eligible=((local.date()>=datetime(2026,10,16).date() and local.date()<=datetime(2026,10,24).date() and 9<=local.hour<24)
            or (local.date()>=datetime(2026,10,17).date() and local.date()<=datetime(2026,10,25).date() and local.hour==0 and local.minute==0))
    if not force and not eligible:
        print(f'Outside JST refresh window: {local.isoformat()} — unchanged')
        return False
    previous=json.loads(path.read_text(encoding='utf-8'))
    snapshot={**previous}
    snapshot['generated_at_utc']=at
    snap_sources=dict(previous.get('sources',{}))
    session=session or requests.Session()
    try:
        news,method=fetch_news(session)
        # Preserve past articles even if upstream only returns recent 20.
        by_url={x['url']:x for x in previous.get('updates',[]) if allowed_official(x.get('url'))}
        for x in news:
            earlier=by_url.get(x['url'],{})
            by_url[x['url']]={**earlier,**{k:v for k,v in x.items() if v},'last_seen_at':at}
        snapshot['updates']=sorted(by_url.values(),key=lambda x:x.get('date',''),reverse=True)[:150]
        snap_sources['news']=source_status(snap_sources.get('news'),True,at,method)
    except Exception as ex:
        snap_sources['news']=source_status(snap_sources.get('news'),False,at,error=f'{type(ex).__name__}: {str(ex)}')
    venues=json.loads(VENUES_PATH.read_text(encoding='utf-8'))
    schedule,successful,failed=fetch_schedule(session,venues)
    if successful:
        prior={s['source_url']:[] for s in previous.get('sessions',[]) if s.get('source_url')}
        for s in previous.get('sessions',[]):prior.setdefault(s['source_url'],[]).append(s)
        current={s['source_url']:[] for s in schedule}
        for s in schedule:current.setdefault(s['source_url'],[]).append(s)
        combined=[]
        for source in venues:
            url=source['url']
            combined.extend(current[url] if url in current and current[url] else prior.get(url,[]))
        combined=sorted(combined,key=lambda s:(s['date'],s.get('start',''),s['sport'],s['ticket']))
        changes=schedule_diff(previous.get('sessions',[]),combined,at)
        all_changes=previous.get('schedule_changes',[]) + changes
        # Dedupe a change only by ticket + exact new value: subsequent reconfirmation isn't a new change.
        dedupe={}
        for change in all_changes:
            k=(change['id'],json.dumps(change['changes'],ensure_ascii=False,sort_keys=True))
            dedupe[k]=change
        snapshot['schedule_changes']=list(dedupe.values())[-100:]
        snapshot['sessions']=combined
        note=f'{successful}/{len(venues)}会場取得。'+('; '.join(failed) if failed else '')
        snap_sources['schedule']=source_status(snap_sources.get('schedule'),True,at,'公式会場別セッション表',error=note,partial=bool(failed))
    else:
        snap_sources['schedule']=source_status(snap_sources.get('schedule'),False,at,error='; '.join(failed))
    snapshot['gold_candidates']=candidate_news(snapshot.get('updates',[]))[:40]
    # Gold medals from result site CANNOT be inferred from news article keywords.
    snapshot['verified_gold']=[]
    snap_sources['results']={'state':'not_connected','last_success':None,'error':'Bornan公式結果の検証済み公開APIが未確認。結果と金メダルは自動確定しません。'}
    snapshot['sources']=snap_sources
    hist=previous.get('run_history',[])
    hist.append({'at':at,'news':snap_sources['news']['state'],'schedule':snap_sources['schedule']['state'],'sessions':len(snapshot.get('sessions',[])),'new_candidates':len(snapshot['gold_candidates'])})
    snapshot['run_history']=hist[-80:]
    path.write_text(json.dumps(snapshot,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'at':at,'jst':local.isoformat(),'news':snap_sources['news']['state'],'schedule':snap_sources['schedule']['state'],'schedule_sessions':len(snapshot.get('sessions',[])),'gold_candidates_unverified':len(snapshot.get('gold_candidates',[]))},ensure_ascii=False))
    return True


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--force',action='store_true',help='Run outside time window (manual dispatch / tests).')
    args=parser.parse_args()
    run(force=args.force)
