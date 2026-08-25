// When these pages are opened from a local dev server (e.g.
// `python -m http.server` from docs/, or the "run" skill), point at a
// locally-running API (see api.py's ATHLETICS_DB_PATH env var for testing
// against athletics_test.db) instead of the deployed production API --
// no manual toggling needed when switching between local testing and the
// live site.
const API_BASE_URL =
  (location.hostname === "localhost" || location.hostname === "127.0.0.1")
    ? "http://127.0.0.1:8000"
    : "https://has-web-scraper-novi.onrender.com";
