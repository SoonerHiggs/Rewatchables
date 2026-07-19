"""
Rewatchables Missing Episode Auto-Add Script
=====================================
Safety net for weeks you don't get to manually add the episode entry
before it airs. Fetches the official Rewatchables RSS feed and, for any
episode not already in rewatchables.json, creates a full entry using
TMDB data.

WHAT IT FILLS IN AUTOMATICALLY (from TMDB):
  title, year, director, runtime, genre, full top-billed cast, hosts (from RSS)

WHAT IT LEAVES BLANK, ON PURPOSE:
  imdb_rating, rt_audience_score — not reliably available from a free API.
  Fill these in by hand when convenient; this just makes sure the entry
  exists at all rather than being missing entirely.

CAVEAT WORTH KNOWING:
  TMDB's genre taxonomy differs from IMDb's (e.g. no "Biography" or "Sport"
  categories — TMDB might tag Ali as "Drama, History" instead). Auto-added
  entries' genre tags may not exactly match ones you added by hand using
  IMDb's genre list. Not wrong, just a different vocabulary — worth a
  glance if consistency matters to you.

  If the RSS episode title doesn't cleanly match a TMDB search result,
  the entry is still added but flagged in the output for you to verify —
  it is NOT silently guessed at.

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

# Matches: 'Title' [anything] With Host1, Host2, and Host3
TITLE_PATTERN = re.compile(
    r"^[\u2018']([^\u2019']+)[\u2019'].*?\bWith\s+(.+)$",
    re.IGNORECASE
)

def parse_hosts(host_str):
    host_str = host_str.strip().rstrip('.')
    host_str = re.sub(r',?\s+and\s+', ', ', host_str, flags=re.IGNORECASE)
    return [h.strip() for h in host_str.split(',') if h.strip()]

def parse_episode_title(title):
    """Returns (movie_title, hosts_list) or (None, None) if unparseable."""
    match = TITLE_PATTERN.match(title.strip())
    if not match:
        return None, None
    movie_title = match.group(1).strip()
    hosts = parse_hosts(match.group(2))
    return movie_title, hosts

def parse_pubdate(pubdate_str):
    """Handles standard RSS pubDate formats, numeric or named timezone."""
    try:
        dt = parsedate_to_datetime(pubdate_str)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return ""

def tmdb_search(title):
    headers = {"Authorization": f"Bearer {TMDB_API_KEY}"}
    resp = requests.get(f"{TMDB_BASE}/search/movie", headers=headers,
                         params={"query": title}, timeout=10)
    resp.raise_for_status()
    results = resp.json().get('results', [])
    return results[0] if results else None

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

def build_entry(movie_title, hosts, episode_date, next_id):
    """Returns (entry_dict, warning_str_or_None) or (None, warning) on failure."""
    match = tmdb_search(movie_title)
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
    if match['title'].lower() != movie_title.lower():
        warning = (f"TMDB matched '{movie_title}' to '{match['title']}' "
                   f"({year}) — verify this is the right movie/year.")

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

    existing_titles = {ep['movie_title'].strip().lower() for ep in data}
    next_id = max(ep['id'] for ep in data) + 1 if data else 1

    print("Fetching RSS feed...")
    resp = requests.get(RSS_URL, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    items = root.findall('.//item')

    added = []
    warnings = []

    for item in items:
        title_el = item.find('title')
        pubdate_el = item.find('pubDate')
        if title_el is None or not title_el.text:
            continue

        movie_title, hosts = parse_episode_title(title_el.text)
        if not movie_title or not hosts:
            continue  # not a parseable episode title — skip, don't guess

        if movie_title.strip().lower() in existing_titles:
            continue  # already have it, manually or from a prior run

        episode_date = parse_pubdate(pubdate_el.text) if pubdate_el is not None else ""

        print(f"New episode found: '{movie_title}' ({episode_date or 'date unknown'}) — looking up on TMDB...")
        entry, warning = build_entry(movie_title, hosts, episode_date, next_id)

        if entry is None:
            warnings.append(warning)
            print(f"  ⚠ {warning}")
            continue

        data.append(entry)
        existing_titles.add(entry['movie_title'].strip().lower())
        added.append(entry)
        next_id += 1
        if warning:
            warnings.append(warning)
            print(f"  ⚠ {warning}")

    if added:
        with open(INPUT_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)

    print(f"\nAdded {len(added)} new episode(s):")
    for entry in added:
        cast_preview = ', '.join(entry['cast'][:3]) + ('...' if len(entry['cast']) > 3 else '')
        print(f"  ✓ {entry['movie_title']} ({entry['movie_year']}) — id {entry['id']}")
        print(f"     Director: {entry['director']} | Runtime: {entry['runtime']} | Cast: {cast_preview}")
        print(f"     Hosts: {', '.join(entry['hosts'])}")
        print(f"     NOTE: imdb_rating / rt_audience_score left blank — fill in by hand")

    if warnings:
        print(f"\n{len(warnings)} item(s) flagged for manual review:")
        for w in warnings:
            print(f"  ⚠ {w}")

    if not added and not warnings:
        print("Nothing to do — every RSS episode already has an entry.")

if __name__ == "__main__":
    main()
