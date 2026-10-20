import json as _json
import re
import time
import random
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode as _urlencode

import requests
from bs4 import BeautifulSoup

# ── 공통 세션 ─────────────────────────────────────────────────────────────────

# 2026-09-12 실측: 네이버가 TLS 지문(JA3)으로 봇을 거른다. 일반 requests(OpenSSL)
# 핸드셰이크는 헤더를 크롬과 똑같이 맞춰도 403 '검색 서비스 이용이 제한' 페이지를 받는다.
# curl_cffi 로 크롬 TLS 지문을 흉내내야 통과한다. (크롬·curl 은 정상 통과)
try:
    from curl_cffi import requests as _creq
except Exception:          # 없으면 기존 requests 로 폴백 (차단될 수 있음)
    _creq = None

_HEADERS = {
    'Accept-Language': 'ko-KR,ko;q=0.9',
    'Referer': 'https://search.naver.com/',
}
# requests 폴백일 때만 UA 를 직접 넣는다.
# curl_cffi 는 impersonate 가 정한 UA 를 그대로 써야 TLS 지문과 어긋나지 않는다.
_FALLBACK_UA = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
)


def _mk_session():
    if _creq is not None:
        s = _creq.Session(impersonate='chrome')
        s.headers.update(_HEADERS)
    else:
        s = requests.Session()
        s.headers.update(dict(_HEADERS, **{'User-Agent': _FALLBACK_UA}))
    return s


_SESSION = _mk_session()

_session_ready = False

# 실측(2026-09-07): 검색 요청은 세션(쿠키)당 100회가 상한이고, 새 세션이면
# 즉시 풀린다. 상한에 닿기 전에 미리 갈아탄다.
ROTATE_AT = 80
# 실측(2026-09-07)
#  · HTTP 403 등급 = 쿠키(세션) 단위. 새 세션이면 대개 즉시 통과한다.
#  · 캡차 안내 페이지 등급 = 세션을 갈아타도 안 풀린다. 몇 분을 기다려야 한다.
FAST_WAITS = (0, 0, 5, 20, 60, 180)          # 403 등급
CAPTCHA_WAITS = (180, 300, 600)              # 캡차 등급 — 3분 → 5분 → 10분

SHOULD_STOP = None      # gui 가 중지 플래그를 꽂아 쓴다

# 네이버 허용치는 고정이 아니다. 최근에 많이 쓴 IP 는 같은 딜레이로도 계속 막힌다.
# 그래서 막히면 스스로 느려지고, 한동안 잘 되면 다시 조금씩 빨라진다.
THROTTLE_STEP = 0.4     # 막힐 때마다 이만큼 더 쉰다(초)
THROTTLE_MAX = 6.0
THROTTLE_OK = 25        # 연속 성공이 이만큼이면 한 칸 줄인다
_extra = 0.0
_ok_run = 0


def reset_throttle():
    """조회를 새로 시작할 때 호출."""
    global _extra, _ok_run
    _extra = 0.0
    _ok_run = 0


def _sleep(sec: float) -> bool:
    """중지를 누르면 곧바로 깨어난다. 계속해도 되면 True."""
    end = time.time() + sec
    while True:
        left = end - time.time()
        if left <= 0:
            return True
        if SHOULD_STOP and SHOULD_STOP():
            return False
        time.sleep(min(1.0, left))

_req_count = 0
NOTIFY = None          # gui 가 로그 함수를 꽂아 쓴다


def _say(msg):
    if NOTIFY:
        try:
            NOTIFY(msg)
        except Exception:
            pass

BLOG_PAT     = re.compile(r'blog\.naver\.com/([a-zA-Z0-9_.-]+)/(\d+)')
_RSS_ID_PAT  = re.compile(r'rss\.blog\.naver\.com/([a-zA-Z0-9_.-]+)')
_resolve_cache: dict = {}


class BotBlockedError(Exception):
    """네이버 봇 감지로 검색이 일시 차단된 상태.

    captcha=True 는 캡차 안내 페이지(HTTP 200) 등급. 실측상 이 등급은
    세션을 갈아타도 안 풀리고 몇 분을 기다려야 한다.
    captcha=False 는 HTTP 403 등급 — 새 세션이면 대개 바로 풀린다.
    """

    def __init__(self, msg, captcha=False):
        super().__init__(msg)
        self.captcha = captcha


# 차단 페이지 안내 문구 (무딜레이 연타 약 10회 → HTTP 403 + 캡차 페이지 실측)
# 정상 페이지에도 'captcha' 단어는 CSS에 존재하므로 문구/상태코드로만 판정
_BOT_BLOCK_MARKERS = ('이용이 제한되었습니다', '일시적으로 제한', '비정상적인 검색')

# 문구만 보면 오탐한다 — 실제로 '수영은 일시적으로 제한됩니다' 같은 글 본문이
# 검색 결과에 섞여 멀쩡한 페이지를 차단으로 오인한 사례가 있었다(2026-09-07).
# 진짜 차단 페이지는 403 에 28KB 남짓이고 검색 결과가 하나도 없다.
_RESULT_HINT = 'sds-comps-text-type-headline1'


def _check_bot_block(resp):
    """검색 응답이 봇 차단 페이지면 BotBlockedError 발생."""
    if resp.status_code in (403, 429):
        raise BotBlockedError(f'네이버 봇 차단 (HTTP {resp.status_code})')
    if resp.status_code == 200:
        body = resp.text
        # 검색 결과가 들어 있으면 차단 페이지가 아니다
        if _RESULT_HINT in body:
            return
        if any(m in body for m in _BOT_BLOCK_MARKERS):
            raise BotBlockedError('네이버 봇 차단 (캡차 안내 페이지)', captcha=True)


def _init_session():
    """예전에는 새 세션마다 네이버 메인·검색을 한 번씩 찍어 쿠키를 받아왔다.
    2026-09-07 실측 결과 세 유형 모두 워밍업 없이도 첫 요청이 정상 통과했고,
    오히려 세션을 갈아탈 때마다 연속 요청 2개가 나가 차단을 다시 부르고 있었다.
    그래서 지금은 아무것도 하지 않는다(호출부 호환을 위해 남겨 둔다)."""
    global _session_ready
    _session_ready = True


def _rotate_session():
    """쿠키를 버리고 새 세션으로. 세션당 요청 한도가 여기서 리셋된다."""
    global _SESSION, _session_ready, _req_count
    _SESSION = _mk_session()
    _session_ready = True
    _req_count = 0


def _fetch(url: str, params: dict, timeout: int = 15, session=None):
    """검색 요청 한 번. 상한이 가까우면 미리 세션을 갈아타고,
    막히면 쉬었다가 새 세션으로 다시 시도한다.
    session 을 주면(성인인증 로그인 세션) 그 세션으로만 보내고 교체하지 않는다."""
    global _req_count, _extra, _ok_run
    fast_i = cap_i = 0
    while True:
        _init_session()
        if session is None and _req_count >= ROTATE_AT:
            _say(f"  세션 교체 (요청 {_req_count}회)")
            _rotate_session()
        if _extra and not _sleep(_extra):
            raise BotBlockedError('중지됨')
        try:
            resp = (session or _SESSION).get(url, params=params, timeout=timeout)
            if session is None:
                _req_count += 1
            _check_bot_block(resp)
            resp.raise_for_status()
            _ok_run += 1
            if _extra and _ok_run >= THROTTLE_OK:
                _ok_run = 0
                _extra = max(0.0, round(_extra - THROTTLE_STEP, 1))
                _say(f"  잘 나가는 중 — 추가 대기를 {_extra:.1f}초로 줄임")
            return resp
        except BotBlockedError as e:
            _ok_run = 0
            if _extra < THROTTLE_MAX:
                _extra = round(_extra + THROTTLE_STEP, 1)
                _say(f"  막혀서 속도를 낮춤 — 요청마다 {_extra:.1f}초 더 쉼")
            if e.captcha:
                if cap_i >= len(CAPTCHA_WAITS):
                    raise
                wait = CAPTCHA_WAITS[cap_i]
                cap_i += 1
                _say(f"  캡차 차단 — {wait // 60}분 쉬었다 재개 "
                     f"({cap_i}/{len(CAPTCHA_WAITS)})")
            else:
                if fast_i >= len(FAST_WAITS):
                    raise
                wait = FAST_WAITS[fast_i]
                fast_i += 1
                _say(f"  차단 감지 — {'세션 교체 후 재시도' if not wait else str(wait) + '초 쉬고 세션 교체'} "
                     f"({fast_i}/{len(FAST_WAITS)})")
            if not _sleep(wait if wait else 1.0):
                raise                      # 중지를 누른 경우
            _rotate_session()


def random_delay(start: int, end: int):
    """0.1초 단위 랜덤 딜레이."""
    lo = max(0, min(start, end))
    hi = max(lo, end)
    if hi > 0:
        time.sleep(random.uniform(lo, hi) * 0.1)


# ── RSS 기반 글 목록 ──────────────────────────────────────────────────────────

def _parse_rss_date(s: str) -> str:
    try:
        return parsedate_to_datetime(s).strftime('%Y-%m-%d')
    except Exception:
        m = re.search(r'(\d{1,2})\s+(\w{3})\s+(\d{4})', s)
        if m:
            months = {'Jan':1,'Feb':2,'Mar':3,'Apr':4,'May':5,'Jun':6,
                      'Jul':7,'Aug':8,'Sep':9,'Oct':10,'Nov':11,'Dec':12}
            d, mo, y = int(m.group(1)), months.get(m.group(2), 1), int(m.group(3))
            return f'{y:04d}-{mo:02d}-{d:02d}'
        return ''


def get_blog_posts(blog_id: str, count: int = 3, skip_first: bool = True) -> list:
    """RSS로 최근 글 목록 반환. [{'title', 'date', 'logNo'}, ...]"""
    try:
        resp = _SESSION.get(f'https://rss.blog.naver.com/{blog_id}.xml', timeout=10)
        resp.raise_for_status()
        try:
            soup = BeautifulSoup(resp.content, 'lxml-xml')
        except Exception:
            soup = BeautifulSoup(resp.content, 'xml')

        items = soup.find_all('item')
        if skip_first and items:
            items = items[1:]

        result = []
        for item in items[:count]:
            title_t = item.find('title')
            link_t  = item.find('link')
            date_t  = item.find('pubDate')

            title  = title_t.get_text(strip=True) if title_t else ''
            link   = link_t.get_text(strip=True)  if link_t  else ''
            m = re.search(r'/(\d+)\?', link)
            log_no = int(m.group(1)) if m else 0
            date   = _parse_rss_date(date_t.get_text(strip=True)) if date_t else ''

            result.append({'title': title, 'date': date, 'logNo': log_no})
        return result
    except Exception:
        return []


def get_blog_info(blog_id: str) -> dict:
    """실제 총 게시글 수(m.blog 페이지), 최초/최종 날짜(RSS) 반환."""
    total = 0
    first_date = ''
    last_date = ''

    # 총 게시글: 모바일 블로그 메인에서 postCount 추출
    try:
        resp = _SESSION.get(f'https://m.blog.naver.com/{blog_id}', timeout=10)
        m = re.search(r'"postCount"\s*:\s*(\d+)', resp.text)
        if m:
            total = int(m.group(1))
    except Exception:
        pass

    # 최초/최종 날짜: RSS 사용 (최근 50개 기준)
    try:
        resp = _SESSION.get(f'https://rss.blog.naver.com/{blog_id}.xml', timeout=10)
        resp.raise_for_status()
        try:
            soup = BeautifulSoup(resp.content, 'lxml-xml')
        except Exception:
            soup = BeautifulSoup(resp.content, 'xml')

        items = soup.find_all('item')
        if items:
            def _d(item):
                t = item.find('pubDate')
                return _parse_rss_date(t.get_text(strip=True)) if t else ''
            last_date  = _d(items[0])
            first_date = _d(items[-1])
    except Exception:
        pass

    return {'total': total, 'first_date': first_date, 'last_date': last_date}


# ── 검색 순위 ─────────────────────────────────────────────────────────────────

def _extract_from_json(obj, posts: list, seen: set, depth: int = 0):
    """JSON 트리를 순회하며 blog URL을 추출. 배열 순서 유지."""
    if depth > 20:
        return
    if isinstance(obj, list):
        for item in obj:
            _extract_from_json(item, posts, seen, depth + 1)
    elif isinstance(obj, dict):
        for field in ('url', 'link', 'href', 'pcLink', 'mLink', 'blogUrl', 'postUrl'):
            val = obj.get(field)
            if isinstance(val, str):
                m = BLOG_PAT.search(val)
                if m:
                    key = (m.group(1).lower(), m.group(2))
                    if key not in seen:
                        seen.add(key)
                        posts.append(key)
                    return  # 이 dict에서 URL 하나만 추출
        for v in obj.values():
            _extract_from_json(v, posts, seen, depth + 1)


def _unwrap_search_html(text: str, search_type: str) -> str:
    """인기글 API JSON 응답에서 실제 검색결과 HTML만 추출. 실패 시 원문 반환."""
    if search_type == '인기글':
        try:
            data = _json.loads(text)
            return data['dom']['collection'][0]['html']
        except Exception:
            pass
    return text



_AD_CLASS_KEYWORDS = ('type_ad', 'is_ad', 'ad_area', 'sp_ad', 'advert', 'is_paid', 'paid_')


def _is_ad(link) -> bool:
    """링크가 속한 결과 카드(li/article) 안에만 광고 표시가 있는지 확인."""
    # 결과 카드 찾기 — 상위 전체 목록 컨테이너까지 올라가면 다른 카드의
    # 광고 텍스트까지 감지되므로 li/article 수준에서 멈춤
    card = None
    el = link.parent
    for _ in range(10):
        if not el or el.name in (None, 'html', 'body'):
            break
        if el.name in ('li', 'article'):
            card = el
            break
        el = el.parent

    if card is None:
        return False

    # 카드 자체 class에 광고 키워드
    classes = ' '.join(card.get('class') or []).lower()
    if any(kw in classes for kw in _AD_CLASS_KEYWORDS):
        return True

    # 카드 내부에만 "광고" 텍스트 노드 탐색
    if card.find(string=re.compile(r'^\s*광고\s*$')):
        return True

    return False


_HAS_RESULT_LINK = re.compile(r'(blog|cafe)\.naver\.com')
_CAFE_PAT        = re.compile(r'cafe\.naver\.com')
_CAFE_POST_PAT   = re.compile(r'cafe\.naver\.com/[^/]+/\d+')
# 2026-10-15 통합검색 개편: 결과 한 건 = 종류별 상자 하나(한 쪽 20칸).
# 제목 span 의 class 가 난독화(매번 바뀜)돼서 headline1 로는 못 잡는다.
_UR_ITEM_CLASSES = (
    'fds-ugc-ur-desk',                    # 블로그·카페
    'fds-web-doc-root',                   # 웹사이트
    'fds-kin-ur-desk',                    # 지식인
    'fds-video-item-2line-profile-desk',  # 동영상
)
_AD_TEXT_PAT     = re.compile(r'^\s*광고\s*$')


def _extract_posts(html: str, search_type: str = '') -> list:
    """
    HTML에서 (blog_id_lower, logno) 순서 추출.
    인기글: fds-ugc-single-intention-item-list 컨테이너 기반으로
            블로그(headline1 있는 것만)·카페 모두 순위 위치에 포함, 광고만 제외.
            headline1 없는 블로그 링크(서브링크)는 순위 위치에서 제외.
    블로그탭: headline1 span 기준, regex fallback.
    """
    posts = []
    seen  = set()
    soup  = BeautifulSoup(html, 'lxml')

    if search_type == '인기글':
        container = soup.find(
            'div',
            class_=lambda c: c and 'fds-ugc-single-intention-item-list' in c,
        )
        if container:
            for child in container.children:
                if not hasattr(child, 'find'):
                    continue
                # 카드 내부에서만 광고 체크
                if child.find(string=re.compile(r'^\s*광고\s*$')):
                    continue  # 광고는 순위에서 제외
                # 메인 블로그 결과: headline1을 포함하는 블로그 링크
                blog_link_h1 = None
                for a in child.find_all('a', href=BLOG_PAT):
                    if a.find(
                        'span',
                        class_=lambda c: c and 'sds-comps-text-type-headline1' in c,
                    ):
                        blog_link_h1 = a
                        break
                if blog_link_h1:
                    m = BLOG_PAT.search(blog_link_h1.get('href', ''))
                    if m:
                        key = (m.group(1).lower(), m.group(2))
                        if key not in seen:
                            seen.add(key)
                            posts.append(key)
                        else:
                            posts.append(('', ''))  # 중복 블로그 — 위치 유지
                    else:
                        posts.append(('', ''))
                elif child.find('a', href=_CAFE_PAT):
                    posts.append(('', ''))  # 카페 결과 — 위치 유지
                # headline1 없는 블로그 링크만 있으면 서브링크 → 건너뜀
            if posts:
                return posts

    if search_type == '신뢰도':
        # 2026-10-15 개편: 결과 한 건이 종류별 상자 하나로 나온다(한 쪽 20칸).
        # 상자를 문서 순서대로 세면 화면 순위와 같다.
        # 블로그 → 결과 추가, 카페·웹사이트·지식인·동영상 → 자리만 차지,
        # 광고 → 순위에서 제외.
        cards, taken = [], []
        for nd in soup.find_all(
            lambda t: t.has_attr('class')
            and any(k in t['class'] for k in _UR_ITEM_CLASSES)
        ):
            if any(nd in t.descendants for t in taken):  # 겹친 바깥 껍데기는 한 번만
                continue
            taken.append(nd)
            cards.append(nd)
        if cards:
            for card in cards:
                if card.find(string=_AD_TEXT_PAT):
                    continue
                key = ('', '')
                for a in card.find_all('a', href=True):
                    href = a['href']
                    m = BLOG_PAT.search(href)
                    if m:
                        key = (m.group(1).lower(), m.group(2))
                        break
                    if _CAFE_POST_PAT.search(href):
                        break          # 카페 — 자리만 차지
                if key[0] and key in seen:
                    posts.append(('', ''))   # 같은 글 재노출 — 자리만
                elif key[0]:
                    seen.add(key)
                    posts.append(key)
                else:
                    posts.append(('', ''))
            return posts

        # 폴백: 카드가 없는 옛 화면 — 예전처럼 headline1 링크를 센다
        seen_hrefs: set = set()
        for link in soup.find_all('a'):
            href = link.get('href', '')
            if not href or href in seen_hrefs:
                continue
            title_span = link.find(
                'span',
                class_=lambda c: c and 'sds-comps-text-type-headline1' in c,
            )
            if title_span is None:
                continue
            seen_hrefs.add(href)
            m = BLOG_PAT.search(href)
            if m:
                key = (m.group(1).lower(), m.group(2))
                if key not in seen:
                    seen.add(key)
                    posts.append(key)
                else:
                    posts.append(('', ''))  # 중복 블로그 — 위치 유지
            else:
                posts.append(('', ''))  # 카페/기타 결과 — 위치 유지
        return posts

    # 블로그 탭: headline1 기반
    for link in soup.find_all('a', href=BLOG_PAT):
        href = link.get('href', '')
        m = BLOG_PAT.search(href)
        if not m:
            continue
        title_span = link.find(
            'span',
            class_=lambda c: c and 'sds-comps-text-type-headline1' in c,
        )
        if title_span is None:
            continue
        if _is_ad(link):
            continue
        key = (m.group(1).lower(), m.group(2))
        if key not in seen:
            seen.add(key)
            posts.append(key)
        else:
            posts.append(('', ''))   # 중복 글 — 순위 자리는 그대로 차지한다

    if not posts:
        for bid, logno in BLOG_PAT.findall(html):
            key = (bid.lower(), logno)
            if key not in seen:
                seen.add(key)
                posts.append(key)

    return posts


def resolve_blog_id(login_id: str) -> str:
    """RSS URL 리다이렉트를 이용해 실제 블로그 아이디 반환. 캐시 적용."""
    key = login_id.lower()
    if key in _resolve_cache:
        return _resolve_cache[key]

    resolved = key
    try:
        resp = _SESSION.get(
            f'https://rss.blog.naver.com/{login_id}.xml',
            timeout=10,
            allow_redirects=True,
        )
        m = _RSS_ID_PAT.search(resp.url)
        if m:
            candidate = re.sub(r'\.xml$', '', m.group(1).lower())
            if candidate and candidate != key:
                resolved = candidate
    except Exception:
        pass

    _resolve_cache[key] = resolved
    return resolved


def is_blog_private(blog_id: str) -> bool:
    """블로그 비공개 여부 확인. True = 비공개."""
    try:
        resp = _SESSION.get(
            f'https://blog.naver.com/{blog_id}',
            timeout=10,
            allow_redirects=True,
        )
        if resp.status_code == 403:
            return True
        if '비공개 블로그입니다' in resp.text:
            return True
        return False
    except Exception:
        return False


# 첫 페이지가 "꽉 찬" 개수. 이보다 적게 오면 그게 그 제목의 전체 결과 수다.
# 블로그는 항상 30개, 인기글은 29~30개로 조금 흔들려서 29를 기준으로 둔다.
FULL_PAGE = {'블로그': 30, '인기글': 29}


# ── 성인 키워드 (KeywordManager 와 같은 방식) ────────────────────────────────
# 로그인 안 하면 결과 대신 이 문구만 나온다(블로그·통합검색 모두 0칸).
_ADULT_MARK = '청소년에게 노출하기 부적합한 검색결과를 제외'
# 옛 문구 판정(폴백용) — 띄어쓰기가 달라질 때를 대비해 남겨 둔다.
ADULT_PAT = re.compile(r'청소년에게\s*노출하기\s*부적합')
_ADULT_SESSION = None     # 성인인증 계정 쿠키를 담은 전용 세션 — 일반 검색과 안 섞이게
last_was_adult = False    # 방금 조회한 제목이 성인 키워드였나


def _adult_blocked(html: str, search_type: str) -> bool:
    """'청소년 제외' 문구가 있고 결과가 0칸일 때만 막힌 것으로 본다.
    (로그인돼 결과가 나오는 화면에도 문구가 섞여 있어 문구만으로는 판정하지 않는다)"""
    if _ADULT_MARK not in html and not ADULT_PAT.search(html):
        return False
    return not _extract_posts(_unwrap_search_html(html, search_type), search_type)


def _is_adult_filtered(html: str) -> bool:
    """옛 이름 유지(문구만 보는 판정) — 새 코드는 _adult_blocked 를 쓴다."""
    return bool(ADULT_PAT.search(html))


# 수집기 서버(성인 쿠키가 있는 PC)에 '이 주소 좀 대신 받아 줘' 하고 부탁하는 길.
# 밖에서는 이미 열린 클로드 서버(8787)의 /ad/ 전달 길로만 닿는다.
ADULT_SERVERS = ['121.179.0.1:8787/ad', '127.0.0.1:8792']
_adult_server_ok = None      # 한 번 통한 주소를 기억 (매번 다 두드리지 않게)


def set_adult_servers(addrs):
    """설정으로 주소를 바꿀 수 있게."""
    global ADULT_SERVERS, _adult_server_ok
    if addrs:
        ADULT_SERVERS = list(addrs)
        _adult_server_ok = None


def _fetch_via_server(url: str) -> str:
    """성인 쿠키를 가진 수집기 서버가 대신 받아 준 화면을 가져온다.
    순위를 세는 일은 여기(부탁한 쪽)서 그대로 한다.
    못 닿으면 '' 를 돌려준다(그러면 호출부가 '성인'으로 표시한다)."""
    global _adult_server_ok
    import urllib.parse as _up
    order = ([_adult_server_ok] if _adult_server_ok else []) + \
            [a for a in ADULT_SERVERS if a != _adult_server_ok]
    for addr in order:
        try:
            ask = f'http://{addr}/get?url=' + _up.quote(url, safe='')
            r = requests.get(ask, timeout=60)
            if r.status_code != 200:
                continue
            d = _json.loads(r.text)
            if d.get('ok') and d.get('html'):
                _adult_server_ok = addr
                return d['html']
        except Exception:
            continue
    return ''


def load_adult_cookie(base_dir) -> dict:
    """KM 과 같은 파일(.claude_api.env)의 NAVER_NID_AUT/NAVER_NID_SES 를 읽는다."""
    import os
    out = {}
    try:
        with open(os.path.join(base_dir, '.claude_api.env'), encoding='utf-8') as f:
            for line in f:
                k, _, v = line.strip().partition('=')
                if k in ('NAVER_NID_AUT', 'NAVER_NID_SES') and v.strip():
                    out[k[6:]] = v.strip()          # NID_AUT / NID_SES
    except Exception:
        pass
    return out if len(out) == 2 else {}


def set_adult_cookie(cookies: dict):
    """성인 키워드용 로그인 세션 준비 (빈 dict 면 끔)."""
    global _ADULT_SESSION
    if not cookies:
        _ADULT_SESSION = None
        return
    sess = _mk_session()
    for k, v in cookies.items():
        sess.cookies.set(k, v, domain='.naver.com')
    _ADULT_SESSION = sess


def has_adult_cookie() -> bool:
    return _ADULT_SESSION is not None


def _result_count(html: str, search_type: str) -> int:
    """그 검색어의 실제 결과 수. 블로그탭은 결과가 0이어도 관련 글 링크가 남아 있어서
    _extract_posts 의 정규식 폴백이 부풀리므로 headline1 개수로만 센다."""
    if search_type == '블로그':
        soup = BeautifulSoup(html, 'lxml')
        return len(soup.find_all(
            'span', class_=lambda c: c and 'sds-comps-text-type-headline1' in c))
    return -1   # 그 외는 호출부에서 _extract_posts 결과 길이를 쓴다


def search_rank(
    title: str,
    blog_id: str,
    max_rank: int = 10,
    search_type: str = '블로그',
    extra_ids: set = None,
    page_delay: tuple = (3, 7),
    log_no=None,
    adult: bool = False,
) -> tuple:
    """
    네이버에서 title 검색 후 그 글의 순위를 반환.
    extra_ids: 원본/리다이렉트 아이디 등 추가로 매칭할 아이디 집합.
    log_no: 찾는 글의 글번호. 주면 '그 글' 만 순위로 인정한다. 안 주면
            예전처럼 같은 아이디의 아무 글이나 맞다고 본다(하위 호환).
    Returns: (rank, hits, adult)
      rank  : 0 이면 max_rank 안에 없음
      hits  : 첫 페이지가 꽉 차지 않았을 때 그 제목의 전체 결과 수 (아니면 None)
      adult : 성인 인증이 필요해 결과를 못 본 경우 True
              (성인인증 쿠키가 있으면 그 세션으로 다시 조회해 실제 순위를 낸다)
    adult(인자): 이미 성인 키워드로 알려진 제목이면 True — 처음부터 로그인 세션으로 간다.
    """
    global last_was_adult
    _init_session()
    hits = None       # 첫 페이지가 꽉 차지 않았을 때만 채운다(0 도 유효한 값)
    last_was_adult = bool(adult)
    adult_blocked = False
    blog_ids = {blog_id.lower()}
    if extra_ids:
        blog_ids.update(bid.lower() for bid in extra_ids)
    # 아이디만 맞으면 잡히던 문제: 검색한 제목의 글은 누락됐는데 같은 블로그의
    # 다른 글이 떠 있어도 순위로 세어졌다. 글번호까지 같아야 그 글로 본다.
    want_log = str(log_no) if log_no else ''
    # 한 번 요청에 실제로 몇 개가 오는지는 검색유형·키워드마다 다르다(블로그 30,
    # 인기글 29~30, 신뢰도 20 안팎). 받은 개수만큼 start 를 밀어서 페이지를 넘긴다.
    start = 1
    first = True
    while start <= max_rank:
        try:
            if search_type == '인기글':
                url = 'https://s.search.naver.com/p/review/50/search.naver'
                params = {'query': title, 'ssc': 'tab.itb.all',
                          'sm': 'tab_hty.brg', 'start': start, 'api_type': 5}
            elif search_type == '신뢰도':
                # page 파라미터는 효과 없음 — start만 결과를 제어
                # start=1 → 1위~20위, start=21 → 21위~40위, ..., start=161 → 161위~180위
                url = 'https://search.naver.com/search.naver'
                params = {'query': title, 'ssc': 'tab.ur.all',
                          'sm': 'tab_pge', 'start': start}
            else:  # 블로그
                url = 'https://search.naver.com/search.naver'
                params = {'ssc': 'tab.blog.all', 'sm': 'tab_opt',
                          'query': title, 'start': start}
            # 성인 키워드로 알려진 것은 처음부터 로그인 세션으로
            use_login = last_was_adult and _ADULT_SESSION is not None
            resp = _fetch(url, params, session=_ADULT_SESSION if use_login else None)
            html = resp.text
            if _adult_blocked(html, search_type):
                last_was_adult = True
                if not use_login and _ADULT_SESSION is not None:
                    # 이 PC 에 쿠키가 있으면(드묾) 로그인 세션으로 다시
                    resp = _fetch(url, params, session=_ADULT_SESSION)
                    html = resp.text
                if _adult_blocked(html, search_type):
                    # 쿠키는 이 PC 에 두지 않는다 — 쿠키를 가진 수집기 서버에 대신 부탁
                    got = ''
                    if 'search.naver.com/search.naver' in url:   # 서버가 받아 주는 주소만
                        got = _fetch_via_server(
                            url + '?' + _urlencode(params))
                    if got and not _adult_blocked(got, search_type):
                        html = got
                        _say('  성인 키워드 — 수집기 서버가 대신 받아 줌')
                    else:
                        adult_blocked = True   # 끝까지 못 봄 → '성인'으로 표시
        except BotBlockedError:
            raise
        except Exception:
            break

        posts = _extract_posts(_unwrap_search_html(html, search_type), search_type)

        if first:
            first = False
            page_html = _unwrap_search_html(html, search_type)
            full = FULL_PAGE.get(search_type)
            if full:
                n = _result_count(page_html, search_type)
                if n < 0:
                    n = len(posts)
                if n < full:
                    hits = n

        for i, (bid, logno) in enumerate(posts, start):
            if i > max_rank:
                return 0, hits, adult_blocked
            if bid in blog_ids and (not want_log or logno == want_log):
                return i, hits, adult_blocked

        if not posts:
            break
        # 꽉 찬 페이지인지는 화면에 실제로 걸린 결과 수로 본다.
        # 광고는 순위에서 빼므로 len(posts) 로 재면 꽉 찬 페이지도 모자라 보인다.
        page_n = _result_count(_unwrap_search_html(resp.text, search_type), search_type)
        if page_n < 0:
            page_n = len(posts)
        # (신뢰도는 슬롯 수가 유동적이라 제외)
        if search_type != '신뢰도' and page_n < FULL_PAGE.get(search_type, 30):
            break
        start += len(posts)

        # 같은 제목의 다음 페이지를 부르는 딜레이(브라우저에서 스크롤에 해당).
        # 제목과 제목 사이 딜레이와 네이버가 보는 기준이 달라 따로 둔다.
        random_delay(page_delay[0], page_delay[1])

    return 0, hits, adult_blocked
