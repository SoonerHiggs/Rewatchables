const WATCHMODE_API_KEY = 'Hf0Z1GLibecKj7Vnce9hcdYb7JvDPfHcMF8egMB1';

async function getStreamingInfo(movieTitle, movieYear) {
  try {
    const searchUrl = `https://api.watchmode.com/v1/search/?apiKey=${WATCHMODE_API_KEY}&search_field=name&search_value=${encodeURIComponent(movieTitle)}`;
    const searchResponse = await fetch(searchUrl);
    const searchData = await searchResponse.json();

    if (!searchData.title_results || searchData.title_results.length === 0) {
      return null;
    }

    const match = searchData.title_results.find(t =>
      t.year === movieYear && t.type === 'movie'
    ) || searchData.title_results[0];

    const sourcesUrl = `https://api.watchmode.com/v1/title/${match.id}/sources/?apiKey=${WATCHMODE_API_KEY}`;
    const sourcesResponse = await fetch(sourcesUrl);
    const sourcesData = await sourcesResponse.json();

    // Deduplicate — keep only one entry per service name, preferring HD
    const seen = {};
    const deduped = sourcesData.filter(source => {
      if (seen[source.name + source.type]) return false;
      seen[source.name + source.type] = true;
      return true;
    });

    // Sort by cost: free first, then subscription, then rent, then buy
    const order = { 'free': 1, 'sub': 2, 'subscription': 2, 'rent': 3, 'buy': 4 };
    const sorted = deduped.sort((a, b) =>
      (order[a.type] || 5) - (order[b.type] || 5)
    );

    return sorted;

  } catch (error) {
    console.error('Watchmode error:', error);
    return null;
  }
}

function formatSources(sources) {
  if (!sources || sources.length === 0) {
    return '<p class="no-sources">No streaming options found.</p>';
  }

  const labels = {
    'free':         { text: 'Free',         color: '#4caf50' },
    'sub':          { text: 'Subscription', color: '#2196f3' },
    'subscription': { text: 'Subscription', color: '#2196f3' },
    'rent':         { text: 'Rent',         color: '#ff9800' },
    'buy':          { text: 'Buy',          color: '#9e9e9e' }
  };

  return sources.slice(0, 8).map(source => {
    const label = labels[source.type] || { text: source.type, color: '#888' };
    const price = source.price ? ` · $${source.price}` : '';
    const link = source.web_url ? `href="${source.web_url}" target="_blank"` : '';
    return `
      <div class="source-item">
        <span class="source-type" style="color: ${label.color}">${label.text}${price}</span>
        <a class="source-name" ${link}>${source.name}</a>
      </div>
    `;
  }).join('');
}