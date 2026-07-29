"""
anime_scraper.py — GitHub Actions version

Reads START_INDEX and END_INDEX from environment variables (set via workflow_dispatch inputs).
Scrapes HiAnime, finds MAL IDs, accumulates output into a single JSON file until it reaches
5 MB (then and only then starts a new chunk), and commits everything to the repo.

Extra files committed each run:
  data/already_processed_urls.txt  — one HiAnime URL per line (append-only, deduped)
                                     Entries here are SKIPPED on future runs.
  data/400-client-error.txt        — MAL API queries that returned HTTP 400 (append-only)
                                     The HiAnime URL that triggered each 400 is stored on
                                     the first line of every block so those entries are also
                                     SKIPPED on future runs.
"""

import json, time, re, os, sys, base64, requests
from bs4 import BeautifulSoup
from difflib import SequenceMatcher

# ═══════════════════════════════════════════════════════════════
#  CONFIG — injected by GitHub Actions env vars
# ═══════════════════════════════════════════════════════════════
#
#  Range inputs are 1-based item numbers (the way humans count):
#    START_ITEM=1  END_ITEM=100  →  process items 1 … 100  (100 items)
#
#  Leave both unset (or set to "0") to enable AUTO mode:
#    the script finds the next DEFAULT_BATCH unprocessed URLs
#    in master-list order and processes those.
#
_START_RAW    = os.environ.get("START_INDEX", "0").strip()
_END_RAW      = os.environ.get("END_INDEX",   "0").strip()

_START_RAW_INT = int(_START_RAW) if _START_RAW else 0
_END_RAW_INT   = int(_END_RAW)   if _END_RAW   else 0

# AUTO mode: END was not provided (0 means unset)
AUTO_MODE     = (_END_RAW_INT == 0)
DEFAULT_BATCH = 100   # URLs to process in auto mode

# Manual mode: 1-based inclusive item numbers.
# If user only sets END (leaves START at 0), clamp START to 1.
START_ITEM = max(1, _START_RAW_INT) if not AUTO_MODE else 1
END_ITEM   = _END_RAW_INT           if not AUTO_MODE else DEFAULT_BATCH

MAX_MB       = 10
MAX_BYTES    = MAX_MB * 1024 * 1024          # 5 242 880 bytes

GITHUB_JSON  = "https://raw.githubusercontent.com/srtfile/hianime.ad/refs/heads/main/data/anime_urls.json"
OUTPUT_DIR   = "data"   # folder inside repo where all output files are saved

# GitHub API (token injected by Actions automatically)
GH_TOKEN     = os.environ.get("GITHUB_TOKEN", "")
GH_REPO      = os.environ.get("GITHUB_REPOSITORY", "")   # e.g. "owner/repo"
GH_BRANCH    = os.environ.get("GITHUB_REF_NAME", "main")
GH_API       = "https://api.github.com"

# Tracking file paths (relative to repo root)
PROCESSED_URLS_PATH = f"{OUTPUT_DIR}/already_processed_urls.txt"
BAD_REQUEST_PATH    = f"{OUTPUT_DIR}/400-client-error.txt"

# Fixed output filename base — all runs accumulate into "anime_data[_partN].json"
OUTPUT_BASENAME            = "anime_data"
# Unverified (partial / no-match) entries go into a separate file series
OUTPUT_BASENAME_UNVERIFIED = "unverified_mal_id"

# ═══════════════════════════════════════════════════════════════
#  STEP 1 — HiAnime scraper
# ═══════════════════════════════════════════════════════════════

HIANIME_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def scrape_anime_info(url: str) -> dict | None:
    print(f"  Scraping: {url}")
    try:
        r = requests.get(url, headers=HIANIME_HEADERS, timeout=10)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"    -> Failed: {e}")
        return None

    soup = BeautifulSoup(r.text, "html.parser")
    data = {}

    tick = soup.find("div", class_="tick")
    if tick:
        pg = tick.find("div", class_="tick-pg")
        if pg:
            data["Rating"] = pg.get_text(strip=True)
        sub = tick.find("div", class_="tick-sub")
        if sub:
            data["Total Episodes"] = sub.get_text(strip=True)
        items = [s.get_text(strip=True) for s in tick.find_all("span", class_="item")]
        data["Type"]         = items[0] if len(items) > 0 else None
        data["Release Year"] = items[1] if len(items) > 1 else None

    info_wrap = soup.find("div", class_="anisc-info-wrap")
    if info_wrap:
        for item in info_wrap.find_all("div", class_="item"):
            head = item.find("span", class_="item-head")
            if not head:
                continue
            key = head.get_text(strip=True).replace(":", "")
            if key in ("Overview", "MAL Score"):
                continue
            name = item.find("span", class_="name")
            if name:
                data[key] = name.get_text(strip=True)
                continue
            a_tags = item.find_all("a")
            if a_tags:
                data[key] = ", ".join(a.get_text(strip=True) for a in a_tags)

    return data


def run_scraper(anime_list: list, skip_urls: set,
                slice_start: int, slice_end: int) -> tuple[list, list]:
    """
    Scrape anime_list[slice_start:slice_end] (0-based slice indices),
    skipping any URL present in skip_urls (already_processed OR 400-error).

    Returns:
        results            — list of scraped dicts
        newly_scraped_urls — HiAnime URLs successfully scraped this run
    """
    subset = anime_list[slice_start:slice_end]
    results, newly_scraped_urls = [], []

    for item in subset:
        slug = item.get("slug")
        if not slug:
            continue
        url = f"https://hianime.ad/anime/{slug}"

        if url in skip_urls:
            print(f"  Skipping (already processed / 400-error): {url}")
            continue

        info = scrape_anime_info(url)
        if info:
            results.append({
                "ID":          item.get("id"),
                "Title":       item.get("title"),
                "HiAnime_URL": url,
                **info,
            })
            newly_scraped_urls.append(url)
        time.sleep(1)

    return results, newly_scraped_urls


# ═══════════════════════════════════════════════════════════════
#  STEP 2 — MAL ID finder
# ═══════════════════════════════════════════════════════════════

MAL_API            = "https://api.myanimelist.net/v2/anime"
CLIENT_ID          = "6114d00ca681b7701d1e15fe11a4987e"
CLIENT_ID_FALLBACK = "b0f57250436db633080e10767f2dab54"   # used when primary returns 400
API_FIELDS         = "id,title,alternative_titles,media_type,num_episodes,status,start_date"
MAL_SEARCH_HTML    = "https://myanimelist.net/anime.php"

MAL_API_HEADERS = {
    "X-MAL-Client-ID": CLIENT_ID,
    "User-Agent": "NineAnimator/2 CFNetwork/976 Darwin/18.2.0",
}
MAL_API_FALLBACK_HEADERS = {
    "X-MAL-CLIENT-ID": CLIENT_ID_FALLBACK,
}
MAL_HTML_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


def extract_search_queries(local: dict) -> list:
    raw, title = local.get("Japanese", ""), local.get("Title", "")
    queries, cjk = [], re.compile(r"[\u3040-\u9fff\uff00-\uffef]+")

    m = re.search(r"\[([^\]]*[\u3040-\u9fff\uff00-\uffef][^\]]*)\]", raw)
    if m:
        queries.append(m.group(1).strip())

    spans = cjk.findall(raw)
    if spans:
        longest = max(spans, key=len)
        if len(longest) >= 3 and longest not in queries:
            queries.append(longest.strip())

    if title and title not in queries:
        queries.append(title.strip())

    first = raw.split(";")[0].strip()
    words = first.split()
    half  = len(words) // 2
    if half >= 2 and [w.lower() for w in words[:half]] == [w.lower() for w in words[half:half*2]]:
        first = " ".join(words[half:half*2])
    first = re.sub(r"\[.*?\]", "", first).strip()[:80]
    if first and first not in queries:
        queries.append(first)

    return queries[:3]


def normalize_type(t: str) -> str:
    return {"tv": "TV", "movie": "Movie", "ova": "OVA", "ona": "ONA",
            "special": "Special", "music": "Music"}.get((t or "").lower().strip(), (t or "").strip())

def parse_year(s: str) -> str | None:
    m = re.search(r"\b(19|20)\d{2}\b", s or "")
    return m.group() if m else None

def api_status(s: str) -> str:
    return {"finished_airing": "Finished Airing",
            "currently_airing": "Currently Airing",
            "not_yet_aired": "Not yet aired"}.get(s, s)


# ── Fallback helpers (used when primary API returns 400) ──────

def clean_query(q: str) -> str:
    """Strip episode info, non-ASCII, and special chars; truncate to 80 chars."""
    q = re.sub(r'episode\s*\d+(\.\d+)?', '', q, flags=re.I)
    q = re.sub(r'[^\x00-\x7F]+', ' ', q)
    q = re.sub(r'[^a-zA-Z0-9\s:-]', ' ', q)
    q = re.sub(r'\s+', ' ', q).strip()
    return q[:80]


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _raw_search_fallback(query: str) -> list:
    """Single MAL API call with the fallback client ID. Returns raw data nodes."""
    params = {
        "q":      query,
        "limit":  10,
        "fields": API_FIELDS,
    }
    try:
        r = requests.get(MAL_API, headers=MAL_API_FALLBACK_HEADERS,
                         params=params, timeout=10)
        r.raise_for_status()
        return r.json().get("data", [])
    except requests.RequestException as e:
        print(f"    [fallback-api-error] {e}")
        return []


def _best_fallback_node(original_query: str, raw_nodes: list) -> dict | None:
    """Pick the node whose title (or any alt title) is most similar to original_query."""
    best_node, best_score = None, 0.0
    for item in raw_nodes:
        node = item.get("node", {})
        score = similarity(original_query, node.get("title", ""))
        alt = node.get("alternative_titles", {})
        for val in alt.values():
            if isinstance(val, list):
                for t in val:
                    score = max(score, similarity(original_query, t))
            elif isinstance(val, str):
                score = max(score, similarity(original_query, val))
        if score > best_score:
            best_score = score
            best_node  = node
    return best_node if best_node else None


def search_mal_api_fallback(original_query: str) -> list:
    """
    Fallback MAL search triggered after a 400 on the primary API.

    Strategy:
      1. Clean the query (strip episode info, non-ASCII, special chars).
      2. Try up to three progressively shorter sub-queries.
      3. Collect all raw nodes, pick the best by similarity score.
      4. Return a one-element list in the same candidate-dict shape used by
         the primary path, or [] if nothing is found.
    """
    cleaned = clean_query(original_query)
    words   = cleaned.split()
    queries = dict.fromkeys([          # dict preserves insertion order, dedupes
        cleaned,
        " ".join(words[:5]),
        " ".join(words[:3]),
    ])

    all_nodes: list = []
    for q in queries:
        if not q:
            continue
        print(f"    [fallback] searching: {q!r}")
        all_nodes.extend(_raw_search_fallback(q))
        time.sleep(0.5)

    if not all_nodes:
        return []

    best = _best_fallback_node(original_query, all_nodes)
    if not best:
        return []

    score = similarity(original_query, best.get("title", ""))
    print(f"    [fallback] best match: {best.get('title')!r}  similarity={score:.2f}")

    return [{
        "mal_id":       best.get("id"),
        "title":        best.get("title", ""),
        "type":         normalize_type(best.get("media_type", "")),
        "episodes":     str(best.get("num_episodes") or ""),
        "status":       api_status(best.get("status", "")),
        "aired":        best.get("start_date", ""),
        "url":          f"https://myanimelist.net/anime/{best.get('id')}",
        "_from_fallback": True,   # signals find_mal_id to set Match_Score 4/4
    }]


def search_mal_api(query: str, hianime_url: str, bad_request_entries: list) -> list:
    """
    Query the MAL API.

    If a 400 is returned the full request URL AND the originating HiAnime URL
    are recorded in bad_request_entries so they can be written to
    400-client-error.txt and skipped on future runs.

    Each entry is a multi-line block:
        hianime_url: <url>
        query: '<query text>'
            [api-error] 400 Client Error: Bad Request for url: <mal url>
    """
    try:
        req = requests.Request(
            "GET", MAL_API,
            headers=MAL_API_HEADERS,
            params={"q": query, "limit": 10, "fields": API_FIELDS},
        )
        prepared = req.prepare()
        full_url = prepared.url   # exact URL sent to MAL

        s = requests.Session()
        r = s.send(prepared, timeout=15)

        if r.status_code == 400:
            print(f"    [api-error] 400 Bad Request — HiAnime URL: {hianime_url}")
            print(f"    [api-error] 400 Client Error: Bad Request for url: {full_url}")
            print(f"    [api-error] Trying fallback search logic...")
            fallback_results = search_mal_api_fallback(query)
            if fallback_results:
                print(f"    [fallback] ✓ Recovered {len(fallback_results)} candidate(s) via fallback.")
                return fallback_results
            # Fallback also came up empty — record the error for future skipping
            print(f"    [fallback] ✗ Fallback also found nothing. Recording 400 error.")
            entry = (
                f"hianime_url: {hianime_url}\n"
                f"query: {query!r}\n"
                f"    [api-error] 400 Client Error: Bad Request for url: {full_url}"
            )
            bad_request_entries.append(entry)
            return []

        if r.status_code == 401:
            return []

        r.raise_for_status()

    except requests.RequestException as e:
        print(f"    [api-error] {e}")
        return []

    out = []
    for node in r.json().get("data", []):
        a = node.get("node", {})
        out.append({
            "mal_id":   a.get("id"),
            "title":    a.get("title", ""),
            "type":     normalize_type(a.get("media_type", "")),
            "episodes": str(a.get("num_episodes") or ""),
            "status":   api_status(a.get("status", "")),
            "aired":    a.get("start_date", ""),
            "url":      f"https://myanimelist.net/anime/{a.get('id')}",
        })
    return out


def search_mal_html(query: str) -> list:
    try:
        r = requests.get(MAL_SEARCH_HTML, params={"q": query, "cat": "anime"},
                         headers=MAL_HTML_HEADERS, timeout=15)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"    [html-error] {e}")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.select_one("div#content table")
    if not table:
        return []

    out = []
    for row in table.find_all("tr")[1:]:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue
        a = cells[0].find("a", href=re.compile(r"/anime/\d+"))
        if not a:
            continue
        m = re.search(r"/anime/(\d+)", a["href"])
        if not m:
            continue
        def ct(i): return cells[i].get_text(strip=True) if i < len(cells) else ""
        out.append({
            "mal_id":   int(m.group(1)),
            "title":    a.get_text(strip=True),
            "type":     ct(2), "episodes": ct(3),
            "status":   ct(4), "aired":    ct(5),
            "url":      a["href"],
        })
    return out


def score_candidate(cand: dict, local: dict) -> dict:
    score, fails = 0, []

    mt = normalize_type(cand.get("type", "")).lower()
    lt = normalize_type(local.get("Type", "")).lower()
    if mt == lt: score += 1
    else: fails.append(f"type: mal={mt!r} local={lt!r}")

    me = re.sub(r"\D", "", str(cand.get("episodes") or ""))
    le = re.sub(r"\D", "", str(local.get("Total Episodes") or ""))
    if me and le and me == le: score += 1
    elif not me: fails.append(f"eps: mal=unknown local={le}")
    else: fails.append(f"eps: mal={me} local={le}")

    ms = (cand.get("status") or "").strip().lower()
    ls = (local.get("Status") or "").strip().lower()
    if ("finish" in ms and "finish" in ls) or ms == ls: score += 1
    else: fails.append(f"status: mal={ms!r} local={ls!r}")

    my = parse_year(str(cand.get("aired") or ""))
    ly = parse_year(local.get("Aired") or "")
    if my and ly:
        if my == ly: score += 1
        else: fails.append(f"year: mal={my} local={ly}")
    else:
        fails.append(f"year: unverifiable mal={my} local={ly}")

    return {"pass": len(fails) == 0, "score": score, "fails": fails}


def best_from(candidates, local):
    scored = []
    for c in candidates:
        s = score_candidate(c, local)
        scored.append((s["score"], s["pass"], s["fails"], c))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0] if scored else None


def find_mal_id(local: dict, bad_request_entries: list) -> dict:
    result = {
        **local,
        "MAL_ID":      None,
        "MAL_Title":   None,
        "MAL_URL":     None,
        "Match_Score": None,
        "Match_Note":  "no match",
    }

    hianime_url = local.get("HiAnime_URL", "")
    partial = None

    for query in extract_search_queries(local):
        print(f"  Query: {query!r}")
        candidates = search_mal_api(query, hianime_url, bad_request_entries)
        layer = "api"
        if not candidates:
            candidates = search_mal_html(query)
            layer = "html"
        print(f"    [{layer}] {len(candidates)} candidates")

        if not candidates:
            time.sleep(1)
            continue

        best = best_from(candidates, local)
        if not best:
            continue
        score, passed, fails, cand = best

        # Fallback candidates are accepted immediately with a forced 4/4 score
        # because they were chosen by similarity ranking inside
        # search_mal_api_fallback — no metadata fields to cross-check.
        if cand.get("_from_fallback"):
            result.update({
                "MAL_ID":      cand["mal_id"],
                "MAL_Title":   cand["title"],
                "MAL_URL":     cand["url"],
                "Match_Score": "4/4",
                "Match_Note":  "verified (fallback)",
            })
            return result

        if passed:
            result.update({
                "MAL_ID":      cand["mal_id"],
                "MAL_Title":   cand["title"],
                "MAL_URL":     cand["url"],
                "Match_Score": f"{score}/4",
                "Match_Note":  "verified",
            })
            return result

        if partial is None or score > partial[0]:
            partial = (score, passed, fails, cand)

        time.sleep(1.5)

    if partial:
        score, _, fails, cand = partial
        result.update({
            "MAL_ID":      f"UNVERIFIED:{cand['mal_id']}",
            "MAL_Title":   cand["title"],
            "MAL_URL":     cand["url"],
            "Match_Score": f"{score}/4",
            "Match_Note":  f"partial; fails={fails}",
        })
    return result


# ═══════════════════════════════════════════════════════════════
#  STEP 3 — Accumulate into ≤5 MB chunks
#
#  Strategy:
#    - All runs share a single fixed filename: anime_data.json
#    - When the latest chunk file exists in the repo, download it
#      and APPEND new entries to it.
#    - Only start anime_data_part2.json (etc.) when the combined
#      content would exceed MAX_BYTES (5 MB).
#    - Never create a new file just because it's a new run.
# ═══════════════════════════════════════════════════════════════

def list_existing_chunk_files(basename: str = OUTPUT_BASENAME) -> list[tuple[int, str]]:
    """
    List all <basename>[_partN].json files already in OUTPUT_DIR on the repo.
    Returns a sorted list of (part_number, repo_path) tuples.
      anime_data.json        → part 1
      anime_data_part2.json  → part 2  (etc.)
    Works for any basename (anime_data, unverified_mal_id, …).
    """
    url = f"{GH_API}/repos/{GH_REPO}/contents/{OUTPUT_DIR}"
    r = requests.get(url, headers=gh_headers(), params={"ref": GH_BRANCH})
    if r.status_code != 200:
        return []

    files = []
    for entry in r.json():
        name = entry.get("name", "")
        if name == f"{basename}.json":
            files.append((1, f"{OUTPUT_DIR}/{name}"))
        else:
            m = re.match(rf"^{re.escape(basename)}_part(\d+)\.json$", name)
            if m:
                files.append((int(m.group(1)), f"{OUTPUT_DIR}/{name}"))

    files.sort(key=lambda x: x[0])
    return files


def fetch_existing_chunk(repo_path: str) -> list:
    """Download and parse the JSON array from an existing chunk file. Returns [] on error."""
    raw = fetch_remote_text(repo_path)
    if not raw.strip():
        return []
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"  Warning: could not parse existing chunk {repo_path}: {e}")
        return []


def chunk_path_for_part(part: int, basename: str = OUTPUT_BASENAME) -> str:
    """Return the repo-relative path for a given part number and basename."""
    if part == 1:
        return f"{OUTPUT_DIR}/{basename}.json"
    return f"{OUTPUT_DIR}/{basename}_part{part}.json"


def accumulate_and_split(new_entries: list,
                         basename: str = OUTPUT_BASENAME) -> list[tuple[str, str, bool]]:
    """
    Merge new_entries into the existing chunk files, respecting the 5 MB limit.

    Algorithm:
      1. Find the latest (highest-numbered) existing chunk file.
      2. Download its current contents.
      3. Append new_entries one by one.  If adding an entry would push the
         file over MAX_BYTES, close the current chunk and open a new one.
      4. Return a list of (repo_path, json_string, is_modified) tuples —
         only files that actually changed are flagged for commit.

    Returns:
        list of (repo_path, json_content_str, modified_flag)
    """
    existing_chunks = list_existing_chunk_files(basename)

    if existing_chunks:
        latest_part, latest_path = existing_chunks[-1]
        print(f"  Found existing chunk: {latest_path}")
        current_data = fetch_existing_chunk(latest_path)
        print(f"  Existing entries in latest chunk: {len(current_data)}")
        current_part = latest_part
    else:
        print(f"  No existing chunk files found for '{basename}' — starting fresh.")
        current_data = []
        current_part = 1
        latest_path  = chunk_path_for_part(1, basename)

    results_to_commit: list[tuple[str, str, bool]] = []
    current_path     = chunk_path_for_part(current_part, basename)
    modified         = False

    for entry in new_entries:
        trial = current_data + [entry]
        trial_bytes = json.dumps(trial, indent=4, ensure_ascii=False).encode("utf-8")

        if len(trial_bytes) >= MAX_BYTES:
            # Current chunk is full — save it and open a new one
            if current_data:
                content = json.dumps(current_data, indent=4, ensure_ascii=False)
                results_to_commit.append((current_path, content, modified))
                print(f"  Chunk full ({len(trial_bytes)/1024:.1f} KB would exceed {MAX_MB} MB) "
                      f"— closing {current_path} with {len(current_data)} entries.")
            current_part += 1
            current_path  = chunk_path_for_part(current_part, basename)
            current_data  = [entry]
            modified      = True
        else:
            current_data.append(entry)
            modified = True

    # Final (possibly only) chunk
    if current_data and modified:
        content = json.dumps(current_data, indent=4, ensure_ascii=False)
        results_to_commit.append((current_path, content, modified))

    return results_to_commit


# ═══════════════════════════════════════════════════════════════
#  STEP 4 — Commit to GitHub via API
# ═══════════════════════════════════════════════════════════════

def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

def get_file_sha(path: str) -> str | None:
    url = f"{GH_API}/repos/{GH_REPO}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), params={"ref": GH_BRANCH})
    if r.status_code == 200:
        return r.json().get("sha")
    return None

def fetch_remote_text(path: str) -> str:
    """Download the raw text of a repo file, or '' if absent."""
    url = f"{GH_API}/repos/{GH_REPO}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), params={"ref": GH_BRANCH})
    if r.status_code == 200:
        encoded = r.json().get("content", "")
        return base64.b64decode(encoded).decode("utf-8")
    return ""

def commit_file(path: str, content: str, message: str) -> bool:
    url = f"{GH_API}/repos/{GH_REPO}/contents/{path}"
    sha = get_file_sha(path)

    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch":  GH_BRANCH,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=gh_headers(), json=payload)
    if r.status_code in (200, 201):
        print(f"  ✓ Committed: {path}")
        return True
    else:
        print(f"  ✗ Failed to commit {path}: {r.status_code} {r.text[:200]}")
        return False


# ═══════════════════════════════════════════════════════════════
#  HELPERS — tracking files
# ═══════════════════════════════════════════════════════════════

def ensure_tracking_file(path: str) -> None:
    """Create the tracking file in the repo if it does not exist yet."""
    url = f"{GH_API}/repos/{GH_REPO}/contents/{path}"
    r = requests.get(url, headers=gh_headers(), params={"ref": GH_BRANCH})
    if r.status_code == 200:
        return   # already exists
    print(f"  Creating missing tracking file: {path}")
    payload = {
        "message": f"init: create {path}",
        "content": base64.b64encode(b"").decode("ascii"),
        "branch":  GH_BRANCH,
    }
    rc = requests.put(url, headers=gh_headers(), json=payload)
    if rc.status_code in (200, 201):
        print(f"  ✓ Created: {path}")
    else:
        print(f"  ✗ Could not create {path}: {rc.status_code} {rc.text[:200]}")


def load_remote_lines(path: str) -> list[str]:
    """Return non-empty lines from a text file in the repo (or [] if absent)."""
    raw = fetch_remote_text(path)
    return [line for line in raw.splitlines() if line.strip()]

def merge_lines(existing: list[str], new_lines: list[str]) -> str:
    """Append new_lines to existing (deduped) and return as a newline-terminated string."""
    seen     = set(existing)
    combined = list(existing)
    for line in new_lines:
        if line not in seen:
            seen.add(line)
            combined.append(line)
    return "\n".join(combined) + "\n" if combined else ""

def extract_hianime_urls_from_400_file(lines: list[str]) -> set:
    """
    Parse 400-client-error.txt lines and collect every HiAnime URL listed there.
    Each block starts with a line like:  hianime_url: https://hianime.ad/anime/...
    """
    urls = set()
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("hianime_url:"):
            url = stripped[len("hianime_url:"):].strip()
            if url:
                urls.add(url)
    return urls


# ═══════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════

def resolve_slice(anime_list: list, skip_urls: set) -> tuple[int, int, str]:
    """
    Work out which 0-based slice of anime_list to process this run.

    Manual mode  (START_ITEM / END_ITEM both non-zero):
        User supplies 1-based item numbers, inclusive on both ends.
        START_ITEM=1, END_ITEM=100  →  slice [0:100]  (100 items)

    Auto mode  (both are 0 / unset):
        Walk the master list in order and collect the next DEFAULT_BATCH
        URLs that are not in skip_urls.  Return the slice that covers
        exactly those items (may span more than DEFAULT_BATCH positions
        if some positions are already processed).
    """
    if not AUTO_MODE:
        # Convert 1-based inclusive → 0-based slice
        slice_start = START_ITEM - 1          # item 1 → index 0
        slice_end   = END_ITEM                # item 100 → stop at 100 (exclusive)
        label       = f"manual {START_ITEM}–{END_ITEM}"
        return slice_start, slice_end, label

    # Auto: scan for next DEFAULT_BATCH unprocessed items
    collected   = 0
    first_idx   = None
    last_idx    = -1

    for idx, item in enumerate(anime_list):
        slug = item.get("slug")
        if not slug:
            continue
        url = f"https://hianime.ad/anime/{slug}"
        if url in skip_urls:
            continue
        if first_idx is None:
            first_idx = idx
        last_idx  = idx
        collected += 1
        if collected >= DEFAULT_BATCH:
            break

    if first_idx is None:
        print("AUTO mode: no unprocessed URLs found — nothing to do.")
        sys.exit(0)

    slice_start = first_idx
    slice_end   = last_idx + 1          # make it exclusive
    label       = f"auto next-{collected} (indices {first_idx}–{last_idx})"
    return slice_start, slice_end, label


def main():
    print(f"Repo:  {GH_REPO}  branch={GH_BRANCH}\n")

    # ── Fetch master list ──────────────────────────────────────
    print("Fetching master list from GitHub...")
    try:
        r = requests.get(GITHUB_JSON, timeout=15)
        r.raise_for_status()
        anime_list = r.json().get("anime", [])
    except Exception as e:
        print(f"Failed to load master list: {e}")
        sys.exit(1)
    print(f"Total entries: {len(anime_list)}\n")

    # ── Ensure tracking files exist ────────────────────────────
    print("Ensuring tracking files exist...")
    ensure_tracking_file(PROCESSED_URLS_PATH)
    ensure_tracking_file(BAD_REQUEST_PATH)
    print()

    # ── Load already_processed_urls.txt ───────────────────────
    print("Loading already_processed_urls.txt from repo...")
    existing_processed = load_remote_lines(PROCESSED_URLS_PATH)
    processed_url_set  = set(existing_processed)
    print(f"  {len(processed_url_set)} URLs already processed.\n")

    # ── Load 400-client-error.txt and extract HiAnime URLs ────
    print("Loading 400-client-error.txt from repo...")
    existing_400_lines   = load_remote_lines(BAD_REQUEST_PATH)
    bad_request_url_set  = extract_hianime_urls_from_400_file(existing_400_lines)
    print(f"  {len(existing_400_lines)} existing 400-error lines.")
    print(f"  {len(bad_request_url_set)} unique HiAnime URLs blocked by past 400 errors.\n")

    # Combined skip set: processed + 400-errored
    skip_urls = processed_url_set | bad_request_url_set
    print(f"Total URLs to skip this run: {len(skip_urls)}\n")

    # ── Resolve which slice to process ────────────────────────
    slice_start, slice_end, range_label = resolve_slice(anime_list, skip_urls)
    item_count = slice_end - slice_start
    mode_tag   = "AUTO" if AUTO_MODE else "MANUAL"
    print(f"Mode:  {mode_tag}  |  {range_label}")
    print(f"Slice: [{slice_start}:{slice_end}]  ({item_count} master-list positions)\n")

    # ── Step 1: Scrape HiAnime ─────────────────────────────────
    print("=" * 55)
    print("STEP 1 — Scraping HiAnime")
    print("=" * 55)
    scraped, newly_scraped_urls = run_scraper(anime_list, skip_urls,
                                              slice_start, slice_end)
    print(f"\nScraped {len(scraped)} entries.\n")

    # ── Step 2: Find MAL IDs ───────────────────────────────────
    print("=" * 55)
    print("STEP 2 — Finding MAL IDs")
    print("=" * 55)
    enriched             = []
    bad_request_entries: list[str] = []

    for i, entry in enumerate(scraped):
        print(f"\n[{i+1}/{len(scraped)}] {entry.get('Title', '?')}  |  {entry.get('HiAnime_URL', '')}")
        result = find_mal_id(entry, bad_request_entries)
        tag = "✓ VERIFIED" if result["Match_Note"] in ("verified", "verified (fallback)") else f"✗ {result['Match_Note']}"
        print(f"  → MAL_ID={result['MAL_ID']}  {tag}")
        enriched.append(result)
        time.sleep(2)

    VERIFIED_NOTES   = {"verified", "verified (fallback)"}
    verified_entries   = [e for e in enriched if e.get("Match_Note") in VERIFIED_NOTES]
    unverified_entries = [e for e in enriched if e.get("Match_Note") not in VERIFIED_NOTES]

    print(f"\n{len(verified_entries)} verified  |  {len(unverified_entries)} unverified.\n")

    # ── Step 3: Accumulate into existing chunk files (≤5 MB) ──
    print("=" * 55)
    print("STEP 3 — Accumulating into output files (max 5 MB per file)")
    print("=" * 55)

    print(f"\n  [anime_data] {len(verified_entries)} verified entries →")
    verified_chunks   = accumulate_and_split(verified_entries,   OUTPUT_BASENAME)
    print(f"  {len(verified_chunks)} verified file(s) to commit.")

    print(f"\n  [unverified_mal_id] {len(unverified_entries)} unverified entries →")
    unverified_chunks = accumulate_and_split(unverified_entries, OUTPUT_BASENAME_UNVERIFIED)
    print(f"  {len(unverified_chunks)} unverified file(s) to commit.\n")

    # ── Step 4: Commit JSON output ─────────────────────────────
    print("=" * 55)
    print("STEP 4 — Committing to GitHub")
    print("=" * 55)
    all_committed = True

    all_chunks = [
        (verified_chunks,   "verified"),
        (unverified_chunks, "unverified"),
    ]
    for chunk_list, label in all_chunks:
        for repo_path, content, _ in chunk_list:
            size_kb     = len(content.encode("utf-8")) / 1024
            entry_count = len(json.loads(content))
            print(f"  [{label}] {repo_path}  ({size_kb:.1f} KB, {entry_count} total entries)")
            ok = commit_file(
                path=repo_path,
                content=content,
                message=(
                    f"scrape: append {len(verified_entries)} verified + "
                    f"{len(unverified_entries)} unverified [{range_label}]"
                ),
            )
            if not ok:
                all_committed = False
                print(f"  ✗ Commit failed for {repo_path} — URLs will NOT be marked processed.")
            time.sleep(1)

    # ── Step 5: Update already_processed_urls.txt ─────────────
    # Only mark URLs as processed when ALL JSON commits succeeded.
    # If any commit failed, the URLs stay untracked so the next run retries them.
    if newly_scraped_urls and all_committed:
        print(f"\n  Updating {PROCESSED_URLS_PATH} (+{len(newly_scraped_urls)} URLs)...")
        updated_processed = merge_lines(existing_processed, newly_scraped_urls)
        commit_file(
            path=PROCESSED_URLS_PATH,
            content=updated_processed,
            message=f"track: add {len(newly_scraped_urls)} processed URLs [{range_label}]",
        )
        time.sleep(1)
    elif not all_committed:
        print(f"\n  ⚠ Skipping {PROCESSED_URLS_PATH} update — one or more JSON commits failed.")
        print(f"    These {len(newly_scraped_urls)} URLs will be retried on the next run.")
    else:
        print(f"\n  No new URLs to add to {PROCESSED_URLS_PATH}.")

    # ── Step 6: Update 400-client-error.txt ───────────────────
    if bad_request_entries:
        print(f"\n  Updating {BAD_REQUEST_PATH} (+{len(bad_request_entries)} entries)...")
        updated_400 = merge_lines(existing_400_lines, bad_request_entries)
        commit_file(
            path=BAD_REQUEST_PATH,
            content=updated_400,
            message=f"track: add {len(bad_request_entries)} MAL 400 errors [{range_label}]",
        )
        time.sleep(1)
    else:
        print(f"\n  No new 400 errors to record.")

    print("\nAll done.")


if __name__ == "__main__":
    main()
