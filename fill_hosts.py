"""
Rewatchables Host Fill Script
=====================================
Fetches the official Rewatchables podcast RSS feed and fills in the
`hosts` field for any entries in rewatchables.json that are currently
missing it (hosts: []), by parsing episode titles.

The Rewatchables episode titles consistently follow the pattern:
  'Movie Title' [optional subtitle like "Live From Boston"] With Host1, Host2, and Host3

SETUP:
  1. Place this script in the same folder as rewatchables.json
  2. Run: python fill_hosts.py

WHAT IT DOES:
  - Only fills entries where hosts is currently an empty list.
  - Never overwrites hosts that are already filled in.
  - Matches episodes to your JSON by movie title (case-insensitive).
  - If an episode's title can't be confidently parsed, or no match is
    found in rewatchables.json, it's skipped and reported — never guessed.

OUTPUT:
  Updates rewatchables.json in place.
  Prints a summary of what was filled and what needs manual attention.
"""

import json, re, sys
import xml.etree.ElementTree as ET

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not found.")
    print("Please run: pip install requests")
    exit(1)

RSS_URL = "https://feeds.megaphone.fm/the-rewatchables"
INPUT_FILE = "rewatchables.json"

# Matches: 'Title' [anything] With Host1, Host2, and Host3
# Handles both straight and curly quotes around the title.
TITLE_PATTERN = re.compile(
    r"^[\u2018']([^\u2019']+)[\u2019'].*?\bWith\s+(.+)$",
    re.IGNORECASE
)

def fetch_feed():
    print(f"Fetching RSS feed from {RSS_URL}...")
    resp = requests.get(RSS_URL, timeout=15)
    resp.raise_for_status()
    return ET.fromstring(resp.content)

def parse_hosts(host_str):
    """Turn 'Bill Simmons, Chris Ryan, and Kyle Brandt' into a list of names."""
    host_str = host_str.strip().rstrip('.')
    # Remove a leading "and " on the last item, then split on commas
    host_str = re.sub(r',?\s+and\s+', ', ', host_str, flags=re.IGNORECASE)
    hosts = [h.strip() for h in host_str.split(',') if h.strip()]
    return hosts

def parse_episode_title(title):
    """Returns (movie_title, hosts_list) or (None, None) if pattern doesn't match."""
    match = TITLE_PATTERN.match(title.strip())
    if not match:
        return None, None
    movie_title = match.group(1).strip()
    hosts = parse_hosts(match.group(2))
    return movie_title, hosts

def main():
    print(f"Loading {INPUT_FILE}...")
    with open(INPUT_FILE, encoding='utf-8') as f:
        data = json.load(f)

    # Only care about entries missing hosts
    missing = [ep for ep in data if not ep.get('hosts')]
    if not missing:
        print("Nothing to do — every entry already has hosts filled in.")
        return

    print(f"{len(missing)} entries missing hosts. Checking RSS feed...\n")
    missing_by_title = {ep['movie_title'].lower(): ep for ep in missing}

    root = fetch_feed()
    items = root.findall('.//item')

    filled = []
    unparsed = []

    for item in items:
        title_el = item.find('title')
        if title_el is None or not title_el.text:
            continue
        raw_title = title_el.text

        movie_title, hosts = parse_episode_title(raw_title)
        if not movie_title or not hosts:
            continue  # not an episode title we can confidently parse — skip, don't guess

        key = movie_title.lower()
        if key in missing_by_title:
            ep = missing_by_title[key]
            ep['hosts'] = hosts
            filled.append((ep['movie_title'], ep['episode_date'], hosts))
            del missing_by_title[key]  # avoid double-matching

    with open(INPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

    print(f"Filled {len(filled)} entries:")
    for title, date, hosts in filled:
        print(f"  ✓ {title} ({date}): {', '.join(hosts)}")

    if missing_by_title:
        print(f"\n{len(missing_by_title)} entries still missing hosts (no confident match in feed):")
        for title, ep in missing_by_title.items():
            print(f"  - {ep['movie_title']} ({ep['episode_date']}) — check manually")

    print(f"\nSaved {INPUT_FILE}.")

if __name__ == "__main__":
    main()
