#!/usr/bin/env python3
"""
YTMusicManager.py

A small interactive manager around yt-dlp for pulling down an artist's
discography as audio, while keeping a persistent "ignore list" (yt-dlp's
download-archive) so already-downloaded tracks are never re-fetched, and
so you can manually blocklist specific videos you never want pulled.

After each download, the artist's folder is (re)tagged so that every file's
Album and Album/Performer (album-artist) tags match the artist name you
configured -- regardless of whatever metadata YouTube happened to embed.

Run it, point it at your music folder, and use the menu.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default working folder. Override by passing a path as the first CLI arg,
# or by setting the YTMUSIC_DIR environment variable.
DEFAULT_BASE_DIR = "/home/user/Music/YTMusic/"

ARTISTS_FILENAME = "artists.json"
ARCHIVE_FILENAME = "archive.txt"          # yt-dlp --download-archive (the real ignore list)
MANUAL_IGNORE_FILENAME = "manual_ignore.txt"  # human-readable log of manually blocked videos

COOKIES_BROWSER = "chrome"

# Extensions we'll attempt to tag after a download. yt-dlp's `-x -f bestaudio`
# without --audio-format keeps whatever codec YouTube served (usually opus,
# sometimes m4a), so we just try anything mutagen might recognize.
AUDIO_EXTENSIONS = {".opus", ".m4a", ".mp3", ".ogg", ".flac", ".wav", ".webm"}

# Matches the standard 11-char YouTube video id out of most URL shapes.
YT_ID_RE = re.compile(r"(?:v=|/|^)([A-Za-z0-9_-]{11})(?:[&?/]|$)")


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.artists_path = base_dir / ARTISTS_FILENAME
        self.archive_path = base_dir / ARCHIVE_FILENAME
        self.manual_ignore_path = base_dir / MANUAL_IGNORE_FILENAME
        self._ensure_files()

    def _ensure_files(self):
        self.base_dir.mkdir(parents=True, exist_ok=True)
        if not self.artists_path.exists():
            self.artists_path.write_text("[]", encoding="utf-8")
        if not self.archive_path.exists():
            self.archive_path.touch()
        if not self.manual_ignore_path.exists():
            self.manual_ignore_path.touch()

    def load_artists(self):
        try:
            return json.loads(self.artists_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def save_artists(self, artists):
        self.artists_path.write_text(json.dumps(artists, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_folder_name(name: str) -> str:
    """Turn an artist name into a safe folder name."""
    name = name.strip()
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name)
    return name or "Unknown Artist"


def check_yt_dlp_available():
    if shutil.which("yt-dlp") is None:
        print("\n[!] yt-dlp was not found on your PATH.")
        print("    Install it first, e.g.:  pip install -U yt-dlp\n")
        return False
    return True


def check_mutagen_available():
    try:
        import mutagen  # noqa: F401
        return True
    except ImportError:
        print("\n[!] The 'mutagen' library is not installed, so Album")
        print("    tags can't be fixed up after download.")
        print("    Install it with:  pip install mutagen\n")
        return False


def extract_video_id(url_or_id: str) -> str | None:
    url_or_id = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_id):
        return url_or_id
    match = YT_ID_RE.search(url_or_id)
    return match.group(1) if match else None


def prompt(text: str) -> str:
    try:
        return input(text)
    except (EOFError, KeyboardInterrupt):
        print()
        return ""


# ---------------------------------------------------------------------------
# Tagging
# ---------------------------------------------------------------------------

def tag_file(path: Path, artist_name: str) -> bool:
    """
    Force a single audio file's Album and Album/Performer (album-artist)
    tags to match `artist_name`, leaving every other tag (title, cover art,
    track performer, etc.) untouched. Returns True if the file was written.
    """
    import mutagen

    try:
        audio = mutagen.File(str(path), easy=True)
    except Exception as exc:
        print(f"    [!] Could not open '{path.name}' for tagging: {exc}")
        return False

    if audio is None:
        # Not a format mutagen recognizes (e.g. a stray .webm without audio
        # tags support) -- just skip it quietly.
        return False

    if audio.tags is None:
        try:
            audio.add_tags()
        except Exception:
            pass

    changed = False
    for key in ("album", "albumartist"):
        current = audio.get(key)
        if current != [artist_name]:
            audio[key] = artist_name
            changed = True

    if changed:
        try:
            audio.save()
        except Exception as exc:
            print(f"    [!] Could not save tags for '{path.name}': {exc}")
            return False

    return changed


def tag_artist_folder(store: Store, artist: dict) -> None:
    """Re-tag every audio file in an artist's folder with the correct
    Album values. Safe to run repeatedly (idempotent)."""
    if not check_mutagen_available():
        return

    folder_path = store.base_dir / artist["folder"]
    if not folder_path.is_dir():
        return

    tagged = 0
    for f in sorted(folder_path.iterdir()):
        if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS:
            if tag_file(f, artist["name"]):
                tagged += 1

    if tagged:
        print(f"Tagged {tagged} file(s) as '{artist['name']}'.")


def retag_all(store: Store):
    """Menu action: re-tag every already-downloaded artist folder."""
    artists = store.load_artists()
    if not artists:
        print("No artists configured yet.")
        return
    if not check_mutagen_available():
        return
    print("\n--- Re-tagging all artist folders ---")
    for artist in artists:
        tag_artist_folder(store, artist)
    print("\nDone.")


# ---------------------------------------------------------------------------
# Core actions
# ---------------------------------------------------------------------------

def add_artist(store: Store):
    print("\n--- Add a new artist ---")
    name = prompt("Artist name: ").strip()
    if not name:
        print("Cancelled: name cannot be empty.")
        return

    url = prompt("YouTube / YT Music channel URL: ").strip()
    if not url:
        print("Cancelled: URL cannot be empty.")
        return

    folder = sanitize_folder_name(name)

    artists = store.load_artists()
    if any(a["url"] == url for a in artists):
        print("This artist URL is already in your list.")
        return

    artists.append({"name": name, "url": url, "folder": folder})
    store.save_artists(artists)

    (store.base_dir / folder).mkdir(parents=True, exist_ok=True)
    print(f"Added '{name}' -> {url}")

    if prompt("Download now? [y/N]: ").strip().lower() == "y":
        download_artist(store, artists[-1])


def list_artists(store: Store):
    artists = store.load_artists()
    print("\n--- Artists ---")
    if not artists:
        print("(none yet)")
        return
    for i, a in enumerate(artists, 1):
        print(f"{i}. {a['name']}  [{a['url']}]")


def remove_artist(store: Store):
    artists = store.load_artists()
    if not artists:
        print("No artists to remove.")
        return
    list_artists(store)
    choice = prompt("\nNumber to remove (blank to cancel): ").strip()
    if not choice:
        return
    try:
        idx = int(choice) - 1
        removed = artists.pop(idx)
    except (ValueError, IndexError):
        print("Invalid selection.")
        return
    store.save_artists(artists)
    print(f"Removed '{removed['name']}' from the list (files on disk are kept).")


def build_command(store: Store, artist: dict) -> list:
    folder_path = store.base_dir / artist["folder"]
    folder_path.mkdir(parents=True, exist_ok=True)
    output_template = str(folder_path / "%(title)s [%(id)s].%(ext)s")

    return [
        "yt-dlp",
        "--cookies-from-browser", COOKIES_BROWSER,
        "-x",
        "-f", "bestaudio",
        "--js-runtimes", "node",
        "--remote-components", "ejs:github",
        "--match-filter", "original_url!*=/shorts/",
        "--download-archive", str(store.archive_path),
        "-o", output_template,
        artist["url"],
    ]


def download_artist(store: Store, artist: dict):
    if not check_yt_dlp_available():
        return
    cmd = build_command(store, artist)

    print(f"\n>>> Downloading: {artist['name']}")
    print(" ".join(cmd), "\n")
    try:
        subprocess.run(cmd, check=False)
    except FileNotFoundError:
        print("[!] Could not run yt-dlp. Is it installed and on PATH?")
        return

    # Whether or not new tracks were actually fetched, make sure everything
    # in the folder carries the right Album tags.
    tag_artist_folder(store, artist)


def download_all(store: Store):
    artists = store.load_artists()
    if not artists:
        print("No artists configured yet. Add one first.")
        return
    if not check_yt_dlp_available():
        return
    for artist in artists:
        download_artist(store, artist)
    print("\nAll artists processed.")


def manage_ignore_list(store: Store):
    while True:
        print("\n--- Ignore list (skips these videos on future downloads) ---")
        print("1. View manually-ignored videos")
        print("2. Add a video to the ignore list")
        print("3. Back")
        choice = prompt("> ").strip()

        if choice == "1":
            lines = store.manual_ignore_path.read_text(encoding="utf-8").splitlines()
            if not lines:
                print("(no manually ignored videos)")
            for line in lines:
                print(" -", line)

        elif choice == "2":
            raw = prompt("Video URL or ID to ignore: ").strip()
            vid = extract_video_id(raw)
            if not vid:
                print("Could not parse a video ID from that input.")
                continue
            archive_entry = f"youtube {vid}"
            existing = store.archive_path.read_text(encoding="utf-8").splitlines()
            if archive_entry in existing:
                print("Already in the ignore list.")
                continue
            with store.archive_path.open("a", encoding="utf-8") as f:
                f.write(archive_entry + "\n")
            with store.manual_ignore_path.open("a", encoding="utf-8") as f:
                f.write(f"{vid}  ({raw})\n")
            print(f"Video {vid} will now be skipped by yt-dlp.")

        elif choice == "3":
            return
        else:
            print("Invalid choice.")


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

MENU = """
==========================================
 yt-dlp Music Manager
 Folder: {base}
==========================================
 1. Download / update all artists
 2. Add a new artist
 3. List artists
 4. Remove an artist
 5. Manage ignore list
 6. Re-tag all downloaded files (Album)
 7. Exit
==========================================
"""


def main():
    base_dir_arg = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("YTMUSIC_DIR", DEFAULT_BASE_DIR)
    base_dir = Path(base_dir_arg).expanduser()
    store = Store(base_dir)

    while True:
        print(MENU.format(base=store.base_dir))
        choice = prompt("Choose an option: ").strip()

        if choice == "1":
            download_all(store)
        elif choice == "2":
            add_artist(store)
        elif choice == "3":
            list_artists(store)
        elif choice == "4":
            remove_artist(store)
        elif choice == "5":
            manage_ignore_list(store)
        elif choice == "6":
            retag_all(store)
        elif choice == "7":
            print("Bye.")
            break
        else:
            print("Invalid choice, try again.")


if __name__ == "__main__":
    main()
