#!/usr/bin/env python3
"""
드라마/예능 시청률 수집기
- Naver 요일별 검색 -> regex로 드라마명+시청률 추출
- 각 드라마별 Naver 지식패널 -> 채널/방영일/포스터/줄거리
Output: drama_data.json
"""

import json, re, os, urllib.parse, sys
from datetime import datetime

# 스크립트 위치 기준 경로
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
           'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36')

# 시청률 검색 쿼리 (드라마 / 예능)
DRAMA_QUERIES = [
    '월화드라마 시청률',
    '수목드라마 시청률',
    '금토드라마 시청률',
    '주말드라마 시청률',
    '일일드라마 시청률',
]
VARIETY_QUERIES = [
    '주말예능 시청률',
    '평일예능 시청률',
]

# 제목으로 볼 수 없는 필터 패턴
TITLE_BLACKLIST = re.compile(
    r'(위키트리|조이뉴스|OSEN|osen|스포츠|뉴스|기자|시청률이|방송국'
    r'|tvN|SBS|KBS|MBC|JTBC|채널|대타|편성'
    r'|배우|출연|감독|화제|관련|이번|지난|현재|오늘'
    r'|하지만|그리고|그래서|때문에|이라고|라며|에서|라는)',
    re.IGNORECASE
)


def _normalize_title(t):
    """공백 제거 정규화 (중복 감지용)"""
    return re.sub(r'\s+', '', t)


# ── 시청률 수집 ────────────────────────────────────────────────────────────────
def search_ratings(page, query):
    """쿼리로 Naver 검색 -> {제목: 최고시청률}"""
    q = urllib.parse.quote(query)
    page.goto('https://search.naver.com/search.naver?where=nexearch&query=' + q,
              wait_until='domcontentloaded', timeout=20000)
    page.wait_for_timeout(2000)

    text = page.evaluate('() => document.body.innerText')

    results = {}
    # 여는/닫는 따옴표 — chr()로 빌드해 소스에 non-ASCII 없음 (Python 3.12 호환)
    # U+2018/2019=curly-single  U+201C/201D=curly-double
    # U+300C/300D=corner-bracket  U+300E/300F=white-corner-bracket
    OQ = chr(0x2018)+chr(0x2019)+chr(0x201c)+chr(0x201d)+chr(0x300c)+chr(0x300e)
    CQ = chr(0x2018)+chr(0x2019)+chr(0x201c)+chr(0x201d)+chr(0x300d)+chr(0x300f)
    pat = re.compile(
        '[' + OQ + '\"\']([ -~가-힣]{2,25})[' + CQ + '\"\']'
        '[^\n%]{0,40}?(\\d+\\.?\\d*)%'
    )
    for m in pat.finditer(text):
        title = m.group(1).strip()
        pct_s = m.group(2)
        if not pct_s:
            continue
        pct = float(pct_s)
        # 필터링
        if not title or len(title) < 2 or pct < 0.3 or pct > 50:
            continue
        if not re.search(r'[가-힣]', title):
            continue
        if TITLE_BLACKLIST.search(title):
            continue
        # 말줄임표/줄바꿈 포함 제목 제거
        if '…' in title or '...' in title or '\n' in title:
            continue
        results[title] = max(results.get(title, 0), pct)

    return results


def _dedup_ratings(ratings):
    """공백 차이로 인한 중복 제거 — 더 높은 시청률 기준으로 대표 제목 선택"""
    norm_map = {}
    for title, pct in ratings.items():
        key = _normalize_title(title)
        if key not in norm_map or pct > norm_map[key][1]:
            norm_map[key] = (title, pct)
    return {v[0]: v[1] for v in norm_map.values()}


# ── 드라마 상세 정보 수집 ──────────────────────────────────────────────────────
def fetch_drama_info(page, title):
    """Naver 지식패널 -> 채널/방영일/포스터/줄거리/출연진"""
    q = urllib.parse.quote(title + ' 드라마')
    page.goto('https://search.naver.com/search.naver?where=nexearch&query=' + q,
              wait_until='domcontentloaded', timeout=20000)
    page.wait_for_timeout(2000)

    info = page.evaluate(r'''() => {
        const dls = [...document.querySelectorAll('dl')];
        let schedule = '';
        for (const dl of dls) {
            const txt = dl.innerText;
            if (txt.includes('편성') || txt.includes('방송')) {
                schedule = txt.replace('편성','').replace('방송','').trim().slice(0,80);
                break;
            }
        }
        const cast_dl = [...document.querySelectorAll('dl')].find(dl => dl.innerText.includes('출연'));
        const cast_txt = cast_dl ? cast_dl.innerText.replace('출연','').trim() : '';

        const desc = document.querySelector('.desc');

        const imgs = [...document.querySelectorAll('img[src*="pstatic"]')].filter(i =>
            i.alt && i.alt.length > 1 && !i.alt.includes('N페이') && !i.src.includes('gnb')
        );

        return {
            schedule,
            cast_txt,
            synopsis: desc ? desc.textContent.trim().slice(0, 300) : '',
            imgs: imgs.slice(0, 5).map(i => ({ alt: i.alt, src: i.src }))
        };
    }''')

    sched = info.get('schedule', '')
    channel = ''
    air_days = ''
    for ch in ['SBS', 'KBS2', 'KBS1', 'MBC', 'tvN', 'JTBC', 'OCN', 'ENA', 'TV조선', 'MBN', '채널A', 'KBS']:
        if ch in sched:
            channel = ch
            break
    days_m = re.search(r'[(（]([월화수목금토일,· ]+)[)）]', sched)
    if days_m:
        air_days = days_m.group(1).replace(' ', '').replace(',', '·')

    poster_url = ''
    for img in info.get('imgs', []):
        if title[:3] in img['alt'] or img['alt'] in title:
            poster_url = img['src']
            break

    if not poster_url:
        try:
            qt = urllib.parse.quote(title)
            page.goto('https://movie.naver.com/movie/search/result.naver?query=' + qt,
                      wait_until='networkidle', timeout=15000)
            page.wait_for_timeout(700)
            p = page.evaluate(r'''(tgt) => {
                const imgs = [...document.querySelectorAll('img[src*="pstatic"]')].filter(i =>
                    i.alt && i.alt.length > 1 && !i.alt.includes('N'));
                const m = imgs.find(i => tgt.includes(i.alt) || i.alt.includes(tgt.slice(0,3)));
                return m ? m.src : '';
            }''', title)
            if p:
                poster_url = p
        except Exception:
            pass

    return {
        'channel':   channel,
        'air_days':  air_days,
        'schedule':  sched,
        'synopsis':  info.get('synopsis', ''),
        'cast':      [c.strip() for c in re.split(r'[,·\s]+', info.get('cast_txt', ''))
                      if c.strip() and len(c.strip()) > 1][:5],
        'poster_url': poster_url,
    }


# ── 메인 ─────────────────────────────────────────────────────────────────────
def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('[ERROR] playwright 미설치')
        return

    data_path = os.path.join(SCRIPT_DIR, 'drama_data.json')

    # 캐시 로드
    cache = {}
    try:
        with open(data_path, encoding='utf-8') as f:
            prev = json.load(f)
        for item in prev.get('dramas', []) + prev.get('variety', []):
            t = item.get('title', '')
            if t:
                cache[t] = {k: item[k] for k in ('channel', 'air_days', 'synopsis', 'poster_url', 'cast', 'schedule') if k in item}
        print('[Cache] {}편 로드'.format(len(cache)).encode('utf-8', errors='replace').decode('ascii', errors='replace'))
    except Exception:
        pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=['--no-sandbox'])
        page = browser.new_page(user_agent=BASE_UA)

        # ── 드라마 시청률 수집 ──────────────────────────────────────────────
        drama_ratings_raw = {}
        for query in DRAMA_QUERIES:
            r = search_ratings(page, query)
            for t, pct in r.items():
                drama_ratings_raw[t] = max(drama_ratings_raw.get(t, 0), pct)
            print('[{}] {}편'.format(query, len(r)).encode('utf-8', errors='replace').decode('ascii', errors='replace'))
        drama_ratings = _dedup_ratings(drama_ratings_raw)

        # ── 예능 시청률 수집 ────────────────────────────────────────────────
        variety_ratings_raw = {}
        for query in VARIETY_QUERIES:
            r = search_ratings(page, query)
            for t, pct in r.items():
                variety_ratings_raw[t] = max(variety_ratings_raw.get(t, 0), pct)
            print('[{}] {}편'.format(query, len(r)).encode('utf-8', errors='replace').decode('ascii', errors='replace'))

        # 예능에서 드라마 제목 제거
        drama_normalized = {_normalize_title(t) for t in drama_ratings}
        variety_ratings = _dedup_ratings({
            t: p for t, p in variety_ratings_raw.items()
            if _normalize_title(t) not in drama_normalized
        })

        # ── 상세 정보 enrichment ────────────────────────────────────────────
        def enrich(title_pct_map, label):
            items = sorted(title_pct_map.items(), key=lambda x: -x[1])[:12]
            result = []
            for rank, (title, rating) in enumerate(items, 1):
                print('[{}] {}. {} ({}%)'.format(
                    label, rank, title, rating
                ).encode('utf-8', errors='replace').decode('ascii', errors='replace'))
                info = dict(cache.get(title, {}))
                if not info.get('synopsis') or not info.get('channel'):
                    fetched = fetch_drama_info(page, title)
                    for k, v in fetched.items():
                        if v:
                            info[k] = v
                    cache[title] = info
                result.append({
                    'rank':       rank,
                    'title':      title,
                    'rating':     rating,
                    'channel':    info.get('channel', ''),
                    'air_days':   info.get('air_days', ''),
                    'synopsis':   info.get('synopsis', ''),
                    'cast':       info.get('cast', []),
                    'poster_url': info.get('poster_url', ''),
                })
            return result

        dramas  = enrich(drama_ratings,  '드라마')
        variety = enrich(variety_ratings, '예능')

        browser.close()

    result = {
        'updated': datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'dramas':  dramas,
        'variety': variety,
    }

    with open(data_path, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    total = dramas + variety
    print('저장 완료: 드라마 {}편 / 예능 {}편'.format(len(dramas), len(variety)).encode('utf-8', errors='replace').decode('ascii', errors='replace'))
    print('포스터: {} / 줄거리: {}'.format(
        sum(1 for d in total if d.get('poster_url')),
        sum(1 for d in total if d.get('synopsis'))
    ))


if __name__ == '__main__':
    main()
