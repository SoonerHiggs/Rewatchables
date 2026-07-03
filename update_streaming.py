"""
Rewatchables Streaming Update Script
=====================================
Fetches streaming availability for all movies in rewatchables.json
using the Watchmode API and saves to streaming_data.json.

SETUP:
  1. Place this script in the same folder as rewatchables.json
  2. Set the WATCHMODE_API_KEY environment variable (get a key at watchmode.com)
       - Local (bash/zsh):  export WATCHMODE_API_KEY="your-key-here"
       - Local (Windows):   setx WATCHMODE_API_KEY "your-key-here"  (new terminal after)
       - GitHub Actions:    stored as a repo secret, injected automatically
  3. Run weekly: python update_streaming.py --recent
     Run monthly: python update_streaming.py --full

OUTPUT:
  streaming_data.json in the same folder
  Then upload streaming_data.json to GitHub (or let the Action commit it).
"""

import json, time, os, sys
from datetime import date, datetime, timedelta

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library not found.")
    print("Please run: pip install requests")
    exit(1)

# ── API KEY ──────────────────────────────────────────────────────────────────
# Never hardcode the key here. Set it as an environment variable instead:
#   export WATCHMODE_API_KEY="your-key-here"
WATCHMODE_API_KEY = os.environ.get("WATCHMODE_API_KEY")
# ─────────────────────────────────────────────────────────────────────────────

INPUT_FILE  = "rewatchables.json"
OUTPUT_FILE = "streaming_data.json"
DELAY       = 0.5   # seconds between API calls (to avoid rate limiting)

# Services to include (case-insensitive match against Watchmode service names)
SERVICES_TO_INCLUDE = {
    # Subscription / Free streaming
    "netflix", "max", "hulu", "prime video", "amazon prime video",
    "peacock", "apple tv+", "disney+", "tubi", "pluto tv",
    "paramount+", "showtime", "starz", "mubi", "kanopy",
    "criterion channel", "mgm+",
    # Rent / Buy platforms
    "amazon video", "apple tv", "vudu", "fandango at home",
    "google play movies", "youtube", "microsoft store",
    "directv", "amc on demand", "redbox", "spectrum on demand"
}

def get_unique_movies(data):
    """Get one entry per unique movie (by title+year), skipping rewatch duplicates."""
    seen = set()
    unique = []
    for ep in sorted(data, key=lambda x: x['episode_date']):
        key = f"{ep['movie_title']}_{ep['movie_year']}"
        if key not in seen:
            seen.add(key)
            unique.append(ep)
    return unique

def get_recent_movies(data, unique, streaming, days=30):
    """Get movies added to the JSON in the last N days, plus any with no streaming data."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    recent_keys = set()

    # Movies with episode dates in the last N days
    for ep in data:
        if ep['episode_date'] >= cutoff:
            key = f"{ep['movie_title']}_{ep['movie_year']}"
            recent_keys.add(key)

    # Movies with no streaming data yet
    existing_keys = set(streaming['movies'].keys())
    for m in unique:
        key = f"{m['movie_title']}_{m['movie_year']}"
        if key not in existing_keys:
            recent_keys.add(key)

    # Filter unique to only recent/missing
    return [m for m in unique if f"{m['movie_title']}_{m['movie_year']}" in recent_keys]

def search_movie(title, year):
    """Search Watchmode for a movie and return its Watchmode ID."""
    url = f"https://api.watchmode.com/v1/search/?apiKey={WATCHMODE_API_KEY}&search_field=name&search_value={requests.utils.quote(title)}"
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        raise Exception(f"Search HTTP {resp.status_code}: {resp.text[:100]}")
    results = resp.json().get('title_results', [])
    # Try to find exact year match first
    match = next((r for r in results if r.get('year') == year and r.get('type') == 'movie'), None)
    if not match:
        match = next((r for r in results if r.get('type') == 'movie'), None)
    return match['id'] if match else None

def get_sources(watchmode_id):
    """Get streaming sources for a Watchmode title ID."""
    url = f"https://api.watchmode.com/v1/title/{watchmode_id}/sources/?apiKey={WATCHMODE_API_KEY}&region=US"
    resp = requests.get(url, timeout=10)
    if resp.status_code != 200:
        raise Exception(f"Sources HTTP {resp.status_code}: {resp.text[:100]}")
    return resp.json()

def filter_and_dedupe_sources(sources):
    """Filter to known services, dedupe, and sort by type (free first, then sub, rent, buy)."""
    seen = set()
    filtered = []
    for s in sources:
        name = s.get('name', '').lower()
        if name not in SERVICES_TO_INCLUDE:
            continue
        key = f"{name}_{s.get('type')}"
        if key not in seen:
            seen.add(key)
            filtered.append({
                "service": s.get('name'),
                "type": s.get('type'),
                "url": s.get('web_url', ''),
                "price": s.get('price')
            })
    # Sort: free first, then sub, then rent, then buy
    order = {'free': 1, 'sub': 2, 'subscription': 2, 'rent': 3, 'buy': 4}
    return sorted(filtered, key=lambda x: order.get(x['type'], 5))

def load_existing():
    """Load existing streaming_data.json if it exists (for resuming)."""
    if os.path.exists(OUTPUT_FILE):
        with open(OUTPUT_FILE, encoding='utf-8') as f:
            return json.load(f)
    return {"last_updated": "", "movies": {}}

def save(data):
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def test_connection():
    print("Testing Watchmode API connection...")
    url = f"https://api.watchmode.com/v1/status/?apiKey={WATCHMODE_API_KEY}"
    try:
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            info = resp.json()
            remaining = info.get('requests_remaining', '?')
            print(f"Connected! Requests remaining this month: {remaining}\n")
            return True
        else:
            print(f"API error {resp.status_code}: {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"Connection error: {e}")
        return False

def main():
    if not WATCHMODE_API_KEY:
        print("ERROR: WATCHMODE_API_KEY environment variable is not set.")
        print('  Local:  export WATCHMODE_API_KEY="your-key-here"')
        print("  Action: add it as a repo secret named WATCHMODE_API_KEY")
        return

    if not test_connection():
        return

    print(f"Loading {INPUT_FILE}...")
    with open(INPUT_FILE, encoding='utf-8') as f:
        data = json.load(f)

    unique = get_unique_movies(data)
    print(f"Unique movies to check: {len(unique)}")

    streaming = load_existing()
    already_done = set(streaming['movies'].keys())

    if "--recent" in sys.argv:
        remaining = get_recent_movies(data, unique, streaming, days=30)
        print(f"Recent mode — fetching {len(remaining)} new/recent movies...\n")
    else:
        if already_done:
            print(f"Resuming — {len(already_done)} already fetched\n")
        remaining = [m for m in unique if f"{m['movie_title']}_{m['movie_year']}" not in already_done]
        print(f"Fetching streaming data for {len(remaining)} movies...\n")

    done = len(already_done)
    errors = []

    for i, movie in enumerate(remaining):
        key = f"{movie['movie_title']}_{movie['movie_year']}"
        title = movie['movie_title']
        year = movie['movie_year']

        try:
            watchmode_id = search_movie(title, year)
            if watchmode_id:
                sources = get_sources(watchmode_id)
                filtered = filter_and_dedupe_sources(sources)
                streaming['movies'][key] = {"sources": filtered}
                service_names = [s['service'] for s in filtered] or ['None found']
                print(f"✓ ({done+1}/{len(unique)}) {title} ({year}): {', '.join(service_names)}")
            else:
                streaming['movies'][key] = {"sources": []}
                print(f"- ({done+1}/{len(unique)}) {title} ({year}): Not found in Watchmode")
            done += 1
        except Exception as e:
            errors.append(f"{title} ({year}): {str(e)[:80]}")
            print(f"✗ ({done+1}/{len(unique)}) {title} ({year}): {str(e)[:80]}")
            done += 1

        # Save progress every 10 movies
        if done % 10 == 0:
            streaming['last_updated'] = str(date.today())
            save(streaming)

        time.sleep(DELAY)

    streaming['last_updated'] = str(date.today())
    save(streaming)

    print(f"\nDone! {done} movies processed, {len(errors)} errors.")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  - {e}")
    print(f"\nUpload {OUTPUT_FILE} to GitHub to update the live site.")

if __name__ == "__main__":
    if "--full" in sys.argv:
        print("Full refresh mode - clearing existing streaming data...")
        if os.path.exists(OUTPUT_FILE):
            os.remove(OUTPUT_FILE)
            print(f"Deleted {OUTPUT_FILE}")
            print()
    elif "--recent" in sys.argv:
        print("Recent mode - only fetching new/recent movies (last 30 days)...")
        print("Use --full once a month for a complete refresh.\n")
    main()
