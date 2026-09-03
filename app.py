#!/usr/bin/env python3
"""
Reel Pipeline — Local workflow tool for AI content generation.
Downloads reels, extracts frames, organizes project files.
"""

import os
import sys
import json
import subprocess
import shutil
import webbrowser
import glob
import re
import time
import hashlib
from datetime import datetime
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory, send_file
from werkzeug.utils import secure_filename
import requests as http_requests

# ── Config ──────────────────────────────────────────────────────────────────
BASE_DIR = Path.home() / "ReelPipeline"
PROJECTS_DIR = BASE_DIR / "projects"
PROJECTS_DIR.mkdir(parents=True, exist_ok=True)

# The four per-project stage directories. Also the allowlist for any request
# that names a subfolder, so a path never comes straight from a request body.
STAGE_DIRS = ("source", "frames", "swapped", "final")

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024  # 200MB

# ── Helpers ─────────────────────────────────────────────────────────────────

def slug(text, maxlen=40):
    """Create a filesystem-safe slug from text."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return s[:maxlen] if s else "untitled"


def project_path(project_id):
    """Resolve a project id to a directory, refusing anything outside PROJECTS_DIR.

    project_id arrives straight off the URL, so it is untrusted. Resolving both
    sides and checking containment is what stops "..", an absolute path, or a
    symlink from walking out of the projects tree -- every route below reaches
    the filesystem through this function, so the check belongs here and only
    here.
    """
    root = PROJECTS_DIR.resolve()
    p = (root / project_id).resolve()
    # must be a child of the root, never the root itself -- otherwise an id
    # that resolves back to PROJECTS_DIR would let a delete take the whole tree
    if root not in p.parents:
        raise FileNotFoundError(f"Project {project_id} not found")
    if not p.exists():
        raise FileNotFoundError(f"Project {project_id} not found")
    return p


INSTAGRAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def extract_shortcode(url):
    """Extract the shortcode from an Instagram reel/post URL."""
    m = re.search(r'instagram\.com/(?:reel|p|reels)/([A-Za-z0-9_-]+)', url)
    return m.group(1) if m else None


def _unescape_url(u):
    """Unescape Instagram JSON-encoded URLs."""
    return u.replace('\\u0026', '&').replace('\\/', '/')


def fetch_video_url(shortcode):
    """Try multiple methods to extract the video CDN URL from Instagram."""
    session = http_requests.Session()
    session.headers.update(INSTAGRAM_HEADERS)
    errors = []

    # Method 1: GraphQL API with web session
    try:
        resp = session.get("https://www.instagram.com/", timeout=15)
        csrf = session.cookies.get("csrftoken", "")
        lsd_m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', resp.text)
        lsd = lsd_m.group(1) if lsd_m else ""
        if lsd:
            gql_resp = session.post(
                "https://www.instagram.com/graphql/query/",
                headers={
                    "X-CSRFToken": csrf,
                    "X-FB-LSD": lsd,
                    "X-IG-App-ID": "936619743392459",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://www.instagram.com/",
                    "Origin": "https://www.instagram.com",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "variables": json.dumps({"shortcode": shortcode}),
                    "doc_id": "8845758582119845",
                    "lsd": lsd,
                },
                timeout=15,
            )
            if gql_resp.ok:
                gql = gql_resp.json()
                media = (gql.get("data") or {}).get("xdt_shortcode_media")
                if media and media.get("video_url"):
                    return _unescape_url(media["video_url"])
    except Exception as e:
        errors.append(f"graphql: {e}")

    # Method 2: Embed page (captioned variant has more data)
    for embed_path in [
        f"/reel/{shortcode}/embed/captioned/",
        f"/reel/{shortcode}/embed/",
        f"/p/{shortcode}/embed/captioned/",
    ]:
        try:
            resp = session.get(
                f"https://www.instagram.com{embed_path}", timeout=15
            )
            if resp.ok:
                matches = re.findall(r'"video_url":"([^"]+)"', resp.text)
                if matches:
                    return _unescape_url(matches[0])
        except Exception as e:
            errors.append(f"{embed_path}: {e}")

    # Method 3: Main page — og:video meta tag or inline JSON
    for page_path in [f"/reel/{shortcode}/", f"/p/{shortcode}/"]:
        try:
            resp = session.get(
                f"https://www.instagram.com{page_path}", timeout=15
            )
            if resp.ok:
                m = re.search(
                    r'<meta\s+property="og:video"\s+content="([^"]+)"',
                    resp.text,
                )
                if m:
                    return m.group(1).replace("&amp;", "&")
                matches = re.findall(r'"video_url":"([^"]+)"', resp.text)
                if matches:
                    return _unescape_url(matches[0])
        except Exception as e:
            errors.append(f"{page_path}: {e}")

    raise ValueError(
        f"Could not extract video URL via direct methods. "
        f"Tried: {'; '.join(errors) or 'all methods returned no video_url'}"
    )


def list_files_in(directory):
    """List files with metadata in a directory."""
    if not directory.exists():
        return []
    files = []
    for f in sorted(directory.iterdir()):
        if f.is_file() and not f.name.startswith("."):
            files.append({
                "name": f.name,
                "size": f.stat().st_size,
                "modified": f.stat().st_mtime,
                "path": str(f),
            })
    return files


# ── API Routes ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/static/<path:path>")
def serve_static(path):
    return send_from_directory("static", path)


@app.route("/api/projects", methods=["GET"])
def get_projects():
    """List all projects."""
    projects = []
    if PROJECTS_DIR.exists():
        for d in sorted(PROJECTS_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if d.is_dir() and not d.name.startswith("."):
                meta_file = d / "meta.json"
                meta = {}
                if meta_file.exists():
                    meta = json.loads(meta_file.read_text())
                projects.append({
                    "id": d.name,
                    "name": meta.get("name", d.name),
                    "url": meta.get("url", ""),
                    "created": meta.get("created", ""),
                    "status": meta.get("status", "new"),
                })
    return jsonify(projects)


@app.route("/api/projects", methods=["POST"])
def create_project():
    """Create a new project from a reel URL."""
    data = request.json
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400

    shortcode = extract_shortcode(url)
    if not shortcode:
        return jsonify({"error": "Could not parse Instagram URL. Expected /reel/... or /p/..."}), 400

    # Auto-generate name: {timestamp}_{shortcode}
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    project_name = f"{ts}_{shortcode}"
    project_id = project_name
    proj = PROJECTS_DIR / project_id

    # Create folder structure
    (proj / "source").mkdir(parents=True)
    (proj / "frames").mkdir(parents=True)
    (proj / "swapped").mkdir(parents=True)
    (proj / "final").mkdir(parents=True)

    meta = {
        "name": project_name,
        "url": url,
        "shortcode": shortcode,
        "created": datetime.now().isoformat(),
        "status": "downloading",
    }
    (proj / "meta.json").write_text(json.dumps(meta, indent=2))

    return jsonify({"id": project_id, "name": project_name, "status": "downloading"})


def _remux_video(raw_path, final_path):
    """Remux through FFmpeg for clean container metadata. Instant (copy, no re-encode)."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(raw_path), "-c", "copy",
         "-movflags", "+faststart", str(final_path)],
        capture_output=True, text=True, timeout=30,
    )
    if final_path.exists() and final_path.stat().st_size > 0:
        raw_path.unlink(missing_ok=True)
        return final_path
    # Remux failed — use raw file directly
    raw_path.rename(final_path)
    return final_path


def _download_direct(shortcode, source_dir):
    """Try direct Instagram extraction → download → remux. Returns video Path or raises."""
    video_cdn_url = fetch_video_url(shortcode)

    raw_path = source_dir / "raw.mp4"
    resp = http_requests.get(
        video_cdn_url, headers=INSTAGRAM_HEADERS, stream=True, timeout=60
    )
    resp.raise_for_status()
    with open(raw_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)

    return _remux_video(raw_path, source_dir / "reel.mp4")


def _download_ytdlp(shortcode, source_dir):
    """Fallback: use yt-dlp if installed. Returns video Path or raises.

    Takes the validated shortcode rather than the URL the caller was given.
    extract_shortcode() uses an unanchored search, so the original string only
    has to *contain* an Instagram permalink -- the rest of it is arbitrary, and
    handing that to a tool with yt-dlp's option surface is argument injection.
    Rebuilding the URL from the shortcode means only [A-Za-z0-9_-] ever reaches
    the subprocess.
    """
    url = f"https://www.instagram.com/reel/{shortcode}/"
    # Check if yt_dlp is importable before shelling out
    try:
        import importlib
        importlib.import_module("yt_dlp")
    except ImportError:
        raise FileNotFoundError("yt-dlp not installed")

    raw_path = source_dir / "raw.mp4"
    result = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "--no-playlist",
         "-o", str(raw_path),
         "--merge-output-format", "mp4",
         "--", url],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-500:] if result.stderr else "yt-dlp failed")

    # yt-dlp may pick a slightly different name
    if not raw_path.exists():
        candidates = [f for f in source_dir.iterdir()
                      if f.is_file() and f.suffix in (".mp4", ".webm", ".mkv")]
        if not candidates:
            raise RuntimeError("yt-dlp produced no video file")
        raw_path = candidates[0]

    return _remux_video(raw_path, source_dir / "reel.mp4")


@app.route("/api/projects/<project_id>/download", methods=["POST"])
def download_reel(project_id):
    """Download the reel — direct extraction first, yt-dlp fallback."""
    proj = project_path(project_id)
    meta = json.loads((proj / "meta.json").read_text())
    url = meta["url"]
    source_dir = proj / "source"

    shortcode = extract_shortcode(url)
    if not shortcode:
        meta["status"] = "download_failed"
        meta["error"] = "Could not extract shortcode from URL"
        (proj / "meta.json").write_text(json.dumps(meta, indent=2))
        return jsonify({"error": "Invalid Instagram URL"}), 400

    # --- Attempt 1: direct Instagram extraction ---
    try:
        video_file = _download_direct(shortcode, source_dir)
        meta["status"] = "downloaded"
        meta["video_file"] = video_file.name
        meta["method"] = "direct"
        (proj / "meta.json").write_text(json.dumps(meta, indent=2))
        return jsonify({
            "status": "downloaded",
            "file": video_file.name,
            "size": video_file.stat().st_size,
        })
    except Exception as direct_err:
        direct_msg = str(direct_err)

    # --- Attempt 2: yt-dlp fallback ---
    try:
        video_file = _download_ytdlp(shortcode, source_dir)
        meta["status"] = "downloaded"
        meta["video_file"] = video_file.name
        meta["method"] = "yt-dlp"
        (proj / "meta.json").write_text(json.dumps(meta, indent=2))
        return jsonify({
            "status": "downloaded",
            "file": video_file.name,
            "size": video_file.stat().st_size,
        })
    except FileNotFoundError:
        # yt-dlp not installed
        pass
    except Exception as yt_err:
        direct_msg += f" | yt-dlp: {yt_err}"

    meta["status"] = "download_failed"
    meta["error"] = direct_msg
    (proj / "meta.json").write_text(json.dumps(meta, indent=2))
    return jsonify({"error": direct_msg}), 500


@app.route("/api/projects/<project_id>/extract-frames", methods=["POST"])
def extract_frames(project_id):
    """Extract frames from the downloaded video using FFmpeg."""
    proj = project_path(project_id)
    meta = json.loads((proj / "meta.json").read_text())
    source_dir = proj / "source"
    frames_dir = proj / "frames"

    data = request.json or {}
    mode = data.get("mode", "smart")  # smart, all, first
    count = data.get("count", 8)

    # Find video file
    video_files = [f for f in source_dir.iterdir() if f.is_file() and f.suffix in ('.mp4', '.webm', '.mkv')]
    if not video_files:
        return jsonify({"error": "No video file found"}), 404
    video = video_files[0]

    # Get video duration
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(video)],
        capture_output=True, text=True
    )
    duration = 10.0
    try:
        duration = float(json.loads(probe.stdout)["format"]["duration"])
    except:
        pass

    # Clear old frames
    for f in frames_dir.glob("*.jpg"):
        f.unlink()

    if mode == "first":
        # Just the first frame
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video),
            "-vframes", "1", "-q:v", "2",
            str(frames_dir / "frame_001.jpg")
        ], capture_output=True)
    elif mode == "smart":
        # Extract N evenly spaced frames
        interval = max(duration / (count + 1), 0.1)
        for i in range(count):
            ts = interval * (i + 1)
            subprocess.run([
                "ffmpeg", "-y", "-ss", f"{ts:.2f}",
                "-i", str(video),
                "-vframes", "1", "-q:v", "2",
                str(frames_dir / f"frame_{i+1:03d}.jpg")
            ], capture_output=True)
    else:
        # Extract one frame per second
        subprocess.run([
            "ffmpeg", "-y", "-i", str(video),
            "-vf", f"fps=1", "-q:v", "2",
            str(frames_dir / "frame_%03d.jpg")
        ], capture_output=True)

    frames = list_files_in(frames_dir)
    meta["status"] = "frames_extracted"
    meta["frame_count"] = len(frames)
    (proj / "meta.json").write_text(json.dumps(meta, indent=2))

    return jsonify({"status": "frames_extracted", "frames": frames})


@app.route("/api/projects/<project_id>/frames", methods=["GET"])
def get_frames(project_id):
    """List extracted frames."""
    proj = project_path(project_id)
    frames = list_files_in(proj / "frames")
    return jsonify(frames)


@app.route("/api/projects/<project_id>/frame/<filename>")
def serve_frame(project_id, filename):
    """Serve a frame image."""
    proj = project_path(project_id)
    return send_from_directory(proj / "frames", filename)


@app.route("/api/projects/<project_id>/source/<filename>")
def serve_source(project_id, filename):
    """Serve a source video."""
    proj = project_path(project_id)
    return send_from_directory(proj / "source", filename)


@app.route("/api/projects/<project_id>/swapped/<filename>")
def serve_swapped(project_id, filename):
    """Serve a swapped image."""
    proj = project_path(project_id)
    return send_from_directory(proj / "swapped", filename)


@app.route("/api/projects/<project_id>/final/<filename>")
def serve_final(project_id, filename):
    """Serve a final file."""
    proj = project_path(project_id)
    return send_from_directory(proj / "final", filename)


@app.route("/api/projects/<project_id>/upload/<stage>", methods=["POST"])
def upload_file(project_id, stage):
    """Upload a file to a project stage (swapped, final)."""
    if stage not in ("swapped", "final", "source"):  # frames are generated, not uploaded
        return jsonify({"error": "Invalid stage"}), 400

    proj = project_path(project_id)
    target_dir = proj / stage
    meta = json.loads((proj / "meta.json").read_text())

    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400

    f = request.files["file"]
    filename = secure_filename(f.filename)
    filepath = target_dir / filename
    f.save(filepath)

    if stage == "swapped":
        meta["status"] = "swapped"
    elif stage == "final":
        meta["status"] = "complete"
    (proj / "meta.json").write_text(json.dumps(meta, indent=2))

    return jsonify({
        "status": meta["status"],
        "file": filename,
        "size": filepath.stat().st_size,
    })


@app.route("/api/projects/<project_id>/files", methods=["GET"])
def get_project_files(project_id):
    """Get all files organized by stage."""
    proj = project_path(project_id)
    meta = json.loads((proj / "meta.json").read_text())
    return jsonify({
        "meta": meta,
        "source": list_files_in(proj / "source"),
        "frames": list_files_in(proj / "frames"),
        "swapped": list_files_in(proj / "swapped"),
        "final": list_files_in(proj / "final"),
    })


@app.route("/api/projects/<project_id>/open-folder", methods=["POST"])
def open_folder(project_id):
    """Open project folder in Finder."""
    proj = project_path(project_id)
    data = request.json or {}
    subfolder = data.get("subfolder", "")

    # subfolder comes from the request body; restrict it to the stage
    # directories this app creates rather than joining arbitrary user input
    # into a path that gets handed to `open`.
    if subfolder and subfolder not in STAGE_DIRS:
        return jsonify({"error": "Unknown subfolder"}), 400

    target = proj / subfolder if subfolder else proj
    if not target.exists():
        return jsonify({"error": "Folder not found"}), 404

    subprocess.run(["open", str(target)])
    return jsonify({"opened": str(target)})


@app.route("/api/projects/<project_id>/select-frame", methods=["POST"])
def select_frame(project_id):
    """Mark a frame as the selected reference frame."""
    proj = project_path(project_id)
    meta = json.loads((proj / "meta.json").read_text())
    data = request.json
    frame_name = data.get("frame")

    if not frame_name:
        return jsonify({"error": "No frame specified"}), 400

    frame_path = proj / "frames" / frame_name
    if not frame_path.exists():
        return jsonify({"error": "Frame not found"}), 404

    # Copy selected frame to a known location
    ref_path = proj / "frames" / "SELECTED_reference.jpg"
    shutil.copy2(frame_path, ref_path)

    # Delete all other frames — keep only the selected one and the reference copy
    frames_dir = proj / "frames"
    for f in frames_dir.iterdir():
        if f.is_file() and f.name != frame_name and f.name != "SELECTED_reference.jpg":
            f.unlink()

    meta["selected_frame"] = frame_name
    meta["status"] = "frame_selected"
    (proj / "meta.json").write_text(json.dumps(meta, indent=2))

    return jsonify({"status": "frame_selected", "frame": frame_name})


@app.route("/api/projects/<project_id>/open-frame", methods=["POST"])
def open_frame(project_id):
    """Open the selected frame in Preview for easy drag-and-drop."""
    proj = project_path(project_id)
    meta = json.loads((proj / "meta.json").read_text())
    data = request.json or {}
    frame_name = data.get("frame", meta.get("selected_frame"))

    if not frame_name:
        return jsonify({"error": "No frame selected"}), 400

    frame_path = proj / "frames" / frame_name
    if not frame_path.exists():
        return jsonify({"error": "Frame not found"}), 404

    subprocess.run(["open", str(frame_path)])
    return jsonify({"opened": str(frame_path)})


@app.route("/api/projects/<project_id>", methods=["DELETE"])
def delete_project(project_id):
    """Delete a project and all its files."""
    proj = project_path(project_id)
    shutil.rmtree(proj)
    return jsonify({"deleted": project_id})


@app.route("/api/projects/<project_id>/rename", methods=["POST"])
def rename_project(project_id):
    """Rename a project."""
    proj = project_path(project_id)
    meta = json.loads((proj / "meta.json").read_text())
    data = request.json
    new_name = data.get("name", "").strip()
    if new_name:
        meta["name"] = new_name
        (proj / "meta.json").write_text(json.dumps(meta, indent=2))
    return jsonify({"name": meta["name"]})


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5050
    print(f"\n  ◆ Reel Pipeline running at http://localhost:{port}")
    print(f"  ◆ Projects stored in {PROJECTS_DIR}\n")
    webbrowser.open(f"http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False)
