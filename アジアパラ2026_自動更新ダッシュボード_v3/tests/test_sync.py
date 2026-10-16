import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from sync import wp_posts, rss_posts, parse_venue_sessions, candidate_news, schedule_diff


def test_wp_results():
    docs=[{'id':2026,'link':'https://www.asianparagames-2026.org/ja/news-2038/','title':{'rendered':'公式 &amp; ニュース'},'date':'2026-10-15T12:00:00','excerpt':{'rendered':'<p>紹介文</p>'}}]
    vals=wp_posts(docs)
    assert vals[0]['title']=='公式 & ニュース'
    assert vals[0]['summary']=='紹介文'


def test_rss():
    body='<rss><channel><item><link>https://www.asianparagames-2026.org/ja/news-2038/</link><title>開会</title><pubDate>2026-10-15T00:00:00Z</pubDate></item></channel></rss>'
    assert len(rss_posts(body))==1


def test_venue_rowspan():
    body='''<table><tr><th>会場</th><th>競技-種別</th><th>日程</th><th>チケットコード</th><th>開場時間</th><th>競技開始時間</th><th>競技終了予定時間</th></tr><tr><td>会場</td><td>水泳</td><td rowspan=2>10/19（月）</td><td>PSW01</td><td>8:00</td><td>10:00</td><td>11:19</td></tr><tr><td>PSW02</td><td>15:00</td><td>17:00</td><td>21:17</td></tr></table>'''
    out=parse_venue_sessions(body,'パラ水泳','レインボープール','https://www.asianparagames-2026.org/ja/venue/pool/')
    assert len(out)==2, out
    assert out[0]['date']=='2026-10-19'
    assert out[1]['date']=='2026-10-19'
    assert out[1]['start']=='17:00'


def test_no_unverified_gold():
    docs=[{'url':'https://www.asianparagames-2026.org/ja/news-99/','title':'日本代表の金メダル獲得を発表','date':'2026-10-19'}]
    got=candidate_news(docs)
    assert got and got[0]['status']=='review_required'


def test_changed_schedule():
    old=[{'id':'PSW01','date':'2026-10-19','start':'09:00'}]
    new=[{'id':'PSW01','date':'2026-10-19','start':'10:00','source_url':'https://www.asianparagames-2026.org/ja/venue/pool/','sport':'水泳'}]
    assert schedule_diff(old,new,'2026-10-16T00:00:00Z')[0]['changes']['start']['new']=='10:00'


def test_english_dates_change_on_each_row():
    body='''<table><tr><th>Venue</th><th>Date</th><th>Ticket Code</th><th>Competition Starts</th></tr><tr><td>Oct. 17 (Sat)</td><td>BCA01</td><td>7:30</td><td>9:30</td><td>20:10</td></tr><tr><td>Oct. 19 (Mon)</td><td>BCA02</td><td>7:30</td><td>9:30</td><td>20:10</td></tr></table>'''
    out=parse_venue_sessions(body,'ボッチャ','会場','https://www.asianparagames-2026.org/venue/xyz/')
    assert [x['date'] for x in out]==['2026-10-17','2026-10-19']


def test_end_to_end_success_and_zero_confirmed_gold(tmp_path, monkeypatch):
    import json
    from datetime import datetime,timezone
    import sync
    snap=tmp_path/'live.json'
    original=json.loads((Path(__file__).resolve().parents[1]/'data/live.json').read_text())
    snap.write_text(json.dumps(original,ensure_ascii=False))
    monkeypatch.setattr(sync,'VENUES_PATH',tmp_path/'venues.json')
    (tmp_path/'venues.json').write_text(json.dumps([{'sport':'パラ水泳','venue':'公式会場','url':'https://www.asianparagames-2026.org/ja/venue/xyz/'}]))
    class DummyResp:
        def __init__(self,text):self.text=text;self.content=text.encode('utf8')
        def raise_for_status(self):pass
    class DummySession:
        def get(self,url,**kwargs):
            if '/wp-json/' in url:
                return DummyResp(json.dumps([{'id':8,'link':'https://www.asianparagames-2026.org/ja/news-9999/','title':{'rendered':'日本代表が金メダルを獲得'},'date':'2026-10-20T18:00:00','excerpt':{'rendered':'<p>速報</p>'}}]))
            return DummyResp('''<table><tr><th>競技開始時間</th></tr><tr><td>10/20</td><td>PSW01</td><td>8:00</td><td>10:00</td><td>11:30</td></tr></table>''')
    sync.run(path=snap, now=datetime(2026,10,20,4,0,tzinfo=timezone.utc),session=DummySession())
    updated=json.loads(snap.read_text())
    assert updated['sources']['news']['state']=='ok'
    assert updated['sources']['schedule']['state']=='ok'
    assert updated['gold_candidates'][0]['status']=='review_required'
    assert updated['verified_gold']==[]
    assert updated['sessions'][0]['start']=='10:00'


def test_window_does_not_poll_before_nine(tmp_path,monkeypatch):
    from datetime import datetime,timezone
    import sync
    path=tmp_path/'live.json';path.write_text('{}')
    # 2026-10-20 23:55 UTC => 2026-10-21 08:55 JST, outside scope.
    assert not sync.run(path=path,now=datetime(2026,10,20,23,55,tzinfo=timezone.utc))
    assert path.read_text()=='{}'
