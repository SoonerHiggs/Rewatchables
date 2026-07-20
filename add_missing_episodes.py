"""
Rewatchables Missing Episode Auto-Add Script  (v2 - fixed title parsing)
=====================================
Safety net for weeks you don't get to manually add the episode entry
before it airs. Fetches the official Rewatchables RSS feed and, for any
episode not already in rewatchables.json, creates a full entry using
TMDB data.

WHAT IT FILLS IN AUTOMATICALLY (from TMDB):
  title, year, director, runtime, genre, full top-billed cast, hosts (from RSS)

WHAT IT LEAVES BLANK, ON PURPOSE:
  imdb_rating, rt_audience_score — not reliably available from a free API.

TITLE PARSING (v2):
  Episode titles look like: 'Movie Title' [optional subtitle] With Host1, Host2
  The v1 script captured everything between the opening quote and the FIRST
  apostrophe-like character it saw — which broke on any title containing an
  apostrophe (Ferris Bueller's Day Off -> "Ferris Bueller", You've Got Mail
  -> "You", The Devil's Advocate -> "The Devil"). This caused ~30 bad
  entries in a single run.

  v2 instead: splits on the first standalone "With" (word boundary), then
  within the text before it, takes everything between the FIRST quote
  character and the LAST quote character — so internal apostrophes no
  longer truncate the title. Trailing punctuation (commas, periods) left
  over from titles like "'Jaws,'" is stripped before the TMDB search.

DUPLICATE DETECTION (v2):
  v1 checked the raw RSS-parsed title against existing entries — so a
  movie already in the file under a slightly different title format
  (e.g. no ellipsis, different punctuation) could get added a second
  time under TMDB's exact title text. v2 normalizes both the RSS title
  AND the TMDB-matched canonical title (lowercase, punctuation stripped)
  before comparing, and checks again after the TMDB match resolves —
  not just before.

  If the RSS episode title doesn't cleanly match a TMDB search result,
  or looks like a duplicate under normalization, it's flagged in the
  output for manual review rather than silently guessed at or skipped.

SETUP:
  export TMDB_API_KEY="your-tmdb-read-access-token"
  python add_missing_episodes.py

OUTPUT:
  Updates rewatchables.json in place. Prints a summary of what was added
  and flags anything worth double-checking.
"""

import json, re, sys, os
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not found.")
    print("Please run: pip install requests")
    exit(1)

RSS_URL = "https://feeds.megaphone.fm/the-rewatchables"
INPUT_FILE = "rewatchables.json"
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
TMDB_BASE = "https://api.themoviedb.org/3"
CAST_LIMIT = 10  # top-billed cast members to include

# Any of these count as a "quote" character wrapping the movie title
QUOTE_CHARS = "'\u2018\u2019\""

# Splits the title into (before, after) at the first standalone "With"
WITH_SPLIT = re.compile(r"\bWith\b", re.IGNORECASE)

# Matches a trailing "(YYYY)" disambiguator some episode titles include,
# e.g. "'Kicking and Screaming (1995)'" — useful as a year hint for TMDB,
# but confuses TMDB's text search if left in the query.
YEAR_HINT = re.compile(r"\s*\((\d{4})\)\s*$")


def normalize_title(title):
    """Lowercase, strip punctuation/whitespace differences for duplicate comparison."""
    return re.sub(r"[^a-z0-9]", "", title.lower())


def parse_hosts(host_str):
    host_str = host_str.strip().rstrip('.')
    host_str = re.sub(r',?\s+and\s+', ', ', host_str, flags=re.IGNORECASE)
    return [h.strip() for h in host_str.split(',') if h.strip()]


def parse_episode_title(title):
    """
    Returns (movie_title, year_hint_or_None, hosts_list) or (None, None, None)
    if the title can't be confidently parsed.
    """
    title = title.strip()
    with_match = WITH_SPLIT.search(title)
    if not with_match:
        return None, None, None

    before = title[:with_match.start()]
    after = title[with_match.end():]

    quote_positions = [i for i, ch in enumerate(before) if ch in QUOTE_CHARS]
    if len(quote_positions) < 2:
        return None, None, None  # can't find a clean quoted title — don't guess

    start, end = quote_positions[0], quote_positions[-1]
    movie_title = before[start + 1:end].strip()
    movie_title = movie_title.strip(" ,.\u2013\u2014")  # trailing commas/periods/dashes

    year_hint = None
    year_match = YEAR_HINT.search(movie_title)
    if year_match:
        year_hint = int(year_match.group(1))
        movie_title = YEAR_HINT.sub("", movie_title).strip()

    if not movie_title:
        return None, None, None

    hosts = parse_hosts(after)
    if not hosts:
        return None, None, None

    return movie_title, year_hint, hosts


def parse_pubdate(pubdate_str):
    """
    Converts the RSS pubDate to US/Eastern before extracting the date.
    Megaphone timestamps are typically UTC; late-night episode drops can
    land on the "next" calendar day in UTC even though they're still the
    same day in US time — which caused real date drift against manually
    entered episodes (e.g. an episode logged as Oct 28 parsing as Oct 29).

    zoneinfo needs the IANA tzdata database, which Windows doesn't ship
    by default — without it this would silently fail. Falls back to a
    fixed UTC-5 offset (ignores DST, so may be off by an hour near
    spring/fall transitions, but still gets the calendar date right in
    the vast majority of cases) rather than returning nothing.
    """
    dt = parsedate_to_datetime(pubdate_str)
    try:
        from zoneinfo import ZoneInfo
        dt_local = dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import timedelta, timezone
        dt_local = dt.astimezone(timezone(timedelta(hours=-5)))
    return dt_local.strftime("%Y-%m-%d")


def tmdb_search(title, year_hint=None):
    """
    Returns the best-matching result, or None. TMDB's default result order
    isn't reliably popularity-sorted, especially for short/generic titles —
    e.g. searching "Dodgeball" or "Ocean's 11" can return obscure foreign
    films or shorts ahead of the famous movie. To avoid that, we pull all
    results and pick the one with the highest 'popularity' score, which is
    a much stronger signal than raw result order for disambiguating
    common titles.
    """
    headers = {"Authorization": f"Bearer {TMDB_API_KEY}"}
    params = {"query": title}
    if year_hint:
        params["year"] = year_hint
    resp = requests.get(f"{TMDB_BASE}/search/movie", headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    results = resp.json().get('results', [])
    if not results and year_hint:
        # Retry without the year filter in case it was too restrictive
        params.pop("year")
        resp = requests.get(f"{TMDB_BASE}/search/movie", headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        results = resp.json().get('results', [])
    if not results:
        return None
    return max(results, key=lambda r: r.get('popularity', 0))


def tmdb_details(movie_id):
    headers = {"Authorization": f"Bearer {TMDB_API_KEY}"}
    resp = requests.get(f"{TMDB_BASE}/movie/{movie_id}", headers=headers,
                         params={"append_to_response": "credits"}, timeout=10)
    resp.raise_for_status()
    return resp.json()


def format_runtime(minutes):
    if not minutes:
        return ""
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m"


def build_entry(movie_title, year_hint, hosts, episode_date, next_id):
    """Returns (entry_dict, warning_str_or_None) or (None, warning) on failure."""
    match = tmdb_search(movie_title, year_hint)
    if not match:
        return None, f"No TMDB match found for '{movie_title}' — add manually."

    details = tmdb_details(match['id'])
    director = next(
        (c['name'] for c in details.get('credits', {}).get('crew', []) if c['job'] == 'Director'),
        ''
    )
    cast = [c['name'] for c in details.get('credits', {}).get('cast', [])[:CAST_LIMIT]]
    genres = [g['name'] for g in details.get('genres', [])]
    year = int(details['release_date'][:4]) if details.get('release_date') else None
    runtime = format_runtime(details.get('runtime'))

    warning = None
    if normalize_title(match['title']) != normalize_title(movie_title):
        warning = (f"TMDB matched '{movie_title}' to '{match['title']}' "
                   f"({year}) — verify this is the right movie/year.")
    elif match.get('vote_count', 0) < 50:
        # Title matches cleanly, but this is a low-confidence pick (obscure
        # or foreign film with the same title as a more famous movie).
        # Worth a manual glance even though the text matched.
        warning = (f"'{movie_title}' matched a low-profile TMDB entry "
                   f"({match.get('vote_count', 0)} votes) — double check this "
                   f"is the movie you meant, not an obscure same-named one.")

    entry = {
        "movie_title": match['title'],
        "movie_year": year,
        "director": director,
        "episode_date": episode_date,
        "hosts": hosts,
        "is_rewatch": False,
        "episode_label": "",
        "runtime": runtime,
        "imdb_rating": None,
        "rt_audience_score": None,
        "genre": genres,
        "cast": cast,
        "id": next_id
    }
    return entry, warning


def main():
    if not TMDB_API_KEY:
        print("ERROR: TMDB_API_KEY environment variable is not set.")
        print('  export TMDB_API_KEY="your-tmdb-read-access-token"')
        sys.exit(1)

    print(f"Loading {INPUT_FILE}...")
    with open(INPUT_FILE, encoding='utf-8') as f:
        data = json.load(f)

    # Normalized (title, year) pairs already in the file — used to catch
    # duplicates even when punctuation/formatting differs slightly.
    existing_keys = {
        (normalize_title(ep['movie_title']), ep.get('movie_year'))
        for ep in data
    }
    existing_titles_only = {normalize_title(ep['movie_title']) for ep in data}
    next_id = max(ep['id'] for ep in data) + 1 if data else 1

    print("Fetching RSS feed...")
    resp = requests.get(RSS_URL, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = root.findall('.//item')

    added = []
    warnings = []
    skipped_duplicates = []

    for item in items:
        title_el = item.find('title')
        pubdate_el = item.find('pubDate')
        if title_el is None or not title_el.text:
            continue

        movie_title, year_hint, hosts = parse_episode_title(title_el.text)
        if not movie_title or not hosts:
            continue  # not a parseable episode title — skip, don't guess

        norm = normalize_title(movie_title)
        if norm in existing_titles_only:
            continue  # already have it (by title), manually or from a prior run

        # Before even hitting TMDB: if this looks like a short/partial
        # version of a title already in the file (e.g. "Halloween 4" vs.
        # "Halloween 4: The Return of Michael Myers"), flag it rather than
        # risk TMDB's search failing to find the full title and matching
        # something unrelated instead. Only applied to reasonably long
        # normalized strings (5+ chars) to avoid false positives on short
        # common words.
        if len(norm) >= 5:
            possible_match = next(
                (t for t in existing_titles_only if norm in t or t in norm),
                None
            )
            if possible_match:
                episode_date = parse_pubdate(pubdate_el.text) if pubdate_el is not None else ""
                skipped_duplicates.append((movie_title, "?", episode_date))
                print(f"New episode found: '{movie_title}' ({episode_date or 'date unknown'})")
                print(f"  \u26a0 Skipped pre-TMDB — looks like it may already exist under a longer/different title. Verify manually.")
                continue

        episode_date = parse_pubdate(pubdate_el.text) if pubdate_el is not None else ""

        print(f"New episode found: '{movie_title}' ({episode_date or 'date unknown'}) — looking up on TMDB...")
        entry, warning = build_entry(movie_title, year_hint, hosts, episode_date, next_id)

        if entry is None:
            warnings.append(warning)
            print(f"  \u26a0 {warning}")
            continue

        # Re-check for duplicates using the TMDB-resolved canonical title,
        # since it may differ from the raw RSS text (e.g. added ellipsis,
        # different punctuation) even though it's the same movie.
        resolved_key = (normalize_title(entry['movie_title']), entry['movie_year'])
        if resolved_key in existing_keys:
            skipped_duplicates.append((entry['movie_title'], entry['movie_year'], episode_date))
            print(f"  \u26a0 Skipped — '{entry['movie_title']}' ({entry['movie_year']}) already exists under a different title format.")
            continue

        data.append(entry)
        existing_keys.add(resolved_key)
        existing_titles_only.add(norm)
        added.append(entry)
        next_id += 1
        if warning:
            warnings.append(warning)
            print(f"  \u26a0 {warning}")

    if added:
        with open(INPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    print(f"\nAdded {len(added)} new episode(s):")
    for entry in added:
        cast_preview = ', '.join(entry['cast'][:3]) + ('...' if len(entry['cast']) > 3 else '')
        print(f"  \u2713 {entry['movie_title']} ({entry['movie_year']}) — id {entry['id']}")
        print(f"     Director: {entry['director']} | Runtime: {entry['runtime']} | Cast: {cast_preview}")
        print(f"     Hosts: {', '.join(entry['hosts'])}")
        print(f"     NOTE: imdb_rating / rt_audience_score left blank — fill in by hand")

    if skipped_duplicates:
        print(f"\n{len(skipped_duplicates)} likely duplicate(s) skipped (already in file under different formatting):")
        for title, year, date in skipped_duplicates:
            print(f"  - {title} ({year}) — RSS episode dated {date}")

    if warnings:
        print(f"\n{len(warnings)} item(s) flagged for manual review:")
        for w in warnings:
            print(f"  \u26a0 {w}")

    if not added and not warnings and not skipped_duplicates:
        print("Nothing to do — every RSS episode already has an entry.")

if __name__ == "__main__":
    main()
