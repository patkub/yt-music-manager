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
If you've set a cover image for the artist, that image is embedded into
every audio file's artwork tag as well.

Run it, point it at your music folder, and use the menu.
"""

import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Default working folder. Override by passing a path as the first CLI arg,
# or by setting the YTMUSIC_DIR environment variable.
DEFAULT_BASE_DIR = "/home/user/Music/YTMusic/"

ARTISTS_FILENAME = "artists.json"
ARCHIVE_FILENAME = "archive.txt"          # yt-dlp --download-archive (the real ignore list)
MANUAL_IGNORE_FILENAME = "manual_ignore.txt"  # human-readable log of manually blocked videos
COVER_BASENAME = "cover"                  # cover.<ext> lives inside each artist folder

# Extensions we'll attempt to tag after a download. yt-dlp is told to always
# transcode to opus (see build_command), so this is what we expect to find.
AUDIO_EXTENSIONS = {".opus"}

# Formats we know how to embed cover art into, and how.
COVER_CAPABLE_EXTENSIONS = {".mp3", ".m4a", ".flac", ".ogg", ".opus"}

# Reasonable cover image types to accept.
ALLOWED_COVER_MIME = {"image/jpeg", "image/png"}

COOKIES_BROWSER = "chrome"

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
        print("    tags and cover art can't be fixed up after download.")
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


def find_artist_by_index(store: Store, label: str) -> dict | None:
    """Shared 'list artists, ask for a number' flow used by several menus."""
    artists = store.load_artists()
    if not artists:
        print("No artists configured yet.")
        return None
    list_artists(store)
    choice = prompt(f"\nNumber to {label} (blank to cancel): ").strip()
    if not choice:
        return None
    try:
        idx = int(choice) - 1
        return artists[idx]
    except (ValueError, IndexError):
        print("Invalid selection.")
        return None


# ---------------------------------------------------------------------------
# Cover images
# ---------------------------------------------------------------------------

def _guess_image_ext(mime: str, fallback_path: str = "") -> str:
    ext = mimetypes.guess_extension(mime or "") or ""
    if ext == ".jpe":
        ext = ".jpg"
    if not ext and fallback_path:
        ext = Path(urlparse(fallback_path).path).suffix
    return ext or ".jpg"


def _find_existing_cover(folder_path: Path) -> Path | None:
    for ext in (".jpg", ".jpeg", ".png"):
        candidate = folder_path / f"{COVER_BASENAME}{ext}"
        if candidate.exists():
            return candidate
    return None


def _clear_existing_covers(folder_path: Path):
    for ext in (".jpg", ".jpeg", ".png"):
        candidate = folder_path / f"{COVER_BASENAME}{ext}"
        if candidate.exists():
            candidate.unlink()


def fetch_cover_bytes(source: str) -> tuple[bytes, str] | None:
    """Return (bytes, mime_type) for a local path or http(s) URL, or None on failure."""
    parsed = urlparse(source)

    if parsed.scheme in ("http", "https"):
        try:
            req = urllib.request.Request(source, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
                mime = resp.headers.get_content_type() or "image/jpeg"
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
            print(f"[!] Could not download image: {exc}")
            return None
    else:
        path = Path(source).expanduser()
        if not path.is_file():
            print(f"[!] File not found: {path}")
            return None
        data = path.read_bytes()
        mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"

    if mime not in ALLOWED_COVER_MIME:
        # Best effort: sniff magic bytes rather than trust extension/header.
        if data[:3] == b"\xff\xd8\xff":
            mime = "image/jpeg"
        elif data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        else:
            print(f"[!] Unsupported image type ({mime}). Use a JPEG or PNG.")
            return None

    return data, mime


def set_cover_image(store: Store):
    """Menu action: attach/replace a cover image for one artist."""
    artist = find_artist_by_index(store, "set a cover for")
    if artist is None:
        return

    folder_path = store.base_dir / artist["folder"]
    folder_path.mkdir(parents=True, exist_ok=True)

    existing = _find_existing_cover(folder_path)
    if existing:
        print(f"Current cover: {existing.name}")

    source = prompt("Path to an image file, or an http(s) URL (blank to cancel): ").strip()
    if not source:
        return

    result = fetch_cover_bytes(source)
    if result is None:
        return
    data, mime = result

    ext = _guess_image_ext(mime, source)
    _clear_existing_covers(folder_path)
    cover_path = folder_path / f"{COVER_BASENAME}{ext}"
    cover_path.write_bytes(data)

    artists = store.load_artists()
    for a in artists:
        if a["url"] == artist["url"]:
            a["cover"] = cover_path.name
    store.save_artists(artists)

    print(f"Saved cover image -> {cover_path}")

    if prompt("Apply this cover to all existing downloaded files now? [y/N]: ").strip().lower() == "y":
        if not check_mutagen_available():
            return
        tag_artist_folder(store, artist)


def remove_cover_image(store: Store):
    """Menu action: remove a previously-set cover image for one artist."""
    artist = find_artist_by_index(store, "remove the cover for")
    if artist is None:
        return

    folder_path = store.base_dir / artist["folder"]
    if not artist.get("cover") and _find_existing_cover(folder_path) is None:
        print("This artist has no cover image set.")
        return

    _clear_existing_covers(folder_path)

    artists = store.load_artists()
    for a in artists:
        if a["url"] == artist["url"]:
            a.pop("cover", None)
    store.save_artists(artists)
    print(f"Removed cover image for '{artist['name']}'. (Existing embedded art in files is left as-is.)")


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


def embed_cover_in_file(path: Path, cover_data: bytes, cover_mime: str) -> bool:
    """
    Embed `cover_data` as the front-cover artwork of a single audio file.
    Uses format-specific mutagen APIs since the 'easy' interface doesn't
    expose artwork. Returns True if the file was written.
    """
    ext = path.suffix.lower()
    if ext not in COVER_CAPABLE_EXTENSIONS:
        return False

    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError, APIC

            try:
                tags = ID3(str(path))
            except ID3NoHeaderError:
                tags = ID3()
            tags.delall("APIC")
            tags.add(APIC(
                encoding=3,
                mime=cover_mime,
                type=3,  # front cover
                desc="Cover",
                data=cover_data,
            ))
            tags.save(str(path))
            return True

        elif ext == ".m4a":
            from mutagen.mp4 import MP4, MP4Cover

            fmt = MP4Cover.FORMAT_PNG if cover_mime == "image/png" else MP4Cover.FORMAT_JPEG
            tags = MP4(str(path))
            tags["covr"] = [MP4Cover(cover_data, imageformat=fmt)]
            tags.save()
            return True

        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture

            audio = FLAC(str(path))
            pic = Picture()
            pic.data = cover_data
            pic.type = 3
            pic.mime = cover_mime
            audio.clear_pictures()
            audio.add_picture(pic)
            audio.save()
            return True

        elif ext in (".ogg", ".opus"):
            import base64
            from mutagen.flac import Picture

            if ext == ".opus":
                from mutagen.oggopus import OggOpus as OggFileClass
            else:
                from mutagen.oggvorbis import OggVorbis as OggFileClass

            pic = Picture()
            pic.data = cover_data
            pic.type = 3
            pic.mime = cover_mime
            pic_data = base64.b64encode(pic.write()).decode("ascii")

            audio = OggFileClass(str(path))
            audio["metadata_block_picture"] = [pic_data]
            audio.save()
            return True

    except Exception as exc:
        print(f"    [!] Could not embed cover art in '{path.name}': {exc}")
        return False

    return False


def tag_artist_folder(store: Store, artist: dict) -> None:
    """Re-tag every audio file in an artist's folder with the correct
    Album values, and embed the artist's cover image if one is set.
    Safe to run repeatedly (idempotent)."""
    if not check_mutagen_available():
        return

    folder_path = store.base_dir / artist["folder"]
    if not folder_path.is_dir():
        return

    cover_bundle = None
    cover_name = artist.get("cover")
    cover_path = (folder_path / cover_name) if cover_name else _find_existing_cover(folder_path)
    if cover_path and cover_path.is_file():
        mime = mimetypes.guess_type(str(cover_path))[0] or "image/jpeg"
        cover_bundle = (cover_path.read_bytes(), mime)

    tagged = 0
    covered = 0
    for f in sorted(folder_path.iterdir()):
        if not (f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS):
            continue
        if tag_file(f, artist["name"]):
            tagged += 1
        if cover_bundle is not None:
            if embed_cover_in_file(f, cover_bundle[0], cover_bundle[1]):
                covered += 1

    if tagged:
        print(f"Tagged {tagged} file(s) as '{artist['name']}'.")
    if covered:
        print(f"Embedded cover art in {covered} file(s).")
    elif cover_bundle is not None:
        skipped_exts = {f.suffix.lower() for f in folder_path.iterdir()
                         if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
                         and f.suffix.lower() not in COVER_CAPABLE_EXTENSIONS}
        if skipped_exts:
            print(f"Note: cover art isn't supported for: {', '.join(sorted(skipped_exts))}")


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

    if prompt("Set a cover image now? [y/N]: ").strip().lower() == "y":
        set_cover_image_for(store, artists[-1])

    if prompt("Download now? [y/N]: ").strip().lower() == "y":
        # Reload in case a cover was just attached.
        artists = store.load_artists()
        current = next(a for a in artists if a["url"] == url)
        download_artist(store, current)


def set_cover_image_for(store: Store, artist: dict):
    """Same flow as set_cover_image() but for an artist we already have,
    without re-prompting for which artist to pick."""
    folder_path = store.base_dir / artist["folder"]
    folder_path.mkdir(parents=True, exist_ok=True)

    source = prompt("Path to an image file, or an http(s) URL (blank to skip): ").strip()
    if not source:
        return

    result = fetch_cover_bytes(source)
    if result is None:
        return
    data, mime = result

    ext = _guess_image_ext(mime, source)
    _clear_existing_covers(folder_path)
    cover_path = folder_path / f"{COVER_BASENAME}{ext}"
    cover_path.write_bytes(data)

    artists = store.load_artists()
    for a in artists:
        if a["url"] == artist["url"]:
            a["cover"] = cover_path.name
    store.save_artists(artists)
    print(f"Saved cover image -> {cover_path}")


def list_artists(store: Store):
    artists = store.load_artists()
    print("\n--- Artists ---")
    if not artists:
        print("(none yet)")
        return
    for i, a in enumerate(artists, 1):
        cover_flag = " [cover set]" if a.get("cover") else ""
        print(f"{i}. {a['name']}  [{a['url']}]{cover_flag}")


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
        "--audio-format", "opus",
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
    # in the folder carries the right Album tags and cover art.
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
 6. Re-tag all downloaded files (Album + cover art)
 7. Set/update cover image for an artist
 8. Remove cover image for an artist
 9. Exit
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
            set_cover_image(store)
        elif choice == "8":
            remove_cover_image(store)
        elif choice == "9":
            print("Bye.")
            break
        else:
            print("Invalid choice, try again.")


if __name__ == "__main__":
    main()
