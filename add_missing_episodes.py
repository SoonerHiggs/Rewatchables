"""
Rewatchables Missing Episode Auto-Add Script  (v3 - fixed sequel false-positive)
=====================================
Safety net for weeks you don't get to manually add the episode entry
before it airs. Fetches the official Rewatchables RSS feed and, for any
episode not already in rewatchables.json, creates a full entry using
TMDB data.

WHAT IT FILLS IN AUTOMATICALLY (from TMDB):
  title, year, director, runtime, genre, full top-billed cast, hosts (from RSS)

WHAT IT LEAVES BLANK, ON PURPOSE:
  imdb_rating, rt_audience_score — not reliably available from a free API.

v3 CHANGE — removed the pre-TMDB substring safety check from v2:
  v2 added a check that skipped any new title containing (or contained by)
  an existing title's normalized form, meant to catch cases like
  "Halloween 4" vs. an existing "Halloween 4: The Return of Michael Myers".
  This worked for that case, but it also silently blocked EVERY sequel
  whose title starts with an existing movie's name — "The Karate Kid
  Part II" contains "The Karate Kid" (already in the file from 2020), so
  it never got added when the episode aired. Since Rewatchables covers a
  lot of franchise sequels, this was a bigger problem than the one it
  fixed.

  v3 removes that pre-check entirely and relies solely on comparing the
  TMDB-RESOLVED canonical title against existing entries (exact
  normalized match, not substring) — this still catches genuine
  duplicates like "Halloween 4" -> "Halloween 4: The Return of Michael
  Myers" (since that's an exact match once resolved), without falsely
  blocking "The Karate Kid Part II" (which does NOT exactly match "The
  Karate Kid" once resolved, since they're different TMDB titles).

  A case like "Victory" (2024, distinct Korean film) resolving to
  "Escape to Victory" (1981, already in file, different movie) remains a
  genuine edge case no automated check can safely resolve — TMDB's
  popularity-based matching may occasionally still need manual review
  for two different movies with very similar short titles. That's
  inherent ambiguity, not a bug to engineer around.

TITLE PARSING (v2, unchanged):
  Splits on the first standalone "With", then takes everything between
  the first and last quote character before it — handles apostrophes
  inside titles (Ferris Bueller's Day Off, You've Got Mail) correctly.

TMDB MATCHING (v2, unchanged):
  Picks the highest-popularity search result rather than the first one,
  to avoid obscure same-named films outranking the famous movie.

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

QUOTE_CHARS = "'\u2018\u2019\""
WITH_SPLIT = re.compile(r"\bWith\b", re.IGNORECASE)
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
        return None, None, None

    start, end = quote_positions[0], quote_positions[-1]
    movie_title = before[start + 1:end].strip()
    movie_title = movie_title.strip(" ,.\u2013\u2014")

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
    """Converts to US/Eastern; falls back to fixed UTC-5 if tzdata isn't available (common on Windows)."""
    dt = parsedate_to_datetime(pubdate_str)
    try:
        from zoneinfo import ZoneInfo
        dt_local = dt.astimezone(ZoneInfo("America/New_York"))
    except Exception:
        from datetime import timedelta, timezone
        dt_local = dt.astimezone(timezone(timedelta(hours=-5)))
    return dt_local.strftime("%Y-%m-%d")


def tmdb_search(title, year_hint=None):
    """Returns the highest-popularity matching result, or None."""
    headers = {"Authorization": f"Bearer {TMDB_API_KEY}"}
    params = {"query": title}
    if year_hint:
        params["year"] = year_hint
    resp = requests.get(f"{TMDB_BASE}/search/movie", headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    results = resp.json().get('results', [])
    if not results and year_hint:
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
            continue  # already have it (exact match), manually or from a prior run

        episode_date = parse_pubdate(pubdate_el.text) if pubdate_el is not None else ""

        print(f"New episode found: '{movie_title}' ({episode_date or 'date unknown'}) — looking up on TMDB...")
        entry, warning = build_entry(movie_title, year_hint, hosts, episode_date, next_id)

        if entry is None:
            warnings.append(warning)
            print(f"  \u26a0 {warning}")
            continue

        # Duplicate check happens ONLY here, against the TMDB-resolved
        # canonical title — exact normalized match, not substring. This
        # correctly catches "Halloween 4" -> "Halloween 4: The Return of
        # Michael Myers" (already in file) without falsely blocking
        # sequels like "The Karate Kid Part II" (not an exact match to
        # the existing "The Karate Kid" entry).
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
