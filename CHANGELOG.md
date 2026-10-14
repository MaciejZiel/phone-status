# Changelog

## Unreleased

### Fixed
- The page, login and administrator pages now load `app.js` and `app.css` with a content hash in the URL, so a browser or CDN
  that cached an older copy picks up a new deploy immediately.

### Added
- Administrator view (`/admin`, `/api/admin/status`) with every collected host figure (power, temperature, exact bytes, cores, load, network)
  and the full text of failed checks, behind a login with `SERVER_STATUS_ADMIN_TOKEN`. Disabled (404) when the token is unset.
  Sessions in memory (12 h, HttpOnly/Secure/SameSite=Strict cookie), login limited to 5 attempts per 5 minutes per visitor.
- Public page shows the same host layout with fixed placeholder values, blurred, and a note that details are visible to the administrator.
- nginx snippets for the administrator paths (no cache, login rate limit) and a generated-on-the-server token recipe.
- `tools/demo_app.py`: demo with an invented host, so screenshots never show real figures.

### Changed
- The public JSON host object gains an `extra` list of placeholder cards. Real values are still never in it.
