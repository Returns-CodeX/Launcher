# CodeX-Returns v10 setup

This adds a redesigned launcher UI while reusing the existing downloader in `CodeX-Returns.py`.

## Install and run

1. Keep `Launcher-v10.py`, `launcher_ui.html`, `CodeX-Returns.py`, and `launcher_config.example.json` together.
2. Install the existing dependencies: `python -m pip install requests pywebview`.
3. Copy `launcher_config.example.json` to `launcher_config.json` and adjust the games repository settings if needed.
4. Configure an HTTPS authentication service URL in `launcher_config.json` (`auth_api_url`) or set `CODEX_AUTH_API_URL` in the environment.
5. Run `python Launcher-v10.py`.

The launcher uses the current game API (sync, launch, progress, stop port, quote) and displays the UI in pywebview. Game sessions remain bound to localhost. Cached games and the game index continue to use `.cache/`.

## Authentication service and private repositories

The authentication service is separate from the game repository. It must provide:

- `POST {auth_api_url}/lookup` with `{"username":"..."}`. Return `{"ok":true,"registered":true,"display_name":"..."}` for a known user, or `{"ok":true,"registered":false}` otherwise.
- `POST {auth_api_url}/login` with `{"username":"...","password":"..."}`. Return `{"ok":true,"display_name":"..."}` only after validating the account; return a non-2xx response or `{"ok":false,"error":"..."}` on rejection.

Passwords are sent over HTTPS directly to that service and are not written to launcher files or disk. Use a server-side password hash (for example Argon2id or bcrypt), request throttling, and generic login failure responses. The service must not return password hashes or credential records to the launcher.

A private GitHub repository does not make its files anonymously accessible: the launcher would need credentials to fetch them. **Do not embed a GitHub PAT, encryption key, or password database in a desktop app or a public launcher repo.** Anyone with the app can extract embedded secrets, and client-side encryption cannot hide a decryption key shipped with that app. Use a server-side authentication service to access private data. A separate `Security` repository may hold non-secret configuration; it is not a secure place for secrets if the repo is public, and private-repo access still needs server-side credentials.

If no auth service is configured, the login page reports that sign-in is not configured and does not proceed. This sign-in is for the local launcher UI; it is not a substitute for OS account security or a hosted, multi-user access-control system.

## Device stats and privacy

Clock is local time. Battery, CPU and memory are displayed only when the embedded browser exposes them; browser privacy restrictions may make some hardware data unavailable. Favorites are stored in browser local storage. Launch history is session-only. The launcher binds its UI server to `127.0.0.1`.
