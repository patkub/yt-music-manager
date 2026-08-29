# yt-music-manager
A little wrapper around yt-dlp

## Prerequisites

### yt-dlp
```
pipx install yt-dlp
pipx inject yt-dlp yt-dlp-getpot-wpc
```
Reference
- https://github.com/yt-dlp/yt-dlp
- https://github.com/coletdjnz/yt-dlp-getpot-wpc


## Install
```
cd ./src/
pipx install . --force
```
Add music path to `.bashrc`
```
export YTMUSIC_DIR="/home/user/Music/YTMusic/"
```

## Usage
```
$ yt-music 

==========================================
 yt-dlp Music Manager
 Folder: /home/user/Music/YTMusic
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

Choose an option:
```
