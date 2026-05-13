"""
OHL-Imagica — Indexer
----------------------
Two modes:
  Local mode  — reads from a local folder (original behaviour)
  Drive mode  — reads from Google Drive via service account (for GitHub Actions)

Local usage:
  python build_dam.py --source "G:/My Drive/OpenSesame/Images"

Drive/CI usage (set GOOGLE_SERVICE_ACCOUNT_JSON + ANTHROPIC_API_KEY as env vars):
  python build_dam.py --folder-id "1AbCdEf..." --output docs/index.html
"""

import os, sys, json, base64, io, re, datetime, math, argparse, urllib.parse, time
from pathlib import Path

# ── Args ────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description='OHL-Imagica Indexer')
parser.add_argument('--source', type=str, default=None,
                    help='Local folder to scan')
parser.add_argument('--folder-id', type=str, default=None,
                    help='Google Drive folder ID — enables Drive API mode')
parser.add_argument('--output', type=str, default=None,
                    help='Output HTML path (default: OHL-Imagica.html next to this script)')
parser.add_argument('--drive-folder-id', type=str, default=None,
                    help='(Legacy) Same as --folder-id')
parser.add_argument('--api-key', type=str, default=None,
                    help='Anthropic API key (or set ANTHROPIC_API_KEY env var)')
parser.add_argument('--skip-ai', action='store_true',
                    help='Skip AI captioning')
args = parser.parse_args()

# Normalise: --folder-id takes precedence over legacy --drive-folder-id
args.folder_id = args.folder_id or args.drive_folder_id

# ── Try to import Pillow ────────────────────────────────────────────────────
try:
    from PIL import Image, ImageOps
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("[INFO] Pillow not installed – no image thumbnails will be generated.")
    print("       Install with:  pip install Pillow")

# ── Try to import Anthropic ──────────────────────────────────────────────────
try:
    import anthropic as _anthropic_mod
    HAS_ANTHROPIC = True
except ImportError:
    HAS_ANTHROPIC = False

# ── Try to import Google API client ──────────────────────────────────────────
try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build as _build_service
    from googleapiclient.http import MediaIoBaseDownload
    HAS_GOOGLE = True
except ImportError:
    HAS_GOOGLE = False

# ── Config ──────────────────────────────────────────────────────────────────
SCRIPT_DIR      = Path(__file__).resolve().parent
THUMB_SIZE      = (140, 140)
SCAN_DIR        = Path(args.source).resolve() if args.source else SCRIPT_DIR
OUTPUT_HTML     = Path(args.output).resolve() if args.output else SCRIPT_DIR / "OHL-Imagica.html"
DRIVE_FOLDER_ID = args.folder_id   # used in both modes

# Drive API mode: triggered when --folder-id is set and service account JSON exists
DRIVE_API_MODE = bool(DRIVE_FOLDER_ID and os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON'))

# ── Anthropic client setup ───────────────────────────────────────────────────
ANTHROPIC_API_KEY = args.api_key or os.environ.get('ANTHROPIC_API_KEY', '')
USE_AI_CAPTIONS   = bool(ANTHROPIC_API_KEY) and not args.skip_ai and HAS_ANTHROPIC
AI_CLIENT         = None

if USE_AI_CAPTIONS:
    AI_CLIENT = _anthropic_mod.Anthropic(api_key=ANTHROPIC_API_KEY)
    print(f"[✓] AI captioning enabled (claude-haiku-4-5-20251001)")
elif args.api_key and not HAS_ANTHROPIC:
    print("[!] API key provided but 'anthropic' package not installed.")
    print("    Run:  pip install anthropic")
    USE_AI_CAPTIONS = False

# ── Caption cache ─────────────────────────────────────────────────────────────
# Stored next to this script as captions_cache.json.
# Each entry: { "size": int, "modified": str, "caption": str }
# Key: relative path from scan root (set properly in scan_folder).
CACHE_PATH: Path = SCRIPT_DIR / 'captions_cache.json'
CAPTION_CACHE: dict = {}

def load_cache():
    global CAPTION_CACHE
    if CACHE_PATH.exists():
        try:
            CAPTION_CACHE = json.loads(CACHE_PATH.read_text(encoding='utf-8'))
            print(f"[✓] Caption cache loaded — {len(CAPTION_CACHE)} entries ({CACHE_PATH.name})")
        except Exception:
            CAPTION_CACHE = {}

def save_cache():
    try:
        CACHE_PATH.write_text(json.dumps(CAPTION_CACHE, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        print(f"  [WARN] Could not save caption cache: {e}")

def get_cached_caption(rel_str: str, size: int, modified: str) -> str | None:
    """Return cached caption if the file hasn't changed, else None."""
    entry = CAPTION_CACHE.get(rel_str)
    if entry and entry.get('size') == size and entry.get('modified') == modified:
        return entry['caption']
    return None

def store_cached_caption(rel_str: str, size: int, modified: str, caption: str):
    CAPTION_CACHE[rel_str] = {'size': size, 'modified': modified, 'caption': caption}
    save_cache()   # persist immediately so a crash doesn't lose progress

IMAGE_EXTS  = {'.jpg','.jpeg','.png','.gif','.webp','.bmp','.tif','.tiff','.svg','.ico','.avif','.heic','.heif'}
VIDEO_EXTS  = {'.mp4','.mov','.avi','.mkv','.webm','.m4v','.wmv','.flv','.ogv'}
DESIGN_EXTS = {'.psd','.ai','.eps','.sketch','.fig','.xd'}
DOC_EXTS    = {'.pdf','.doc','.docx','.xls','.xlsx','.ppt','.pptx','.txt','.md'}
SKIP_EXTS   = {'.lnk','.ds_store','.ini','.db'}
SKIP_NAMES  = {'build_dam.py', 'OHL-Imagica.html', 'DAM_Chatbot.html', 'DAM.html', 'index.json'}

def fmt_size(b):
    if b < 1024:       return f"{b} B"
    if b < 1024**2:    return f"{b/1024:.1f} KB"
    return f"{b/1024**2:.1f} MB"

def auto_tags(rel_path: str):
    """Generate tags from folder hierarchy."""
    parts = Path(rel_path).parent.parts
    tags = []
    for p in parts:
        t = re.sub(r'[^a-z0-9]+', '-', p.lower()).strip('-')
        if t and t not in tags:
            tags.append(t)
    return tags

def make_thumbnail_b64(path: Path) -> str | None:
    """Return a base64-encoded JPEG thumbnail, or None on failure."""
    if not HAS_PIL:
        return None
    try:
        with Image.open(path) as img:
            img = ImageOps.exif_transpose(img)  # fix rotation
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            # Convert palette/RGBA to RGB for JPEG
            if img.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', img.size, (240, 242, 245))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=75, optimize=True)
            return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"  [WARN] thumb failed for {path.name}: {e}")
        return None

# ── Google Drive API helpers ─────────────────────────────────────────────────

def setup_drive_service():
    """Build a Google Drive API service from the service account JSON env var."""
    if not HAS_GOOGLE:
        print("[!] google-api-python-client not installed. Run: pip install -r requirements.txt")
        return None
    try:
        info  = json.loads(os.environ['GOOGLE_SERVICE_ACCOUNT_JSON'])
        creds = service_account.Credentials.from_service_account_info(
            info, scopes=['https://www.googleapis.com/auth/drive.readonly'])
        svc = _build_service('drive', 'v3', credentials=creds)
        print("[✓] Google Drive API connected via service account")
        return svc
    except Exception as e:
        print(f"[!] Drive API setup failed: {e}")
        return None

def make_thumbnail_b64_from_bytes(raw_bytes: bytes) -> str | None:
    """Generate a base64 JPEG thumbnail from raw image bytes."""
    if not HAS_PIL:
        return None
    try:
        with Image.open(io.BytesIO(raw_bytes)) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail(THUMB_SIZE, Image.LANCZOS)
            if img.mode in ('RGBA', 'LA', 'P'):
                bg = Image.new('RGB', img.size, (240, 242, 245))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                bg.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=75, optimize=True)
            return base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"  [WARN] thumb from bytes failed: {e}")
        return None

def download_drive_file(file_id: str, drive_svc) -> bytes | None:
    """Download a file from Drive and return raw bytes."""
    try:
        req = drive_svc.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        dl  = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue()
    except Exception as e:
        print(f"  [WARN] Drive download failed for {file_id}: {e}")
        return None

def scan_drive_folder(root_folder_id: str, drive_svc) -> list:
    """Recursively scan a Drive folder and return the same asset structure as scan_folder()."""
    assets = []
    counter = [0]

    _IMAGE_EXTS_BARE = {e.lstrip('.') for e in IMAGE_EXTS}
    _VIDEO_EXTS_BARE = {e.lstrip('.') for e in VIDEO_EXTS}
    _DESIGN_EXTS_BARE = {e.lstrip('.') for e in DESIGN_EXTS}
    _DOC_EXTS_BARE    = {e.lstrip('.') for e in DOC_EXTS}

    def recurse(folder_id, path_str):
        page_token = None
        while True:
            resp = drive_svc.files().list(
                q=f"'{folder_id}' in parents and trashed=false",
                fields="nextPageToken, files(id, name, size, modifiedTime, mimeType)",
                pageSize=1000,
                pageToken=page_token,
            ).execute()

            for item in resp.get('files', []):
                mime = item.get('mimeType', '')
                if mime == 'application/vnd.google-apps.folder':
                    sub = f"{path_str}/{item['name']}" if path_str else item['name']
                    recurse(item['id'], sub)
                    continue

                name = item['name']
                ext  = name.rsplit('.', 1)[-1].lower() if '.' in name else ''

                kind = 'other'
                if   ext in _IMAGE_EXTS_BARE:  kind = 'image'
                elif ext in _VIDEO_EXTS_BARE:  kind = 'video'
                elif ext in _DESIGN_EXTS_BARE: kind = 'design'
                elif ext in _DOC_EXTS_BARE:    kind = 'document'

                file_id  = item['id']
                size     = int(item.get('size', 0))
                modified = item['modifiedTime'][:10]   # 'YYYY-MM-DD'
                rel_str  = f"{path_str}/{name}" if path_str else name

                counter[0] += 1
                print(f"  [{counter[0]:>4}] {rel_str}", end='\r', flush=True)

                # Thumbnail: download file and resize locally
                thumb = None
                if kind == 'image' and ext != 'svg':
                    raw = download_drive_file(file_id, drive_svc)
                    if raw:
                        thumb = make_thumbnail_b64_from_bytes(raw)

                # AI caption (uses same cache as local mode)
                ai_desc = ''
                if kind == 'image' and ext != 'svg':
                    cached = get_cached_caption(rel_str, size, modified)
                    if cached is not None:
                        ai_desc = cached
                    else:
                        ai_desc = caption_image(None, thumb, rel_str, size, modified)
                        if ai_desc:
                            print(f"\n  [AI ✦] {name[:45]}: {ai_desc[:75]}…")

                # Per-file Drive URLs (direct, not search)
                drive_url = f"https://drive.google.com/file/d/{file_id}/view"

                tags     = [p for p in path_str.split('/') if p] if path_str else []
                industry = path_str.split('/')[0] if path_str else name

                assets.append({
                    "id":            rel_str,
                    "name":          name,
                    "path":          path_str or '.',
                    "industry":      industry,
                    "ext":           ext,
                    "kind":          kind,
                    "size":          size,
                    "sizeStr":       fmt_size(size),
                    "modified":      modified,
                    "tags":          tags,
                    "thumb":         thumb,
                    "driveUrl":      drive_url,
                    "aiDescription": ai_desc,
                })

            page_token = resp.get('nextPageToken')
            if not page_token:
                break

    recurse(root_folder_id, '')
    print()
    print(f"  Scanned {counter[0]} files from Google Drive")
    return assets


def caption_image(path: Path | None, thumb_b64: str | None,
                  rel_str: str, size: int, modified: str) -> str:
    """Return an AI caption for the image, using cache when available."""
    if not USE_AI_CAPTIONS or not AI_CLIENT:
        return ''

    # ── Cache hit: skip the API call entirely ─────────────────────────────────
    cached = get_cached_caption(rel_str, size, modified)
    if cached is not None:
        return cached

    # ── Cache miss: call Claude Haiku ─────────────────────────────────────────
    try:
        if thumb_b64:
            img_b64  = thumb_b64
            img_type = 'image/jpeg'
        elif path is not None:
            with open(path, 'rb') as f:
                raw = f.read()
            img_b64  = base64.b64encode(raw).decode()
            ext_map  = {'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png',
                        '.gif':'image/gif','.webp':'image/webp'}
            img_type = ext_map.get(path.suffix.lower(), 'image/jpeg')
        else:
            return ''   # no image data available

        response = AI_CLIENT.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=120,
            messages=[{
                'role': 'user',
                'content': [
                    {
                        'type': 'image',
                        'source': {'type': 'base64', 'media_type': img_type, 'data': img_b64},
                    },
                    {
                        'type': 'text',
                        'text': (
                            'Describe this image in 1-2 short sentences. '
                            'Focus on: the main subject, setting or background, '
                            'dominant colours, mood, and any visible text or logos. '
                            'Be specific and factual — no marketing language.'
                        ),
                    },
                ],
            }],
        )
        time.sleep(0.15)   # stay within rate limits
        caption = response.content[0].text.strip()
        store_cached_caption(rel_str, size, modified, caption)   # save to disk
        return caption
    except Exception as e:
        print(f"  [WARN] AI caption failed for {path.name}: {e}")
        return ''


def scan_folder(root: Path):
    assets = []
    total = 0
    skipped = 0

    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden directories
        dirnames[:] = [d for d in dirnames if not d.startswith('.')]
        for fname in filenames:
            if fname.startswith('.') or fname in SKIP_NAMES:
                continue
            fpath = Path(dirpath) / fname
            ext   = fpath.suffix.lower()
            if ext in SKIP_EXTS:
                skipped += 1
                continue

            rel   = fpath.relative_to(root)
            total += 1
            print(f"  [{total:>4}] {rel}", end='\r', flush=True)

            stat = fpath.stat()
            rel_str = str(rel).replace('\\', '/')

            # Determine type
            is_img    = ext in IMAGE_EXTS
            is_vid    = ext in VIDEO_EXTS
            is_design = ext in DESIGN_EXTS
            is_doc    = ext in DOC_EXTS

            if is_img:    kind = 'image'
            elif is_vid:  kind = 'video'
            elif is_design: kind = 'design'
            elif is_doc:  kind = 'document'
            else:         kind = 'other'

            # Thumbnail
            thumb = None
            if is_img and not ext == '.svg':
                thumb = make_thumbnail_b64(fpath)

            # AI caption (vision model describes image content)
            modified_str = datetime.datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d')
            ai_desc = ''
            if is_img and ext != '.svg':
                cached = get_cached_caption(rel_str, stat.st_size, modified_str)
                if cached is not None:
                    ai_desc = cached
                    # cached — no print noise, just use it silently
                else:
                    ai_desc = caption_image(fpath, thumb, rel_str, stat.st_size, modified_str)
                    if ai_desc:
                        print(f"  [AI ✦] {fname[:45]}: {ai_desc[:75]}…")

            # Parent folder (top-level industry name)
            parts = rel.parts
            industry = parts[1] if len(parts) > 2 else parts[0]

            # Google Drive view-only URL
            encoded_name = urllib.parse.quote(f'"{fname}"')  # exact-match search
            if DRIVE_FOLDER_ID:
                drive_url = (
                    f'https://drive.google.com/drive/folders/{DRIVE_FOLDER_ID}'
                    f'?q={encoded_name}'
                )
            else:
                drive_url = f'https://drive.google.com/drive/search?q={encoded_name}'

            asset = {
                "id":            rel_str,
                "name":          fname,
                "path":          str(rel.parent).replace('\\', '/'),
                "industry":      industry,
                "ext":           ext.lstrip('.').lower(),
                "kind":          kind,
                "size":          stat.st_size,
                "sizeStr":       fmt_size(stat.st_size),
                "modified":      modified_str,
                "tags":          auto_tags(rel_str),
                "thumb":         thumb,      # base64 string or null
                "driveUrl":      drive_url,  # view-only Google Drive link
                "aiDescription": ai_desc,    # Claude vision caption
            }
            assets.append(asset)

    print()
    print(f"  Scanned {total} files  ({skipped} shortcuts/system files skipped)")
    return assets

# ── HTML template ────────────────────────────────────────────────────────────
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OHL-Imagica</title>
<style>
:root {
  --bg:#f0f2f5; --surface:#fff; --surface2:#f4f5f7; --border:#e4e6ea;
  --text:#1a1d23; --text2:#6b7280; --accent:#4f46e5; --accent-l:rgba(79,70,229,.1);
  --user-bg:#4f46e5; --bot-bg:#fff; --shadow:0 2px 12px rgba(0,0,0,.08);
  --radius:14px; --header:60px; --chat-max:720px;
}
[data-theme=dark]{
  --bg:#0d0f14; --surface:#161922; --surface2:#1e2130; --border:#2a2e3d;
  --text:#e8eaf0; --text2:#7c8499; --bot-bg:#1e2130; --shadow:0 2px 12px rgba(0,0,0,.4);
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:var(--bg);color:var(--text);transition:background .2s,color .2s}

/* Layout */
#app{display:flex;flex-direction:column;height:100vh}
#header{height:var(--header);background:var(--surface);border-bottom:1px solid var(--border);display:flex;align-items:center;gap:12px;padding:0 20px;flex-shrink:0;box-shadow:var(--shadow)}
.logo{display:flex;align-items:center;gap:8px;font-weight:800;font-size:17px;color:var(--accent);letter-spacing:-.3px}
.logo-box{width:30px;height:30px;border-radius:8px;background:var(--accent);display:flex;align-items:center;justify-content:center}
.stats{font-size:12px;color:var(--text2);margin-left:4px}
.hdr-right{margin-left:auto;display:flex;align-items:center;gap:8px}

#chat-wrap{flex:1;overflow-y:auto;padding:20px;display:flex;flex-direction:column;align-items:center}
#chat{width:100%;max-width:var(--chat-max);display:flex;flex-direction:column;gap:18px;padding-bottom:120px}

#input-area{position:fixed;bottom:0;left:0;right:0;background:linear-gradient(transparent,var(--bg) 30%);padding:12px 20px 20px;display:flex;justify-content:center}
#input-box{width:100%;max-width:var(--chat-max);display:flex;gap:8px;background:var(--surface);border:1.5px solid var(--border);border-radius:28px;padding:6px 6px 6px 18px;box-shadow:var(--shadow);transition:border-color .15s}
#input-box:focus-within{border-color:var(--accent)}
#user-input{flex:1;border:none;background:none;color:var(--text);font-size:14px;outline:none;resize:none;min-height:28px;max-height:120px;font-family:inherit;line-height:1.5;padding:4px 0}
#user-input::placeholder{color:var(--text2)}
#send-btn{width:38px;height:38px;border-radius:50%;background:var(--accent);border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:background .15s,transform .1s}
#send-btn:hover{background:#4338ca}
#send-btn:active{transform:scale(.93)}
#send-btn svg{color:white}

/* Messages */
.msg-row{display:flex;gap:10px;align-items:flex-end}
.msg-row.user{flex-direction:row-reverse}
.avatar{width:32px;height:32px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:14px}
.bot-avatar{background:var(--accent);color:white;font-size:13px;font-weight:700}
.user-avatar{background:#e0e7ff;font-size:16px}
.bubble{max-width:82%;padding:11px 15px;border-radius:var(--radius);font-size:14px;line-height:1.55}
.bubble.user{background:var(--user-bg);color:white;border-bottom-right-radius:4px}
.bubble.bot{background:var(--bot-bg);color:var(--text);border:1px solid var(--border);border-bottom-left-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.05)}
.bubble.bot b{color:var(--accent)}

/* Results grid inside bubble */
.results-header{font-size:13px;color:var(--text2);margin-bottom:10px}
.img-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:8px;margin-top:4px}
.img-card{background:var(--surface2);border-radius:10px;overflow:hidden;border:1px solid var(--border);cursor:pointer;transition:transform .15s,box-shadow .15s}
.img-card:hover{transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.12)}
.img-thumb{width:100%;aspect-ratio:1;object-fit:cover;display:block;background:var(--border)}
.img-thumb.placeholder{display:flex;align-items:center;justify-content:center;font-size:32px;background:var(--surface2)}
.img-info{padding:5px 7px 6px}
.img-name{font-size:10px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.img-sub{font-size:10px;color:var(--text2)}
.drive-btn{display:none;align-items:center;gap:4px;padding:4px 8px;margin:0 7px 7px;border-radius:6px;background:var(--accent-l);color:var(--accent);font-size:10px;font-weight:600;text-decoration:none;transition:background .15s;border:1px solid transparent}
.drive-btn:hover{background:var(--accent);color:white}
.img-card:hover .drive-btn{display:inline-flex}
.tag-row{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}
.tag{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;background:var(--accent-l);color:var(--accent);cursor:pointer;border:1px solid transparent;transition:all .12s}
.tag:hover{background:var(--accent);color:white}
.show-more{display:inline-block;margin-top:10px;font-size:12px;color:var(--accent);cursor:pointer;text-decoration:underline;background:none;border:none;padding:0}
.suggestions{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.suggestion{padding:6px 12px;border-radius:16px;border:1.5px solid var(--border);background:var(--surface2);font-size:12px;color:var(--text2);cursor:pointer;transition:all .12s}
.suggestion:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-l)}

/* Typing indicator */
.typing{display:flex;gap:4px;align-items:center;padding:14px 16px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--text2);animation:bounce .9s ease-in-out infinite}
.dot:nth-child(2){animation-delay:.15s}
.dot:nth-child(3){animation-delay:.3s}
@keyframes bounce{0%,60%,100%{transform:translateY(0)}30%{transform:translateY(-6px)}}

/* Icon btn */
.icon-btn{width:34px;height:34px;border-radius:8px;border:none;background:none;cursor:pointer;color:var(--text2);display:flex;align-items:center;justify-content:center;transition:background .15s,color .15s}
.icon-btn:hover{background:var(--surface2);color:var(--text)}

/* Lightbox */
#lightbox{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:9999;align-items:center;justify-content:center;flex-direction:column;gap:12px;padding:20px}
#lightbox.show{display:flex}
#lb-img{max-width:90vw;max-height:72vh;object-fit:contain;border-radius:8px;box-shadow:0 8px 40px rgba(0,0,0,.6);cursor:default}
#lb-footer{display:flex;flex-direction:column;align-items:center;gap:8px}
#lb-caption{color:rgba(255,255,255,.65);font-size:13px;text-align:center}
#lb-drive-btn{display:inline-flex;align-items:center;gap:6px;padding:8px 18px;border-radius:20px;background:#fff;color:#1a1d23;font-size:13px;font-weight:600;text-decoration:none;border:none;cursor:pointer;transition:opacity .15s}
#lb-drive-btn:hover{opacity:.88}
#lb-drive-btn svg{flex-shrink:0}
#lb-close{position:absolute;top:16px;right:16px;background:rgba(255,255,255,.15);border:none;border-radius:50%;width:36px;height:36px;cursor:pointer;color:white;font-size:18px;display:flex;align-items:center;justify-content:center;transition:background .15s}
#lb-close:hover{background:rgba(255,255,255,.3)}

::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
</style>
</head>
<body data-theme="">
<div id="app">
  <div id="header">
    <div class="logo">
      <div class="logo-box">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.5">
          <rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/>
          <rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>
        </svg>
      </div>
      OHL-Imagica
    </div>
    <span class="stats" id="hdr-stats"></span>
    <div class="hdr-right">
      <button class="icon-btn" onclick="toggleTheme()" title="Toggle theme">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 12.79A9 9 0 1111.21 3a7 7 0 009.79 9.79z"/>
        </svg>
      </button>
    </div>
  </div>

  <div id="chat-wrap">
    <div id="chat"></div>
  </div>

  <div id="input-area">
    <div id="input-box">
      <textarea id="user-input" placeholder="Ask me anything… 'show automobile images', 'find PSD files', 'how many videos?'" rows="1"
        onkeydown="inputKey(event)" oninput="autoResize(this)"></textarea>
      <button id="send-btn" onclick="sendMessage()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
        </svg>
      </button>
    </div>
  </div>
</div>

<div id="lightbox">
  <button id="lb-close" onclick="closeLightbox()">✕</button>
  <img id="lb-img" src="" alt="">
  <div id="lb-footer">
    <div id="lb-caption"></div>
    <a id="lb-drive-btn" href="#" target="_blank" rel="noopener noreferrer">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
        <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/>
        <polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
      </svg>
      Open in Drive (view only)
    </a>
  </div>
</div>

<script>
// ══════════════════════════════════════════════
// ASSET INDEX (injected by build_dam.py)
// ══════════════════════════════════════════════
const ASSETS = __ASSETS_JSON__;

// ══════════════════════════════════════════════
// SEARCH ENGINE
// ══════════════════════════════════════════════

// KIND_WORDS: only unambiguous file-type/tool words trigger a kind filter.
// Subject-matter words like 'design', 'photo', 'graphic' are intentionally
// excluded — they should match file names, not act as type filters.
const KIND_WORDS = {
  video:    ['video','videos','clip','clips','footage','mp4','mov','avi','mkv','webm','reel'],
  design:   ['psd','photoshop','illustrator','figma','.sketch','.xd'],
  document: ['docx','xlsx','pptx','spreadsheet','powerpoint'],
};

const INDUSTRY_WORDS = {};
ASSETS.forEach(a => {
  const ind = (a.industry || '').toLowerCase();
  if (!INDUSTRY_WORDS[ind]) INDUSTRY_WORDS[ind] = [];
  ind.split(/[\s_\-]+/).filter(Boolean).forEach(w => {
    if (w.length > 2 && !INDUSTRY_WORDS[ind].includes(w)) INDUSTRY_WORDS[ind].push(w);
  });
});

// ── Tokenise a string by common filename separators ──────────────────────────
function tokenize(str) {
  return String(str).toLowerCase().split(/[\s_\-\.\/\\]+/).filter(t => t.length > 1);
}

// ── Very light stemmer: strip common English suffixes ────────────────────────
function stem(w) {
  w = String(w);
  if (w.length > 6 && w.endsWith('ings'))  return w.slice(0, -4);
  if (w.length > 5 && w.endsWith('ing'))   return w.slice(0, -3);
  if (w.length > 5 && w.endsWith('tion'))  return w.slice(0, -4);
  if (w.length > 4 && w.endsWith('ness'))  return w.slice(0, -4);
  if (w.length > 4 && w.endsWith('ed'))    return w.slice(0, -2);
  if (w.length > 4 && w.endsWith('er'))    return w.slice(0, -2);
  if (w.length > 4 && w.endsWith('ly'))    return w.slice(0, -2);
  if (w.length > 3 && w.endsWith('s') && !w.endsWith('ss')) return w.slice(0, -1);
  return w;
}

function parseQuery(q) {
  const lq = q.toLowerCase().trim();
  const tokens = lq.split(/\s+/);

  // ── Detect file kind filter (conservative — unambiguous type words only) ──
  let kindFilter = null;
  for (const [kind, words] of Object.entries(KIND_WORDS)) {
    if (words.some(w => tokens.includes(w) || lq.includes(w))) {
      kindFilter = kind;
      break;
    }
  }
  // "show images" / "show photos" → image kind filter (explicit)
  if (!kindFilter && /\b(images|photos|pictures)\b/.test(lq) && tokens.length <= 3) {
    kindFilter = 'image';
  }

  // ── Detect industry / folder filter ──────────────────────────────────────
  let industryFilter = null;
  for (const [ind, words] of Object.entries(INDUSTRY_WORDS)) {
    if (words.some(w => tokens.includes(w))) { industryFilter = ind; break; }
  }

  // ── Detect extension filter ───────────────────────────────────────────────
  const extMatch = lq.match(/\b(jpg|jpeg|png|gif|webp|psd|mp4|mov|pdf|svg|tiff|bmp|ai)\b/g) || [];
  const extFilter = extMatch[0] || null;

  // ── General keywords — strip only true stop words ─────────────────────────
  // NOTE: subject-matter words ('design','photo','night','graphic' etc.) are
  // intentionally kept so they can match filenames.
  const stopWords = new Set([
    'show','me','find','get','all','the','a','an','of','in','for','with','some',
    'any','can','you','please','list','give','what','how','many','are','there',
    'files','from','and','or','that','have','has','been','is','was','my','want','need',
  ]);
  const keywords = tokens.filter(t => t.length > 1 && !stopWords.has(t) && !/^\d+$/.test(t));

  // Intent detection
  const isCount = /\b(how many|count|total|number of)\b/.test(lq);
  const isStats = /\b(stats|statistics|summary|overview|breakdown|report)\b/.test(lq);

  return { kindFilter, industryFilter, extFilter, keywords, isCount, isStats, lq };
}

function scoreAsset(asset, parsed) {
  let score = 0;
  const { kindFilter, industryFilter, extFilter, keywords } = parsed;

  // ── Hard filters ──────────────────────────────────────────────────────────
  if (kindFilter && asset.kind !== kindFilter) return -1;
  if (extFilter  && asset.ext  !== extFilter)  return -1;
  if (industryFilter) {
    const aInd = (asset.industry || '').toLowerCase();
    if (!aInd.includes(industryFilter) && !industryFilter.includes(aInd)) return -1;
  }

  // ── No keyword query → show everything that passed hard filters ───────────
  if (keywords.length === 0) return 1;

  // ── Build searchable token sets from the asset ────────────────────────────
  const nameNoExt   = asset.name.replace(/\.[^.]+$/, '');
  const nameTokens  = tokenize(nameNoExt);
  const pathTokens  = tokenize(asset.path || '');
  const tagTokens   = (asset.tags || []).flatMap(t => tokenize(t));
  const aiTokens    = tokenize(asset.aiDescription || '');
  const assetTokens = [...new Set([...nameTokens, ...pathTokens, ...tagTokens, ...aiTokens])];
  const fullText    = [asset.name, asset.path, asset.industry,
                       ...(asset.tags || []), asset.aiDescription || '']
                        .join(' ').toLowerCase();

  let kwMatched = 0;

  for (const kw of keywords) {
    const kwStem = stem(kw);

    // 1. Exact token match (highest confidence)
    const exactToken = assetTokens.some(t => t === kw);
    // 2. Stem match (e.g. "design" matches "designing", "heights" matches "height")
    const stemMatch  = !exactToken && assetTokens.some(t => stem(t) === kwStem || t === kwStem);
    // 3. Prefix match for longer words (e.g. "lead" matches "leadership")
    const prefixMatch = !exactToken && !stemMatch && kw.length >= 4 &&
                        assetTokens.some(t => t.startsWith(kw) || kw.startsWith(t));
    // 4. Substring anywhere in full text (weakest — catches edge cases)
    const subMatch   = !exactToken && !stemMatch && !prefixMatch && fullText.includes(kw);

    if      (exactToken)  { score += kw.length > 5 ? 6 : 4; kwMatched++; }
    else if (stemMatch)   { score += kw.length > 5 ? 4 : 3; kwMatched++; }
    else if (prefixMatch) { score += 2; kwMatched++; }
    else if (subMatch)    { score += 1; kwMatched++; }
  }

  // ── Require at least one keyword to have matched ──────────────────────────
  if (kwMatched === 0) return -1;

  // Small boost for images (only when something already matched)
  if (!kindFilter && asset.kind === 'image') score += 0.5;

  return score;
}

function search(query, limit = 24) {
  const parsed = parseQuery(query);

  if (parsed.isStats) return { type: 'stats', parsed };
  if (parsed.isCount) return { type: 'count', parsed };

  const scored = ASSETS
    .map(a => ({ asset: a, score: scoreAsset(a, parsed) }))
    .filter(x => x.score > 0)
    .sort((a, b) => b.score - a.score);

  return { type: 'results', assets: scored.map(x => x.asset), total: scored.length, limit, parsed };
}

// ══════════════════════════════════════════════
// RESPONSE BUILDER
// ══════════════════════════════════════════════
function buildResponse(query) {
  const result = search(query);

  if (result.type === 'stats') return statsResponse();
  if (result.type === 'count') return countResponse(result.parsed);

  const { assets, total, limit, parsed } = result;

  if (total === 0) {
    return {
      text: `I couldn't find any assets matching <b>"${escHtml(query)}"</b>. Try a different keyword, or ask me for the full library overview.`,
      grid: null,
      suggestions: ['Show all images', 'Show all videos', 'Library stats', ...Object.keys(INDUSTRY_WORDS).slice(0,3).map(i => `Show ${i} assets`)]
    };
  }

  const shown = assets.slice(0, limit);
  const kindsFound = [...new Set(assets.map(a => a.kind))];
  const indsFound  = [...new Set(assets.map(a => a.industry))];

  let text = `Found <b>${total} asset${total !== 1 ? 's' : ''}</b>`;
  if (parsed.industryFilter) text += ` in <b>${parsed.industryFilter}</b>`;
  if (parsed.kindFilter)     text += ` (${parsed.kindFilter}s)`;
  if (total > limit) text += ` — showing first ${limit}`;
  text += '.';

  // Suggest related filters
  const tags = [...new Set(assets.flatMap(a => a.tags || []))].slice(0, 6);
  const freepikUrl = `https://www.freepik.com/search?query=${encodeURIComponent(query)}`;

  return {
    text,
    grid: shown,
    totalAssets: total,
    shown: shown.length,
    tags,
    freepikUrl,
    query,
    suggestions: total > limit ? [`Show more results`, ...tags.slice(0,2).map(t => `Filter by tag: ${t}`)] : []
  };
}

function statsResponse() {
  const total  = ASSETS.length;
  const byKind = {};
  const byInd  = {};
  let totalSize = 0;
  ASSETS.forEach(a => {
    byKind[a.kind] = (byKind[a.kind] || 0) + 1;
    byInd[a.industry] = (byInd[a.industry] || 0) + 1;
    totalSize += a.size;
  });

  const sizeStr = totalSize < 1024**3
    ? `${(totalSize/1024**2).toFixed(1)} MB`
    : `${(totalSize/1024**3).toFixed(2)} GB`;

  let text = `📊 <b>Library Overview</b><br><br>`;
  text += `<b>${total}</b> total assets · <b>${sizeStr}</b> total size<br><br>`;
  text += `<b>By type:</b><br>`;
  for (const [k, n] of Object.entries(byKind).sort((a,b) => b[1]-a[1])) {
    text += `&nbsp;&nbsp;${kindIcon(k)} ${k}: <b>${n}</b><br>`;
  }
  text += `<br><b>By folder:</b><br>`;
  for (const [ind, n] of Object.entries(byInd).sort((a,b) => b[1]-a[1])) {
    text += `&nbsp;&nbsp;📁 ${ind}: <b>${n}</b><br>`;
  }

  const uniqueTags = [...new Set(ASSETS.flatMap(a => a.tags || []))];
  text += `<br><b>${uniqueTags.length}</b> unique tags auto-generated.`;

  return {
    text,
    grid: null,
    suggestions: Object.keys(byInd).map(i => `Show ${i} images`)
  };
}

function countResponse(parsed) {
  let filtered = ASSETS;
  if (parsed.kindFilter)     filtered = filtered.filter(a => a.kind === parsed.kindFilter);
  if (parsed.industryFilter) filtered = filtered.filter(a => (a.industry||'').toLowerCase().includes(parsed.industryFilter));
  if (parsed.extFilter)      filtered = filtered.filter(a => a.ext === parsed.extFilter);

  const n = filtered.length;
  let text = `There ${n === 1 ? 'is' : 'are'} <b>${n} asset${n !== 1 ? 's' : ''}</b>`;
  if (parsed.kindFilter)     text += ` of type <b>${parsed.kindFilter}</b>`;
  if (parsed.industryFilter) text += ` in <b>${parsed.industryFilter}</b>`;
  text += ' in the library.';

  return { text, grid: null, suggestions: [`Show me those ${n} assets`, 'Library stats'] };
}

function kindIcon(k) {
  return { image:'🖼️', video:'🎬', design:'🎨', document:'📄', other:'📎' }[k] || '📎';
}

// ══════════════════════════════════════════════
// CHAT UI
// ══════════════════════════════════════════════
const chat = document.getElementById('chat');
let allResults = [];   // holds last full result set for "show more"
let shownCount = 0;

function escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function addMessage(role, contentFn) {
  const row = document.createElement('div');
  row.className = `msg-row ${role}`;
  const avatar = document.createElement('div');
  avatar.className = `avatar ${role === 'bot' ? 'bot-avatar' : 'user-avatar'}`;
  avatar.textContent = role === 'bot' ? 'AI' : '👤';
  const bubble = document.createElement('div');
  bubble.className = `bubble ${role}`;
  contentFn(bubble);
  row.appendChild(role === 'bot' ? avatar : bubble);
  row.appendChild(role === 'bot' ? bubble : avatar);
  chat.appendChild(row);
  scrollBottom();
  return bubble;
}

function addTyping() {
  const row = document.createElement('div');
  row.className = 'msg-row bot';
  row.id = 'typing-row';
  row.innerHTML = `
    <div class="avatar bot-avatar">AI</div>
    <div class="bubble bot"><div class="typing"><span class="dot"></span><span class="dot"></span><span class="dot"></span></div></div>`;
  chat.appendChild(row);
  scrollBottom();
}
function removeTyping() { document.getElementById('typing-row')?.remove(); }

function scrollBottom() {
  setTimeout(() => {
    document.getElementById('chat-wrap').scrollTo({ top: 99999, behavior: 'smooth' });
  }, 50);
}

function renderBubble(bubble, resp) {
  bubble.innerHTML = '';

  // Text
  const textDiv = document.createElement('div');
  textDiv.innerHTML = resp.text;
  bubble.appendChild(textDiv);

  // Image grid
  if (resp.grid && resp.grid.length) {
    const header = document.createElement('div');
    header.className = 'results-header';
    header.style.marginTop = '10px';
    bubble.appendChild(header);

    const grid = document.createElement('div');
    grid.className = 'img-grid';
    resp.grid.forEach(a => grid.appendChild(makeImgCard(a)));
    bubble.appendChild(grid);

    // "Show more" button
    if (resp.totalAssets > resp.shown) {
      const btn = document.createElement('button');
      btn.className = 'show-more';
      btn.textContent = `▼ Show more (${resp.totalAssets - resp.shown} remaining)`;
      btn.onclick = () => { showMoreResults(bubble, resp); btn.remove(); };
      bubble.appendChild(btn);
    }
  }

  // Tag suggestions
  if (resp.tags && resp.tags.length) {
    const tagRow = document.createElement('div');
    tagRow.className = 'tag-row';
    tagRow.innerHTML = '<span style="font-size:11px;color:var(--text2);margin-right:4px">Filter:</span>';
    resp.tags.forEach(tag => {
      const t = document.createElement('span');
      t.className = 'tag';
      t.textContent = tag;
      t.onclick = () => sendMsg(`show ${tag} assets`);
      tagRow.appendChild(t);
    });
    bubble.appendChild(tagRow);
  }

  // Quick suggestions
  if (resp.suggestions && resp.suggestions.length) {
    const s = document.createElement('div');
    s.className = 'suggestions';
    resp.suggestions.forEach(sug => {
      const btn = document.createElement('button');
      btn.className = 'suggestion';
      btn.textContent = sug;
      btn.onclick = () => sendMsg(sug);
      s.appendChild(btn);
    });
    bubble.appendChild(s);
  }

  // Freepik external search link
  if (resp.freepikUrl) {
    const fp = document.createElement('div');
    fp.style.cssText = 'margin-top:10px';
    fp.innerHTML = `<a href="${escHtml(resp.freepikUrl)}" target="_blank" rel="noopener noreferrer"
      style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--text2);text-decoration:none;transition:color .15s"
      onmouseover="this.style.color='var(--accent)'" onmouseout="this.style.color='var(--text2)'">
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/>
        <polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
      </svg>
      Also search "<b>${escHtml(resp.query)}</b>" on Freepik
    </a>`;
    bubble.appendChild(fp);
  }
}

function showMoreResults(bubble, resp) {
  allResults = allResults.length ? allResults : resp.grid;
  // Already showing resp.shown; now load next batch
  const nextBatch = allResults.slice(resp.shown, resp.shown + 24);
  const grid = bubble.querySelector('.img-grid');
  nextBatch.forEach(a => grid.appendChild(makeImgCard(a)));
  const remaining = resp.totalAssets - resp.shown - nextBatch.length;
  if (remaining > 0) {
    const btn = document.createElement('button');
    btn.className = 'show-more';
    btn.textContent = `▼ Show more (${remaining} remaining)`;
    btn.onclick = () => { showMoreResults(bubble, { ...resp, shown: resp.shown + nextBatch.length, totalAssets: resp.totalAssets }); btn.remove(); };
    bubble.appendChild(btn);
  }
}

function makeImgCard(a) {
  const card = document.createElement('div');
  card.className = 'img-card';
  card.title = a.name;
  card.onclick = () => openLightbox(a);

  let thumbHtml;
  if (a.thumb) {
    thumbHtml = `<img class="img-thumb" src="data:image/jpeg;base64,${a.thumb}" alt="${escHtml(a.name)}" loading="lazy">`;
  } else {
    const icon = kindIcon(a.kind);
    thumbHtml = `<div class="img-thumb placeholder">${icon}</div>`;
  }

  const driveHtml = a.driveUrl
    ? `<a class="drive-btn" href="${escHtml(a.driveUrl)}" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">
        <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <path d="M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6"/>
          <polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
        </svg>
        Open in Drive
      </a>`
    : '';
  card.innerHTML = `
    ${thumbHtml}
    <div class="img-info">
      <div class="img-name" title="${escHtml(a.name)}">${escHtml(a.name)}</div>
      <div class="img-sub">${escHtml(a.sizeStr)} · ${escHtml(a.ext.toUpperCase())}</div>
    </div>
    ${driveHtml}`;
  return card;
}

function openLightbox(a) {
  document.getElementById('lb-img').src = a.thumb ? `data:image/jpeg;base64,${a.thumb}` : '';
  document.getElementById('lb-img').style.display = a.thumb ? 'block' : 'none';
  const caption = a.aiDescription
    ? `${a.aiDescription}`
    : `${a.name}  ·  ${a.path}  ·  ${a.sizeStr}`;
  document.getElementById('lb-caption').textContent = caption;
  const driveBtn = document.getElementById('lb-drive-btn');
  driveBtn.href = a.driveUrl || '#';
  driveBtn.style.display = a.driveUrl ? 'inline-flex' : 'none';
  document.getElementById('lightbox').classList.add('show');
}
function closeLightbox() { document.getElementById('lightbox').classList.remove('show'); }
// Close lightbox on backdrop click (not on button/link clicks)
document.getElementById('lightbox').addEventListener('click', function(e) {
  if (e.target === this) closeLightbox();
});

// ══════════════════════════════════════════════
// INPUT HANDLING
// ══════════════════════════════════════════════
function inputKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
}
function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}
function sendMessage() {
  const inp = document.getElementById('user-input');
  const q = inp.value.trim();
  if (!q) return;
  sendMsg(q);
  inp.value = '';
  inp.style.height = 'auto';
}
function sendMsg(q) {
  addMessage('user', b => { b.textContent = q; });
  addTyping();
  setTimeout(() => {
    removeTyping();
    const resp = buildResponse(q);
    addMessage('bot', b => renderBubble(b, resp));
  }, 400 + Math.random() * 300);
}

// ══════════════════════════════════════════════
// THEME
// ══════════════════════════════════════════════
function toggleTheme() {
  const d = document.body.getAttribute('data-theme') === 'dark';
  document.body.setAttribute('data-theme', d ? '' : 'dark');
  localStorage.setItem('dam_theme', d ? '' : 'dark');
}
(function initTheme(){
  const t = localStorage.getItem('dam_theme');
  if (t) document.body.setAttribute('data-theme', t);
})();

// ══════════════════════════════════════════════
// WELCOME MESSAGE
// ══════════════════════════════════════════════
(function init() {
  const total  = ASSETS.length;
  const imgs   = ASSETS.filter(a => a.kind === 'image').length;
  const vids   = ASSETS.filter(a => a.kind === 'video').length;
  const inds   = [...new Set(ASSETS.map(a => a.industry))];

  const aiCaptioned = ASSETS.filter(a => a.aiDescription).length;
  const aiNote = aiCaptioned > 0 ? ` · ${aiCaptioned} AI-captioned` : '';
  document.getElementById('hdr-stats').textContent =
    `${total} assets · ${imgs} images · ${vids} videos · ${inds.length} folders${aiNote}`;

  const bubble = addMessage('bot', b => {});
  bubble.innerHTML = `
    Hi! I'm <b>Imagica</b> ✦, your smart asset assistant 👋<br>
    I have <b>${total} assets</b> indexed across <b>${inds.length} folders</b>:`;

  const sugg = document.createElement('div');
  sugg.className = 'suggestions';
  [
    'Show all images',
    'Show videos',
    'Find automobile photos',
    'How many PSD files?',
    'Library stats',
    ...inds.slice(0,3).map(i => `Show ${i} assets`),
  ].forEach(s => {
    const btn = document.createElement('button');
    btn.className = 'suggestion';
    btn.textContent = s;
    btn.onclick = () => sendMsg(s);
    sugg.appendChild(btn);
  });
  bubble.appendChild(sugg);
})();
</script>
</body>
</html>
"""

def main():
    print("=" * 60)
    print("  OHL-Imagica — Indexer")
    print("=" * 60)

    if DRIVE_API_MODE:
        print(f"  Mode   : Google Drive API")
        print(f"  Folder : {DRIVE_FOLDER_ID}")
    else:
        print(f"  Mode   : Local")
        print(f"  Source : {SCAN_DIR}")
    print(f"  Output : {OUTPUT_HTML}")
    print()

    # Ensure output directory exists (e.g. docs/ for GitHub Pages)
    OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)

    if not DRIVE_API_MODE and not SCAN_DIR.exists():
        print(f"[✗] Source folder not found: {SCAN_DIR}")
        sys.exit(1)

    if USE_AI_CAPTIONS:
        load_cache()

    if DRIVE_API_MODE:
        drive_svc = setup_drive_service()
        if not drive_svc:
            print("[✗] Could not connect to Google Drive. Aborting.")
            sys.exit(1)
        print("[1/3] Scanning Google Drive folder…")
        assets = scan_drive_folder(DRIVE_FOLDER_ID, drive_svc)
    else:
        print("[1/3] Scanning local folder…")
        assets = scan_folder(SCAN_DIR)

    print(f"\n[2/3] Building HTML chatbot ({len(assets)} assets)…")

    # Inject assets JSON into HTML template
    assets_json = json.dumps(assets, ensure_ascii=False, separators=(',', ':'))
    html = HTML_TEMPLATE.replace('__ASSETS_JSON__', assets_json)

    print(f"[3/3] Writing {OUTPUT_HTML.name}…")
    OUTPUT_HTML.write_text(html, encoding='utf-8')

    size_kb = OUTPUT_HTML.stat().st_size / 1024
    print(f"\n✅  Done!  DAM_Chatbot.html ({size_kb:.0f} KB)")
    print(f"\n   Open this file in Chrome or Edge:")
    print(f"   {OUTPUT_HTML}")
    print()

    # Summary
    by_kind = {}
    ai_count = sum(1 for a in assets if a.get('aiDescription'))
    for a in assets:
        by_kind[a['kind']] = by_kind.get(a['kind'], 0) + 1
    print("   Asset breakdown:")
    for k, n in sorted(by_kind.items(), key=lambda x: -x[1]):
        print(f"     {k:10s}: {n}")
    if USE_AI_CAPTIONS:
        print(f"\n   AI captions generated: {ai_count} / {len(assets)}")

if __name__ == '__main__':
    main()
