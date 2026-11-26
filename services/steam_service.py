import logging
import os
import re
import time
import requests
from urllib.parse import quote as url_quote

logger = logging.getLogger(__name__)

_STEAM_API_URL = "https://store.steampowered.com/api/appdetails"
_STORE_BASE_URL = "https://store.steampowered.com/app/{app_id}/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
_AGE_COOKIES = {"birthtime": "0", "mature_content": "1", "wants_mature_content": "1"}


def fetch_game_data(app_id: str) -> dict | None:
    """Fetch game data from the Steam storefront API."""
    try:
        resp = requests.get(
            _STEAM_API_URL,
            params={"appids": app_id, "l": "english"},
            headers=_HEADERS,
            timeout=15,
        )
        if resp.status_code != 200:
            logger.error(f"[Steam] API returned {resp.status_code} for app {app_id}")
            return None
        data = resp.json()
        if not data.get(app_id, {}).get("success"):
            logger.warning(f"[Steam] API reported no success for app {app_id}")
            return None
        return data[app_id]["data"]
    except Exception as e:
        logger.error(f"[Steam] Failed to fetch game data for app {app_id}: {e}")
        return None


def scrape_steam_page(app_id: str, game_name: str, api_data: dict | None = None) -> tuple[str, list[str]]:
    """
    Fetch YouTube trailer URL and related game name candidates from Steam's
    'morelike' recommendations page. Returns (youtube_url, raw_related_names).
    api_data (the storefront API record) tells how well-known the game is, which steers the trailer search.
    """
    youtube_url = _scrape_youtube(app_id, game_name, _is_lesser_known(api_data))
    related = _fetch_related_games(app_id, game_name)
    return youtube_url, related


# Lesser-known = fewer Steam reviews than this (or none yet: unreleased games).
_KNOWN_GAME_REVIEWS = 10000
# Trailer channels preferred for lesser-known games, best first (exact channel names, lowercase).
_TRAILER_CHANNELS = ("ign", "gametrailers", "gamespot")
# PlayStation channels are skipped: their trailers are PS5/PS4 versions, not what we run.
_PS_CHANNEL_RE = re.compile(r"playstation|\bps\s?[345]\b", re.I)


def _is_lesser_known(api_data: dict | None) -> bool:
    try:
        total = int(((api_data or {}).get("recommendations") or {}).get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    return total < _KNOWN_GAME_REVIEWS


def _scrape_youtube(app_id: str, game_name: str, lesser_known: bool = False) -> str:
    """Trailer link: a YouTube search result that passes the trailer rules (see _youtube_top_trailer),
    else the YouTube search link. A video embedded on the Steam page is not used: it skipped those rules
    (kind, channel size, views) and Steam hosts its own trailers now."""
    video = _youtube_top_trailer(game_name, lesser_known)
    return video or _youtube_search(game_name)


_YT_STR = r'"((?:[^"\\]|\\.)*)"'


# A trailer is taken only from a big channel and with real views; otherwise the card keeps the YouTube
# search link and the trailer is picked by hand.
_TRAILER_MIN_SUBS = 100_000
_TRAILER_MIN_VIEWS = 10_000
_SUBS_TTL = 24 * 3600
_SUBS_CACHE: dict[str, tuple[int, float]] = {}   # channel path -> (subscribers, fetched at)
_MAX_CHANNEL_LOOKUPS = 6                          # channel pages read per game at most


def _count(s: str) -> int:
    """'175,213 views' -> 175213, '1.34M subscribers' -> 1340000, 'No views' -> 0."""
    m = re.search(r"([\d.,]+)\s*([KMB]?)", (s or "").replace("\u00a0", " "), re.I)
    if not m:
        return 0
    num, mult = m.group(1), {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[m.group(2).upper()]
    try:
        return int(float(num.replace(",", ".") if num.count(",") == 1 and "." not in num and len(num.split(",")[1]) < 3 else num.replace(",", "")) * mult) if mult > 1 else int(num.replace(",", "").replace(".", ""))
    except ValueError:
        return 0


def _yt_subs(path: str) -> int:
    """Subscriber count of a channel ("/@IGN" or "/channel/UC..."), cached for a day; 0 when unknown.
    Only a real count is cached, so a failed page is asked again next time."""
    if not path:
        return 0
    hit = _SUBS_CACHE.get(path)
    if hit and time.time() - hit[1] < _SUBS_TTL:
        return hit[0]
    n = 0
    try:
        resp = requests.get("https://www.youtube.com" + path, headers=_HEADERS, timeout=15)
        if resp.status_code == 200:
            m = re.search(r'"content":"([\d.,]+\s*[KMB]?) subscribers"', resp.text) or \
                re.search(r"([\d.,]+\s*[KMB]?) subscribers", resp.text)
            n = _count(m.group(1)) if m else 0
    except Exception as e:
        logger.warning(f"[Steam] YouTube channel page failed for {path}: {e}")
    if n:
        if len(_SUBS_CACHE) > 500:
            _SUBS_CACHE.clear()
        _SUBS_CACHE[path] = (n, time.time())
    return n


def _yt_results(query: str) -> list[tuple[str, str, str, int, str]]:
    """YouTube search results as [(video_id, title, channel, views, channel_path)] in YouTube's order.
    The results page sometimes comes back without video entries, so an empty page is asked once more."""
    for _ in range(2):
        try:
            resp = requests.get("https://www.youtube.com/results", params={"search_query": query},
                                headers=_HEADERS, timeout=15)
        except Exception as e:
            logger.warning(f"[Steam] YouTube search failed for '{query}': {e}")
            return []
        if resp.status_code != 200:
            return []
        out = []
        for chunk in resp.text.split('"videoRenderer":{')[1:]:
            vid = re.search(r'"videoId":"([\w-]{11})"', chunk)
            title = re.search(r'"title":\{"runs":\[\{"text":' + _YT_STR, chunk)
            owner = re.search(r'"(?:ownerText|longBylineText)":\{"runs":\[\{"text":' + _YT_STR, chunk)
            views = re.search(r'"viewCountText":\{"simpleText":' + _YT_STR, chunk)
            path = re.search(r'"canonicalBaseUrl":"(/[^"]+)"', chunk) or re.search(r'"browseId":"(UC[\w-]+)"', chunk)
            chpath = (path.group(1) if path.group(1).startswith("/") else "/channel/" + path.group(1)) if path else ""
            if vid and title:
                out.append((vid.group(1), title.group(1), owner.group(1) if owner else "",
                            _count(views.group(1)) if views else 0, chpath))
        if out:
            return out
    return []


# Trailer kinds, checked on the video title with the game's own name taken out (normalised, lowercase).
# Never used: demo / pre-order / DLC / update / beta... trailers, they do not show the game we add.
_TRAILER_BAD_RE = re.compile(
    r"\b(demo|pre ?orders?|pre ?purchase|beta|alpha|playtest|early access|dlc|expansion|season pass|"
    r"season \d+|update|patch|roadmap|mod|fan ?made|concept|reaction|review|parody|leak|leaked)\b")
# Best first: launch / release trailer, then an "official" trailer, then any other trailer, teasers last.
_TRAILER_TIERS = (
    re.compile(r"\b(launch|release(?! date)|out now|available now)\b"),
    re.compile(r"\bofficial\b"),
)
_TRAILER_TEASER_RE = re.compile(r"\bteaser\b")


def _trailer_tier(title: str, game_name: str):
    """0 launch/release, 1 official, 2 other trailer, 3 teaser; None = not a usable trailer."""
    t = norm_title(title)
    g = norm_title(game_name)
    rest = (" " + t + " ").replace(" " + g + " ", " ") if g else t   # the game's own name may hold "demo" etc.
    if "trailer" not in rest or _TRAILER_BAD_RE.search(rest):
        return None
    for i, rx in enumerate(_TRAILER_TIERS):
        if rx.search(rest):
            return i
    return 3 if _TRAILER_TEASER_RE.search(rest) else 2


def _youtube_top_trailer(game_name: str, lesser_known: bool = False) -> str:
    """A YouTube trailer of the game. The title must mention the game and be a real trailer: launch /
    release first, then "official", then any other; demo, pre-order, DLC, update... trailers are never
    taken, nor PlayStation channels. Lesser-known games: IGN / GameTrailers / GameSpot first (also searched
    by name when the plain search has none of them), else the best other result. Only a video with
    10K+ views on a 100K+ subscriber channel counts; with none, "" (the card gets the search link)."""
    want = norm_title(game_name).split()[:2]

    def tier(r):
        t = norm_title(r[1])
        if not all(w in t for w in want) or _PS_CHANNEL_RE.search(r[2] or ""):
            return None
        return _trailer_tier(r[1], game_name)

    def ranked(rows, only_trusted=False):
        """Usable trailers best first (tier, trusted-channel order, YouTube order), views already checked."""
        cands = []
        for i, r in enumerate(rows):
            k = tier(r)
            if k is None or r[3] < _TRAILER_MIN_VIEWS:
                continue
            ch = (r[2] or "").strip().lower()
            if only_trusted and ch not in _TRAILER_CHANNELS:
                continue
            cands.append((k, _TRAILER_CHANNELS.index(ch) if only_trusted else 0, i, r))
        cands.sort(key=lambda c: c[:3])
        return cands

    lookups = [0]

    def big(path):
        """Channel has enough subscribers. Cached channels are free; new pages are capped per game."""
        hit = _SUBS_CACHE.get(path)
        if not (hit and time.time() - hit[1] < _SUBS_TTL):
            if lookups[0] >= _MAX_CHANNEL_LOOKUPS:
                return False
            lookups[0] += 1
        return _yt_subs(path) >= _TRAILER_MIN_SUBS

    def best(rows, only_trusted=False):
        """The best usable trailer whose channel is big enough (channel pages read lazily, best first)."""
        tried = set()
        for c in ranked(rows, only_trusted):
            path = c[3][4]
            if path in tried:
                continue
            if big(path):
                return c[0], c[3][0]
            tried.add(path)
        return None

    def top_tier(rows, only_trusted=False):
        """Best trailer kind among the results, by title and views only (no channel pages)."""
        c = ranked(rows, only_trusted)
        return c[0][0] if c else None

    seen, rows = set(), []

    def add(query):
        for r in _yt_results(query):
            if r[0] not in seen:
                seen.add(r[0])
                rows.append(r)

    add(f"{game_name} official trailer")
    if top_tier(rows) != 0:   # no launch / release trailer yet -> ask for one by name
        add(f"{game_name} launch trailer")
    pick = None
    if lesser_known:
        if top_tier(rows, True) != 0:   # no launch trailer from these channels yet -> ask for theirs by name
            add(f"{game_name} trailer IGN GameTrailers")
        pick = best(rows, True)
    if not pick:
        pick = best(rows)
    if not pick:
        logger.info(f"[Steam] No trailer from a big channel for '{game_name}': keeping the search link")
    return f"https://www.youtube.com/watch?v={pick[1]}" if pick else ""


def _fetch_related_games(app_id: str, game_name: str) -> list[str]:
    """
    Similar games from Steam's "More like this" page (released tab), in Steam's order, with their
    real store names (the app-URL slugs lose punctuation, e.g. "ARK_Survival_Ascended").
    """
    url = f"https://store.steampowered.com/recommended/morelike/app/{app_id}/"
    try:
        resp = requests.get(url, headers={**_HEADERS, "Referer": _STORE_BASE_URL.format(app_id=app_id)},
                            cookies=_AGE_COOKIES, timeout=15)
        if resp.status_code != 200:
            logger.warning(f"[Steam] morelike returned {resp.status_code} for app {app_id}")
            return []
        html = resp.text
        start = html.find('id="released"')
        block = html[start:] if start >= 0 else html
        ids = []
        for x in re.findall(r'data-ds-appid="(\d+)"', block):
            if x != str(app_id) and x not in ids:
                ids.append(x)
        ids = ids[:30]
        items = _steam_items([str(app_id)] + ids)
        me = items.get(str(app_id)) or {}
        similar = [items[i] for i in ids if i in items]
        _related_cache[str(app_id)] = {"franchise": me.get("franchise", ""), "similar": similar}
        logger.info(f"[Steam] morelike: {len(similar)} similar games for app {app_id}, franchise={me.get('franchise') or '-'}")
        return [x["name"] for x in similar]
    except Exception as e:
        logger.warning(f"[Steam] morelike failed for app {app_id}: {e}")
        return []


# app_id -> {"franchise": str, "similar": [{"name", "franchise"}]} from the last morelike fetch,
# read by pick_related() right after scrape_steam_page().
_related_cache: dict = {}


def _steam_items(app_ids: list) -> dict:
    """appid -> {name, franchise}, one request (public IStoreBrowseService, no key needed). Games only."""
    import json as _json
    resp = requests.get("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/",
                        params={"input_json": _json.dumps({"ids": [{"appid": int(i)} for i in app_ids],
                                                           "context": {"language": "english", "country_code": "US"},
                                                           "data_request": {"include_basic_info": True}})},
                        headers=_HEADERS, timeout=15)
    resp.raise_for_status()
    out = {}
    for it in resp.json().get("response", {}).get("store_items", []):
        if it.get("name") and it.get("type", 0) == 0:   # 0 = game (skips DLC, soundtracks, software)
            fr = ((it.get("basic_info") or {}).get("franchises") or [{}])[0].get("name", "")
            out[str(it.get("appid"))] = {"name": it["name"], "franchise": fr}
    return out


_ROMAN = {"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x", "xi", "xii"}
_WEAK_KEYS = {"the", "a", "an", "game", "games", "simulator", "edition", "remastered", "collection"}


def series_key(name: str) -> str:
    """Series name from a title: part before ':' / ' - ', without trailing part numbers / years.
    "Fallout 2: A Post Nuclear..." -> "fallout", "The Witcher 3: Wild Hunt" -> "the witcher"."""
    head = re.split(r":| - | \u2013 | \u2014 ", name or "")[0]
    words = norm_title(head).split()
    while words and (words[-1].isdigit() or words[-1] in _ROMAN):
        words.pop()
    key = " ".join(words)
    return "" if len(key) < 3 or key in _WEAK_KEYS else key


def pick_related(app_id: str, game_name: str, raw_related: list, limit: int = 5) -> list:
    """Related Apps for a card, only games we have in the Boosteroid catalog (their catalog names):
    1) same-franchise games from Steam's similar list, 2) other parts of the series from our catalog,
    3) the rest of Steam's similar games, in Steam's order. The game itself and its other platform
    versions ("Fallout 2 (Epic)") are never listed."""
    from services.boosteroid_catalog_service import find_in_catalog, catalog_loaded, catalog_games
    if not catalog_loaded():
        return list(raw_related[:limit])
    info = _related_cache.get(str(app_id)) or {}
    franchise = info.get("franchise", "")
    keys = {k for k in (norm_title(franchise), series_key(game_name)) if k and k not in _WEAK_KEYS}
    # The game itself, also under its short catalog name: "Fallout 2: A Post Nuclear..." is "Fallout 2 (Epic)" here.
    out, seen = [], {norm_title(game_name), norm_title(re.split(r":| - ", game_name or "")[0])}

    def add(catalog_game):
        n = catalog_game.get("name") or ""
        nn = norm_title(n)
        if nn and nn not in seen:
            seen.add(nn)
            out.append(n)

    if franchise:
        for it in info.get("similar", []):
            g = it.get("franchise") == franchise and find_in_catalog(it["name"])
            if g:
                add(g)
    if keys:
        series = [g for g in catalog_games()
                  if any(norm_title(g.get("name")) == k or norm_title(g.get("name")).startswith(k + " ") for k in keys)]
        for g in sorted(series, key=lambda g: norm_title(g.get("name"))):
            add(g)
    for n in raw_related:
        g = find_in_catalog(n)
        if g:
            add(g)
    return out[:limit]


def _youtube_search(game_name: str) -> str:
    return f"https://www.youtube.com/results?search_query={url_quote(game_name + ' official trailer')}"



def find_store_link(game_name: str, platform: str, steam_app_id: str) -> str:
    """Return the game's link on the platform store. Falls back to a search URL."""
    if platform == "Steam":
        return f"https://store.steampowered.com/app/{steam_app_id}/"
    if platform == "Xbox":
        return _query_xbox_catalog(game_name).get("link") or \
               f"https://www.xbox.com/en-US/search?q={url_quote(game_name)}"
    if platform in ("Epic Games", "Epic"):
        return _find_epic_link(game_name)
    if platform == "Ubisoft":
        slug = re.sub(r"[^a-z0-9]+", "-", game_name.lower()).strip("-")
        return f"https://www.ubisoft.com/en-gb/game/{slug}"
    return f"https://store.steampowered.com/app/{steam_app_id}/"


def norm_title(name: str) -> str:
    """Comparable title: no (Xbox)/(Epic) suffixes, no trademark signs, lowercase alphanumerics only."""
    name = re.sub(r"\([^)]*\)", " ", name or "")
    name = re.sub(r"[™®©]", "", name)
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def _xbox_search(game_name: str) -> list[dict]:
    """Microsoft Store game search (autosuggest). Returns [{pid, title}] in store order."""
    resp = requests.get(
        "https://displaycatalog.mp.microsoft.com/v7.0/productFamilies/autosuggest",
        params={"market": "US", "languages": "en-US", "query": game_name,
                "productFamilyNames": "Games", "platformDependencyName": "Windows.Xbox"},
        headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    out = []
    for res in resp.json().get("Results", []):
        for prod in res.get("Products", []):
            if prod.get("ProductId") and prod.get("Type", "Game") == "Game":
                out.append({"pid": prod["ProductId"], "title": prod.get("Title", "")})
    return out


def _xbox_pegi(pid: str) -> str:
    resp = requests.get(
        "https://displaycatalog.mp.microsoft.com/v7.0/products",
        params={"bigIds": pid, "market": "GB", "languages": "en-US"}, headers=_HEADERS, timeout=10)
    resp.raise_for_status()
    for product in resp.json().get("Products", []):
        for mp in product.get("MarketProperties", []):
            for rating in mp.get("ContentRatings", []):
                if rating.get("RatingSystem", "").upper() == "PEGI":
                    return str(rating.get("RatingId", "")).split(":")[-1]   # "PEGI:16" -> "16"
    return ""


def _query_xbox_catalog(game_name: str) -> dict:
    """
    Finds the game in the Microsoft Store. Only an exact title match counts (after norm_title),
    so a DLC or a different game with a similar name never becomes the card's link.
    Returns {'link', 'pegi'}, both possibly empty.
    """
    try:
        want = norm_title(game_name)
        hits = _xbox_search(game_name)
        match = next((h for h in hits if norm_title(h["title"]) == want), None)
        if not match:
            logger.info(f"[Steam] Xbox: no exact match for '{game_name}' among {[h['title'] for h in hits[:5]]}")
            return {}
        slug = re.sub(r"[^a-z0-9]+", "-", match["title"].lower()).strip("-")
        link = f"https://www.xbox.com/en-US/games/store/{slug}/{match['pid']}"
        pegi = ""
        try:
            pegi = _xbox_pegi(match["pid"])
        except Exception as e:
            logger.warning(f"[Steam] Xbox PEGI lookup failed for '{game_name}': {e}")
        logger.info(f"[Steam] Xbox catalog for '{game_name}': link={link}, pegi={pegi or '-'}")
        return {"link": link, "pegi": pegi}
    except Exception as e:
        logger.warning(f"[Steam] Xbox catalog query failed for '{game_name}': {e}")
        return {}


def _find_epic_link(game_name: str) -> str:
    """Epic product page for the game (exact title match), else an Epic search link."""
    query = ('query s($k:String!){Catalog{searchStore(keywords:$k,count:10,country:"US",locale:"en-US",'
             'category:"games/edition/base"){elements{title productSlug urlSlug '
             'catalogNs{mappings(pageType:"productHome"){pageSlug}} offerMappings{pageSlug pageType}}}}}')
    try:
        resp = requests.post("https://store.epicgames.com/graphql",
                             json={"query": query, "variables": {"k": norm_title(game_name) or game_name}},
                             headers={**_HEADERS, "Content-Type": "application/json"}, timeout=15)
        if resp.status_code == 200:
            want = norm_title(game_name)
            for el in (((resp.json().get("data") or {}).get("Catalog") or {}).get("searchStore") or {}).get("elements") or []:
                if norm_title(el.get("title", "")) != want:
                    continue
                slug = ""
                for m in (el.get("offerMappings") or []) + ((el.get("catalogNs") or {}).get("mappings") or []):
                    if m.get("pageSlug"):
                        slug = m["pageSlug"]
                        break
                slug = slug or el.get("productSlug") or el.get("urlSlug")
                if slug:
                    url = f"https://store.epicgames.com/en-US/p/{slug}"
                    logger.info(f"[Steam] Epic link for '{game_name}': {url}")
                    return url
    except Exception as e:
        logger.warning(f"[Steam] Epic store search failed for '{game_name}': {e}")
    return f"https://store.epicgames.com/en-US/browse?q={url_quote(game_name)}"


def extract_game_info(
    app_id: str,
    platform: str,
    section: str,
    api_data: dict,
    youtube_url: str,
    related_apps: str,
) -> dict:
    """Map Steam API + scraped data to spreadsheet columns A–K."""
    name = api_data.get("name", "")
    import html as _html
    description = _html.unescape(api_data.get("short_description", "")).replace("\ufffd", "").strip()
    description = re.sub(r"\s{2,}", " ", description)
    # Column D: website only shown for Install section
    website = api_data.get("website", "") if section == "Install" else ""

    ratings = api_data.get("ratings") or {}
    pegi = (ratings.get("pegi") or {}).get("rating", "")

    release_date = api_data.get("release_date", {}).get("date", "")
    publishers = api_data.get("publishers", [])
    developers = api_data.get("developers", [])

    # Query Xbox catalog once - used for store link (if Xbox) and PEGI fallback
    xbox_data: dict = {}
    if platform == "Xbox" or not pegi:
        xbox_data = _query_xbox_catalog(name)

    if not pegi and xbox_data.get("pegi"):
        pegi = xbox_data["pegi"]
        logger.info(f"[Steam] PEGI from Xbox catalog for '{name}': {pegi}")
    if not pegi:
        # Last resort: Steam's own age gate (not a PEGI rating, but better than nothing).
        age = api_data.get("required_age", 0)
        pegi = str(age) if age and str(age) != "0" else ""

    if platform == "Xbox":
        app_link = xbox_data.get("link") or f"https://www.xbox.com/en-US/search?q={url_quote(name)}"
    else:
        app_link = find_store_link(name, platform, app_id)

    return {
        "title": name,
        "description": description,
        "platform": platform,
        "description_url": website,
        "pegi_rating": pegi,
        "release_date": release_date,
        "publisher": publishers[0] if publishers else "",
        "developer": developers[0] if developers else "",
        "app_link": app_link,
        "youtube_link": youtube_url,
        "related_apps": related_apps,
        "screenshot_urls": [s["path_full"] for s in api_data.get("screenshots", [])][:6],
    }


def download_screenshots(screenshot_urls: list, dest_dir: str) -> list:
    """Download up to 6 screenshots into dest_dir. Returns paths of saved files."""
    paths = []
    for i, url in enumerate(screenshot_urls[:6]):
        filename = os.path.join(dest_dir, f"screenshot_{i + 1}.jpg")
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=30)
            resp.raise_for_status()
            with open(filename, "wb") as fh:
                fh.write(resp.content)
            paths.append(filename)
            logger.info(f"[Steam] Screenshot {i + 1} saved: {filename}")
        except Exception as e:
            logger.warning(f"[Steam] Screenshot {i + 1} download failed ({url}): {e}")
    return paths
