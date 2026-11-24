# CodeX-Returns v10 setup (local login)

This launcher uses the existing game engine in `CodeX-Returns.py` and the redesigned `launcher_ui.html` interface. It does **not** require an authentication server.

## Run the launcher

Keep these files together: `Launcher-v10.py`, `launcher_ui.html`, `CodeX-Returns.py`, `create_user.py`, and `launcher_config.example.json`.

```bash
python -m pip install requests pywebview
```

Copy `launcher_config.example.json` to `launcher_config.json` and adjust the game repository if needed. The default accounts path is `Security/users.json`, relative to the launcher folder. Put the generated accounts file there (or change `auth_users_file` to its path), then run:

```bash
python create_user.py
python Launcher-v10.py
```

`create_user.py` prompts for username, display name, and password and writes a salted PBKDF2-HMAC-SHA256 verifier. Add additional accounts by running it again. It never writes a plaintext password. Keep the account file in your separate `Security` repo if you want, then copy/download it to the configured path before launching.

## Important security limits

This provides a basic local convenience gate only. It is not a robust security boundary: someone with the launcher or access to the computer can patch/bypass it, and an offline verifier can be guessed if a password is weak. Use a unique, long password. A public account file does not reveal the original password directly, but exposes usernames and lets attackers attempt offline guesses; keep it private if that matters.

Base64 is encoding, not encryption. A password database encrypted with a key shipped in the launcher would also be recoverable. This implementation stores salted, deliberately slow password hashes instead of plaintext or reversible encryption. The game repository remains separate from the account data.

A private GitHub `Security` repo cannot be fetched anonymously. Without an authentication service, keep a local clone/copy of `Security/users.json` at the configured path. Do not put a GitHub access token in the launcher. Local login only gates this running desktop UI; it cannot securely restrict the games to authorized users.

## UI features

Home with featured games and session overview, searchable games page, stats/device snapshot, favorites, active local ports, local clock, best-effort battery status, animated interactive ambient particles, and reduced-motion support. Browser privacy settings may prevent reporting battery, CPU, or memory data. Favorites are saved in browser local storage; launch history is only kept for the current session.
