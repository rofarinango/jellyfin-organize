# jellyfin-organize

Local web UI that renames media files to Jellyfin's naming convention and sends them to a Raspberry Pi media server over SSH, with TMDB lookups and AI-assisted filename parsing.

## How it works

1. Drop video files into `~/Documents/Jellyfin Content` on your Mac.
2. Open the web UI. Each file gets a proposed type (movie/show), title, year, season and episode, parsed from its filename.
3. Fix anything that's off, using **🔎 Buscar en TMDB** to confirm the correct title/year or **✨ Inferir con IA** for messy filenames.
4. Click **Enviar a la Pi**. The file is copied over SSH into the right Jellyfin folder, with a live progress bar and an MD5 checksum check.
5. Once you've confirmed it plays in Jellyfin, click **🗑️ Borrar original en Mac** to delete the local copy.

Files end up laid out the way Jellyfin expects:

```
/mnt/external/StreamingContent/
├── Movies/
│   └── Dune: Part Two (2024)/
│       └── Dune: Part Two (2024).mkv
└── Shows/
    └── Bleach (2004)/
        └── Season 02/
            └── Bleach S02E49.mp4
```

## Features

- **Filename parsing:** detects `S01E02` patterns, years and release tags (`1080p`, `x265`, `WEB-DL`, …), plus fuzzy patterns like `2seeeason-14`.
- **AI fallback:** calls `claude -p` with a JSON schema to work out the title, year, season and episode from obfuscated filenames. Results are cached in `.infer_cache.json`.
- **TMDB integration:** search movies and series and browse a show's seasons, so the folder year matches what Jellyfin's metadata expects.
- **Safe transfer:** uploads to a temp file on the Pi, moves it into place, sets `jellyfin:jellyfin` ownership and `644` permissions, and checks local vs. remote MD5.
- **Live progress:** transfers run in a background thread, and progress comes from polling the size of the remote temp file.
- **Sent registry:** `.sent_registry.json` records what was sent and where. Originals can only be deleted after a successful send.
- **No dependencies:** Python standard library plus a Bash script.

## Requirements

- macOS (the script uses `md5` and `uuidgen`)
- Python 3.8+
- SSH access to the Pi via a host alias named `jellyfin-pi`, with passwordless `sudo` for `mkdir`, `mv`, `chown` and `chmod`
- A [TMDB API key](https://www.themoviedb.org/settings/api) (optional, for lookups)
- [Claude Code](https://claude.com/claude-code) CLI on your `PATH` (optional, for AI inference)

Example `~/.ssh/config` entry:

```
Host jellyfin-pi
    HostName 192.168.1.50
    User pi
    IdentityFile ~/.ssh/id_ed25519
```

## Setup

```bash
git clone <repo-url> jellyfin-organize
cd jellyfin-organize
chmod +x organize_jellyfin.sh
cp config.example.json config.json   # then add your TMDB key
python3 app.py
```

Then open <http://localhost:8787>.

## Configuration

Paths and the host alias are constants at the top of each file:

| Setting | File | Default |
|---|---|---|
| Source folder | `app.py`, `organize_jellyfin.sh` | `~/Documents/Jellyfin Content` |
| SSH host alias | `app.py`, `organize_jellyfin.sh` | `jellyfin-pi` |
| Movies path on Pi | `organize_jellyfin.sh` | `/mnt/external/StreamingContent/Movies` |
| Shows path on Pi | `organize_jellyfin.sh` | `/mnt/external/StreamingContent/Shows` |
| File owner on Pi | `organize_jellyfin.sh` | `jellyfin:jellyfin` |
| Web UI port | `app.py` | `8787` |

## CLI usage

The transfer script also works on its own:

```bash
./organize_jellyfin.sh movie "Dune.Part.Two.2024.mkv" "Dune: Part Two" 2024
./organize_jellyfin.sh show "Bleach TYBW S01E49.mp4" "Bleach" 2004 2 49
```

The file argument is just the filename inside the source folder. For shows, use the year TMDB lists for the series entry, which isn't always the franchise's original release year.

## Project structure

```
app.py                 HTTP server + API (parsing, TMDB, AI inference, transfer jobs)
index.html             Single-page web UI
organize_jellyfin.sh   SSH/SCP transfer with checksum verification
config.json            TMDB API key (do not commit)
.infer_cache.json      Cached AI inferences (generated)
.sent_registry.json    Record of transferred files (generated)
```

## Notes

- The server only listens on `127.0.0.1` and has no authentication, so keep it local.
- Keep `config.json`, `.infer_cache.json`, `.sent_registry.json` and `__pycache__/` out of version control.
