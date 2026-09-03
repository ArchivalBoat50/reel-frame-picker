# Reel Frame Picker

## What it is and why it exists

Producing one short video with an external image/video generation service used to mean the same
half-dozen manual steps every time: grab the source clip off the web, open it in a player, scrub
back and forth looking for a frame where the subject is well lit and facing the right way, screenshot
or export that frame, upload it to the generation tool, download whatever comes back, then remember
which output belonged to which source clip. The scrubbing and the filing were the parts that hurt —
frames ended up in `~/Downloads` as `Screen Shot 2026-04-09 at 14.30.22.png`, and by the third or
fourth clip in a session there was no reliable way to tell which download belonged to which project.
This tool collapses that into one local page: paste a URL, it fetches the video, cuts eight evenly
spaced candidate frames, shows them as a grid so a frame is chosen by eye in one click instead of by
scrubbing, and creates a dated per-project folder that the intermediate and final files get dropped
back into. It does not do the generation itself — the generation still happens in Higgsfield, Kling,
or whatever tool is in use — it removes the fetching, the scrubbing, the exporting, and the filing
around it.

## Architecture

Three pieces: a single-file Flask backend (`app.py`), a single-file browser UI
(`static/index.html`, no build step and no framework), and the filesystem as the database. There is
no SQLite, no ORM, and no in-memory state — every request reads the project directory fresh, and
`meta.json` on disk is the only record of where a project is in the pipeline.

```
  Browser UI  (static/index.html — vanilla JS, fetch(), no build step)
      |
      |  JSON for actions, multipart/form-data for uploads
      v
  Flask  (app.py, bound to 127.0.0.1:5050, Flask dev server)
      |
      +-- POST /api/projects ............ mkdir ~/ReelPipeline/projects/<ts>_<shortcode>/
      |                                   + source/ frames/ swapped/ final/ + meta.json
      |
      +-- POST .../download ............. [1] requests -> instagram.com GraphQL / embed page /
      |                                       og:video meta tag  -> CDN url -> stream to
      |                                       source/raw.mp4
      |                                   [2] fallback: python -m yt_dlp <url> -> source/raw.mp4
      |                                   then ffmpeg -c copy -movflags +faststart
      |                                       -> source/reel.mp4
      |
      +-- POST .../extract-frames ....... ffprobe -> duration
      |                                   ffmpeg x N (one process per frame)
      |                                       -> frames/frame_001.jpg ... frame_008.jpg
      |
      +-- POST .../select-frame ......... copy chosen frame -> frames/SELECTED_reference.jpg,
      |                                   delete the other candidates
      |
      +-- POST .../open-frame ........... subprocess ["open", <path>]  (macOS)
      +-- POST .../open-folder .......... subprocess ["open", <dir>]   (macOS)
      |
      +-- POST .../upload/<stage> ....... secure_filename() -> swapped/ or final/
      |
      +-- GET  .../files ................ meta.json + a listing of all four directories
      |
      \-- every mutating route rewrites meta.json with a new "status"

  Disk  ~/ReelPipeline/projects/20260409-143022_C1a2B3c4D5e/
        |-- source/     reel.mp4              (remuxed download)
        |-- frames/     frame_003.jpg, SELECTED_reference.jpg
        |-- swapped/    output from the image generation step
        |-- final/      output from the video generation step
        \-- meta.json   name, url, shortcode, created, status, method,
                        video_file, frame_count, selected_frame, error
```

### Why a directory per project with a manifest

The alternative — everything in one flat output folder, distinguished by filename — breaks as soon
as more than one clip is in flight. The four-stage directory encodes the pipeline in the filesystem
itself, so the stage a file belongs to is a fact about where it lives rather than a convention in its
name, and "open the folder for this project" is a single unambiguous action. `meta.json` carries the
part the filenames cannot: the original URL, the creation timestamp, which frame was chosen, the
error text from a failed download, and a `status` string that the UI turns into the step indicator
and the coloured dot in the sidebar. Because the manifest lives beside the files, a project stays
self-describing if it is copied, archived, or opened months later, and the listing endpoint can
rebuild the entire UI state by walking the directory — nothing is held in server memory, so
restarting the app loses nothing.

## Key design decisions

**A local web UI rather than a CLI.** The central operation is a visual judgement — "which of these
eight frames is the usable one" — and a terminal cannot show eight images side by side. A browser can
render the grid, accept a drag-and-drop of the generated file coming back from the external tool, and
show per-project status at a glance. Flask was the cheapest way to get that: one process, one file,
no packaging, no Electron. [CONFIRM] that avoiding a desktop-app toolchain was the reason rather than
simply familiarity with Flask.

**Shelling out to `ffmpeg`/`ffprobe` rather than using Python bindings.** The code calls both through
`subprocess.run` with argument lists. The bindings (`ffmpeg-python`, PyAV) add a compiled dependency
and a version-coupling problem for operations that are one command line each; the exact flags used
here (`-ss` seek, `-vframes 1`, `-q:v 2`, `-c copy -movflags +faststart`) are the same flags one would
test by hand in a terminal, which makes them debuggable by copying them out of the source. [CONFIRM]
this reasoning — it is consistent with the code but not documented in it.

**yt-dlp is a fallback, not the primary path.** The download route first tries direct extraction:
it hits `instagram.com`, pulls an LSD token out of the HTML, calls the GraphQL endpoint with a
hard-coded `doc_id`, and falls back through the embed pages and the `og:video` meta tag looking for a
`video_url`. Only if all of that raises does it try yt-dlp, and it invokes it as
`sys.executable -m yt_dlp` after checking the module is importable — so it uses the module in the
current interpreter, not a `yt-dlp` binary on `PATH`. The direct path is faster and has no external
dependency; the yt-dlp path is the one that keeps working when Instagram changes its markup.
[CONFIRM] that this ordering was chosen for speed/dependency reasons rather than being the historical
order in which the two were written.

**Whatever the source, the file is remuxed.** Both download paths finish with
`ffmpeg -c copy -movflags +faststart`. That is a stream copy, not a re-encode, so it costs almost
nothing and there is no quality loss; it normalises the container and moves the moov atom to the
front. If the remux produces nothing, the raw file is renamed into place instead — the download is
never lost to a remux failure.

**Eight evenly spaced frames, by default.** `mode: "smart"` computes
`interval = duration / (count + 1)` and takes one frame at each of `interval * 1 .. interval * count`,
which spreads the samples across the clip without ever landing on the first or last frame — the two
places most likely to be a fade, a black frame, or a title card. Eight is enough to cover the distinct
shots in a short clip while still fitting the grid on one screen without scrolling. [CONFIRM] the
specific reasoning for 8; the value is the default in both `app.py` and the UI's `extractFrames()`
call, but nothing in the code explains it. Two other modes exist in the backend — `first` (one frame)
and anything else (`fps=1`, one frame per second) — but the UI only ever sends `smart`.

**One ffmpeg process per frame, not one pass.** In `smart` mode the code loops and spawns a separate
`ffmpeg` per frame, each with `-ss` *before* `-i` so ffmpeg fast-seeks to the keyframe region instead
of decoding from the start. For a short clip this is fast enough and it keeps each frame's success or
failure independent. [CONFIRM] that this was a deliberate trade rather than the simplest thing that
worked; a single `select`-filter pass would produce the same frames in one process.

**Project IDs are `YYYYMMDD-HHMMSS_<shortcode>`.** Two facts, no prompt. The timestamp makes the
directory name sort chronologically in any file browser and guarantees uniqueness without a counter or
a UUID; the source shortcode makes the folder traceable back to the clip it came from without opening
`meta.json`. The user is never asked to name anything at creation time — naming a project before you
have seen the frames is a decision you cannot make well — and a `rename` endpoint exists to set a
display name in `meta.json` later, which changes the label in the UI but never the directory name, so
existing paths stay valid. A `slug()` helper for sanitising free text into a filesystem-safe string is
defined but is not called anywhere in the current code.

**Selecting a frame deletes the others.** `select-frame` copies the chosen frame to
`SELECTED_reference.jpg` and then unlinks every other file in `frames/`. The upside is that the
project folder afterwards contains exactly the reference image and nothing to confuse it with; the
cost is that the choice is irreversible without re-extracting. It is recoverable — the source video is
still in `source/` and extraction can simply be run again — but it is a destructive default and worth
calling out.

**Uploads.** `POST /api/projects/<id>/upload/<stage>` checks `stage` against an explicit allowlist of
`swapped`, `final`, `source` and rejects anything else, then passes the client-supplied filename
through Werkzeug's `secure_filename()` before joining it to the target directory — so `../` segments,
absolute paths, and NUL bytes in the filename are stripped. `MAX_CONTENT_LENGTH` is set to 200 MB, so
oversized bodies are refused by Flask before they reach the handler. There is no check on file type or
content: the extension list shown in the drop zone ("PNG, JPG, WEBP") is UI text only, and the server
will store any file it is given. There is also no guard for the case where `secure_filename()` reduces
a name to the empty string, which would make the save target the directory itself.

**URL handling.** `extract_shortcode()` applies
`re.search(r'instagram\.com/(?:reel|p|reels)/([A-Za-z0-9_-]+)', url)` and project creation is rejected
with a 400 if it does not match. The captured shortcode is restricted to a safe character class and is
what the direct-download path uses. Every `subprocess.run` in the codebase passes an argument list and
never `shell=True`, so there is no shell-injection surface. The regex is an unanchored `search`, so a string that merely
*contains* an Instagram permalink passes the check. Because of that, neither download path is given
the caller's string: the direct path uses the captured shortcode, and the yt-dlp fallback rebuilds a
canonical `https://www.instagram.com/reel/<shortcode>/` from it and passes `--` before the URL
argument. Only `[A-Za-z0-9_-]` ever reaches a subprocess, which closes the argument-injection route
into yt-dlp's very large option surface.

**Path handling for project IDs — one chokepoint.** `project_id` arrives as a URL path segment and is
untrusted. Every route that touches the filesystem goes through `project_path()`, which resolves both
the root and the joined path and requires the result to be a strict child of `PROJECTS_DIR`. That one
check is what stops `..`, an absolute path, or a symlink from walking out of the projects tree — and
requiring a *strict* child, rather than merely "not outside", is what stops an id that resolves back
to `PROJECTS_DIR` itself from letting a delete take the whole tree. The containment check lives in
that function and only in that function, so a new route cannot forget it. The file-*serving* routes
additionally get `send_from_directory()`'s own check on the filename half. `open-folder` validates its
caller-supplied `subfolder` against `STAGE_DIRS`, the fixed set of four stage directory names, rather
than joining request-body input into a path handed to `open`.

## Measured results

None. This is a personal workflow tool built for one person's own use; there are no benchmarks, no
timing measurements, no before/after comparison, and no usage statistics. Nothing in the code
instruments itself, and no measurement was ever taken. Any number in this section would be invented,
so there are none.

## Stack

- Python 3, Flask (dev server, `debug=False`), Werkzeug (`secure_filename`, `send_from_directory`)
- `requests` for the direct Instagram extraction path
- `yt-dlp`, invoked as a module in the current interpreter, as the download fallback
- `ffmpeg` and `ffprobe`, invoked as subprocesses, for probing, remuxing and frame extraction
- Vanilla HTML/CSS/JavaScript — one static file, `fetch()`, no framework, no bundler, no npm
- The filesystem (`~/ReelPipeline/projects/`) plus a per-project `meta.json` as the datastore
- macOS-specific: `open` for revealing files and folders; `launch.sh` uses Homebrew to install ffmpeg

## Running it locally

Requirements: Python 3, `ffmpeg` and `ffprobe` on `PATH`, and macOS if you want the "Open in Finder"
and "Open in Preview" buttons to do anything.

```bash
cd reel-frame-picker
chmod +x launch.sh
./launch.sh            # default port 5050
./launch.sh 5100       # or pass a port
```

`launch.sh` verifies Python 3, installs `ffmpeg` via Homebrew if it is missing, `pip3 install`s Flask
and `requests` if they are missing, then starts the app. Note that it does **not** install `yt-dlp` —
if you want the fallback download path, install it yourself into the same interpreter:

```bash
pip3 install yt-dlp
```

The app opens `http://localhost:5050` in your browser automatically and prints the projects directory
(`~/ReelPipeline/projects`) on startup. To run it without the launcher:

```bash
pip3 install flask requests yt-dlp
python3 app.py 5050
```

Workflow in the UI: **New Project** and paste a URL (the download starts automatically) → **Extract**
→ click a frame in the grid → **Open in Preview** and drag it into your generation tool → drop the
returned image onto the "Swapped Image" zone → drop the returned video onto the "Final Video" zone.
Each project's folder is one click away via "Open in Finder".

## Known limitations and what I would do next

**Security posture.** This binds to `127.0.0.1` only, which is the main thing keeping it safe, and it
is the assumption everything else leans on. Concretely:

- *No authentication, no CSRF protection.* Any process on the machine can drive the full API, and any
  web page open in the same browser can issue cross-origin requests to `localhost:5050`. The JSON
  endpoints are incidentally protected because an `application/json` body triggers a CORS preflight
  the server does not answer, but the upload endpoint takes `multipart/form-data`, which is a simple
  request and is *not* preflighted — so a hostile page that knew or guessed a project ID could write a
  file into that project's directory. [CONFIRM] the exact exploitability in current browsers; the
  shape of the hole is clear from the code. Fix: a random token minted at startup, put into the served
  HTML and required on every mutating request, plus an `Origin` check.
- *No upload content validation.* Any file type, any content, up to 200 MB. Fix: check the extension
  and sniff the magic bytes against the stage's expected type.
- *File listings return absolute host paths* (`list_files_in` includes `"path": str(f)`) to the
  browser. Harmless locally, wrong the moment this is not local.

**Operational limitations.**

- *Shelling out to subprocesses.* Every media operation is a child process, which means the app's
  correctness depends on the version of ffmpeg on the machine, and failures surface as an empty output
  directory rather than a clean exception. `ffmpeg` and `ffprobe` are assumed present on `PATH` with no
  check at import time — `launch.sh` checks for ffmpeg but the app itself does not, so running
  `python3 app.py` directly on a machine without it fails at frame-extraction time. yt-dlp is handled
  better: its absence is detected up front via an import check and degrades to a clear message.
- *Blocking, single-user, no job queue.* Download and extraction run inside the request. A slow
  download holds the request open for up to 60 s of transfer plus a 120 s yt-dlp timeout, and the UI
  has nothing but a toast to show for it. Worse, the frame-extraction `ffmpeg` and `ffprobe` calls
  have **no** `timeout=` argument at all (unlike the remux and yt-dlp calls, which do) — a hung ffmpeg
  hangs that request indefinitely.
- *`status` is written but never reconciled.* A project is stamped `"downloading"` at creation. If the
  server is killed mid-download, it stays `"downloading"` forever with no way to reset it from the UI.
- *Silent failure in duration probing.* The `ffprobe` result is parsed inside a bare `except:` that
  falls back to a hard-coded `duration = 10.0`. On a clip whose duration cannot be read, frames are
  extracted at the wrong timestamps and the user is told nothing.
- *Instagram-specific and scraper-fragile.* `extract_shortcode` only understands Instagram URLs, and
  the direct path depends on a hard-coded GraphQL `doc_id`, a spoofed `User-Agent`, and regexes against
  markup that is not a contract. All of that will break without notice; yt-dlp is the reason the tool
  survives it.
- *macOS-only convenience layer.* `open` is not a command on Linux or Windows, so "Open in Finder" and
  "Open in Preview" return 500-ish behaviour elsewhere. `launch.sh` assumes Homebrew.
- *Destructive frame selection.* Choosing a frame deletes the other seven, as described above.
- *No tests.* There is no test file in the project and none of the pure functions
  (`extract_shortcode`, `slug`, `_unescape_url`) are covered, despite being the easiest things in the
  codebase to test.

**What I would do next, in order.** (1) Add the CSRF token and `Origin` check described above — with
containment and argument injection closed, that is the largest remaining hole. (2) Add
`timeout=` to the frame-extraction subprocess calls and check the return codes instead of discarding
them. (3) Move download and extraction onto a background worker with a polled progress endpoint, so
the UI shows real progress and a stuck job can be cancelled. (4) Replace the bare `except:` around
`ffprobe` with a real error surfaced to the user. (5) Add tests for the pure functions and a fixture
video for the ffmpeg path. (6) *Then*, if it needed to be multi-user: a real WSGI server, sessions,
per-user roots under a single storage base with enforced containment, a shared job queue, and
object storage instead of the local filesystem — but that is a different program, and for a
single-machine tool the current design is the right size.
