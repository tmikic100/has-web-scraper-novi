// Shared client-side cache for the athlete/club lists (~10k rows) so they're
// fetched once per browser rather than re-fetched by every page, plus the
// site-wide dropdown-vs-classic search preference set on the home page.

const CACHE_TTL_MS = 24 * 60 * 60 * 1000; // matches the weekly data refresh cadence closely enough

async function getCachedList(key, url) {
  const cached = localStorage.getItem(key);
  if (cached) {
    try {
      const parsed = JSON.parse(cached);
      if (Date.now() - parsed.timestamp < CACHE_TTL_MS) {
        return parsed.data;
      }
    } catch (e) {
      // fall through to refetch
    }
  }
  const res = await fetch(url);
  const data = await res.json();
  localStorage.setItem(key, JSON.stringify({ timestamp: Date.now(), data }));
  return data;
}

// Unicode property escape avoids hand-listing a combining-mark codepoint range.
function removeDiacritics(str) {
  return str.normalize("NFD").replace(/\p{Diacritic}/gu, "");
}

function getSearchMode() {
  return localStorage.getItem("searchMode") || "dropdown";
}

function setSearchMode(mode) {
  localStorage.setItem("searchMode", mode);
}