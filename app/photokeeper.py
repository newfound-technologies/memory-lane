#!/usr/bin/env python3
"""
Memory Lane (PhotoKeeper) - gathers photos and videos from folders, removes duplicates,
sorts them by date and type, and files them into one clean library
(for example, inside iCloud Drive).

SAFETY: Your original files are only ever READ. PhotoKeeper copies; it never
moves, renames, edits or deletes anything in your source folders.

Run:  python photokeeper.py            (opens the app in your web browser)
CLI:  python photokeeper.py --cli --src "D:\\Pictures" --dest "C:\\Users\\you\\iCloudDrive\\Photo Library" [--dry-run]
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    HAVE_PIL = True
except ImportError:  # pragma: no cover
    HAVE_PIL = False

try:  # lets Pillow open iPhone HEIC photos
    import pillow_heif
    pillow_heif.register_heif_opener()
    HAVE_HEIF = True
except Exception:
    HAVE_HEIF = False

APP_NAME = "Memory Lane"
UPDATE_REPO = "newfound-technologies/memory-lane"      # where new versions are published (GitHub Releases)
VERSION = "2.8.0"

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".bmp",
             ".tif", ".tiff", ".webp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".wmv", ".mts"}
SKIP_DIRS = {"$recycle.bin", "system volume information", ".photokeeper",
             "node_modules", "appdata", "windows", "program files",
             "program files (x86)", "__pycache__"}
SMALL_IMAGE_BYTES = 20 * 1024  # thumbnails / icons / tiny web images

SCREENSHOT_NAME = re.compile(r"screen\s*shot|screenshot|screen_shot|snip", re.I)
SCREEN_SIZES = {(1920, 1080), (2560, 1440), (1366, 768), (1536, 864),
                (1440, 900), (2880, 1800), (3840, 2160), (1280, 720),
                (1170, 2532), (1179, 2556), (1284, 2778), (1290, 2796),
                (1125, 2436), (1242, 2688), (828, 1792), (750, 1334),
                (1080, 1920), (1080, 2340), (1080, 2400), (1440, 3200)}

DATE_IN_NAME = re.compile(
    r"(?<!\d)(19[89]\d|20[0-4]\d)[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")

TAG_SHOW_MIN = 0.30
HEIC_FOLDER = "iPhone HEIC"
KINDS = ("Photos", "Screenshots", "Videos", HEIC_FOLDER)

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

CONFIG_DIR = Path(os.environ.get("PHOTOKEEPER_HOME") or (Path.home() / ".photokeeper"))
MODEL_DIR = CONFIG_DIR / "models"
CONFIG_FILE = CONFIG_DIR / "config.json"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def find_icloud_drive():
    """Best guess at the iCloud Drive folder on this computer."""
    home = Path.home()
    for cand in (home / "iCloudDrive", home / "iCloud Drive",
                 home / "iCloudDrive" / "iCloud Drive"):
        if cand.is_dir():
            return str(cand)
    return ""


def load_config():
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def quick_key(path, size):
    """Fast fingerprint: file size + first and last 64 KB. Reads at most 128 KB per
    file instead of the whole thing, which makes huge libraries far faster."""
    h = hashlib.sha1()
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(65536))
        if size > 131072:
            f.seek(size - 65536)
            h.update(f.read(65536))
    return f"{size}:{h.hexdigest()}"


def parse_exif_date(value):
    if not value:
        return None
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    value = str(value).strip().replace("\x00", "")
    m = re.match(r"(\d{4})[:\-](\d{2})[:\-](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", value)
    if not m:
        return None
    try:
        d = dt.datetime(*[int(x) for x in m.groups()])
    except ValueError:
        return None
    if d.year < 1980 or d > dt.datetime.now() + dt.timedelta(days=2):
        return None
    return d


def date_from_name(name):
    m = DATE_IN_NAME.search(name)
    if not m:
        return None
    try:
        d = dt.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    if d > dt.datetime.now() + dt.timedelta(days=2):
        return None
    return d


def dhash(img):
    """64-bit 'difference hash': similar-looking pictures get similar hashes."""
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = g.tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (1 if px[row * 9 + col] > px[row * 9 + col + 1] else 0)
    return bits


def hamming(a, b):
    return bin(a ^ b).count("1")


def inspect_image(path):
    """Return dict(taken, taken_src, make, size, phash) - best effort."""
    out = {"taken": None, "make": "", "size": None, "phash": None}
    if not HAVE_PIL:
        return out
    try:
        with Image.open(path) as im:
            out["size"] = im.size
            try:
                exif = im.getexif()
                make = exif.get(271) or ""
                model = exif.get(272) or ""
                out["make"] = (str(make) + str(model)).strip()
                sub = {}
                try:
                    sub = exif.get_ifd(0x8769)
                except Exception:
                    pass
                out["taken"] = (parse_exif_date(sub.get(36867))
                                or parse_exif_date(sub.get(36868))
                                or parse_exif_date(exif.get(306)))
            except Exception:
                pass
            try:
                if im.format == "JPEG":
                    im.draft("L", (128, 128))
                out["phash"] = dhash(im)
            except Exception:
                pass
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# catalog (remembers everything it has filed, so re-runs are fast and safe)
#
# The working database lives on THIS computer (not inside iCloud Drive, where
# constant syncing can damage a database). A backup copy is saved into the
# library folder after every job.
# --------------------------------------------------------------------------
def lib_dir(dest):
    p = Path(dest).expanduser()
    try:
        p = p.resolve()
    except Exception:
        pass
    key = hashlib.sha1(str(p).lower().encode("utf-8", "ignore")).hexdigest()[:12]
    d = CONFIG_DIR / "libs" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def catalog_exists(dest):
    dest = Path(dest).expanduser()
    if not str(dest).strip() or str(dest) == ".":
        return False
    sync = dest / ".photokeeper"
    return ((lib_dir(dest) / "catalog_v2.db").exists() or (sync / "catalog_v2.db").exists()
            or (sync / "catalog_backup.db").exists())


class Catalog:
    def __init__(self, dest):
        self.dest = Path(dest).expanduser()
        self.local = lib_dir(self.dest)
        self.sync_dir = self.dest / ".photokeeper"
        dbp = self.local / "catalog_v2.db"
        if not dbp.exists():
            for cand in (self.sync_dir / "catalog_v2.db", self.sync_dir / "catalog_backup.db"):
                if cand.exists():
                    try:
                        shutil.copy2(cand, dbp)
                    except Exception:
                        pass
                    break
        self.db = sqlite3.connect(str(dbp), check_same_thread=False)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS files(
                sha TEXT PRIMARY KEY, rel_path TEXT, orig_path TEXT,
                taken TEXT, taken_src TEXT, category TEXT, phash TEXT,
                size INTEGER, added TEXT, tagged INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS tags(
                sha TEXT, tag TEXT, score REAL);
            CREATE INDEX IF NOT EXISTS tags_sha ON tags(sha);
            CREATE INDEX IF NOT EXISTS tags_tag ON tags(tag);
            CREATE TABLE IF NOT EXISTS clusters(
                id INTEGER PRIMARY KEY, name TEXT, ignored INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS faces(
                id INTEGER PRIMARY KEY, sha TEXT, x INTEGER, y INTEGER, w INTEGER, h INTEGER,
                emb BLOB, cluster INTEGER);
            CREATE INDEX IF NOT EXISTS faces_sha ON faces(sha);
            CREATE INDEX IF NOT EXISTS faces_cluster ON faces(cluster);
            CREATE INDEX IF NOT EXISTS tags_sha ON tags(sha);
            CREATE TABLE IF NOT EXISTS trash(sha TEXT PRIMARY KEY, old_rel TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS photo_people(
                sha TEXT, cluster INTEGER, PRIMARY KEY(sha, cluster));
            CREATE TABLE IF NOT EXISTS reviewed(sha TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS batches(id INTEGER PRIMARY KEY, started TEXT, finished TEXT, n INTEGER, seen INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS renames(
                id INTEGER PRIMARY KEY, batch INTEGER, sha TEXT, old_rel TEXT, new_rel TEXT,
                reverted INTEGER DEFAULT 0, at TEXT);
        """)
        # A tag counts as a real match if you set it, or the AI is fairly sure (>= TAG_SHOW_MIN),
        # or it is the AI's best guess for that photo. Weak 2nd/3rd guesses are kept but not shown.
        self.db.execute(
            "CREATE TEMP VIEW IF NOT EXISTS shown_tags AS SELECT t.sha, t.tag, t.score, t.manual FROM tags t "
            "WHERE t.manual=1 OR t.score>=%s OR t.score=(SELECT MAX(t2.score) FROM tags t2 WHERE t2.sha=t.sha)"
            % TAG_SHOW_MIN)
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(files)")]
        if "converted" not in cols:
            self.db.execute("ALTER TABLE files ADD COLUMN converted INTEGER DEFAULT 0")
        if "faced" not in cols:
            self.db.execute("ALTER TABLE files ADD COLUMN faced INTEGER DEFAULT 0")
        for col, decl in (("placed", "INTEGER DEFAULT 0"), ("place", "TEXT"), ("lat", "REAL"), ("lon", "REAL")):
            if col not in cols:
                self.db.execute(f"ALTER TABLE files ADD COLUMN {col} {decl}")
        tcols = [r[1] for r in self.db.execute("PRAGMA table_info(tags)")]
        if "manual" not in tcols:
            self.db.execute("ALTER TABLE tags ADD COLUMN manual INTEGER DEFAULT 0")
        self.db.commit()

    # --- filing
    def has(self, key):
        return self.db.execute("SELECT 1 FROM files WHERE sha=?", (key,)).fetchone() is not None

    def add(self, key, rel, orig, taken, src, cat, phash, size):
        self.db.execute(
            "INSERT OR IGNORE INTO files(sha,rel_path,orig_path,taken,taken_src,category,"
            "phash,size,added) VALUES(?,?,?,?,?,?,?,?,?)",
            (key, rel, orig, taken.isoformat() if taken else "", src, cat,
             format(phash, "016x") if phash is not None else "", size,
             dt.datetime.now().isoformat(timespec="seconds")))

    def hashes(self):
        for key, ph in self.db.execute(
                "SELECT sha, phash FROM files WHERE phash != '' AND category != '_Review'"):
            yield key, int(ph, 16)

    def commit(self):
        self.db.commit()

    # --- tagging
    def untagged(self):
        return self.db.execute(
            "SELECT sha, orig_path, rel_path, category FROM files WHERE tagged=0 AND converted=0 "
            "ORDER BY taken DESC").fetchall()

    def set_tags(self, key, tags, flag=1):
        self.db.execute("DELETE FROM tags WHERE sha=? AND manual=0", (key,))
        have = {r[0] for r in self.db.execute("SELECT tag FROM tags WHERE sha=?", (key,))}
        for t, sc in tags:
            if t not in have:
                self.db.execute("INSERT INTO tags(sha,tag,score,manual) VALUES(?,?,?,0)",
                                (key, t, float(sc)))
        self.db.execute("UPDATE files SET tagged=? WHERE sha=?", (flag, key))

    # manual tag editing (your corrections always win over the AI)
    @staticmethod
    def clean_tag(t):
        return re.sub(r"\s+", " ", (t or "").strip().lower())[:40]

    def add_tag(self, ids, tag):
        tag = self.clean_tag(tag)
        if not tag:
            return 0
        n = 0
        for i in ids:
            if not self.db.execute("SELECT 1 FROM tags WHERE sha=? AND tag=?", (i, tag)).fetchone():
                self.db.execute("INSERT INTO tags(sha,tag,score,manual) VALUES(?,?,1.0,1)", (i, tag))
                n += 1
            else:       # an AI guess you confirm becomes your own tag
                self.db.execute("UPDATE tags SET manual=1 WHERE sha=? AND tag=?", (i, tag))
        self.db.commit()
        return n

    def unseen_batch(self):
        rows = self.db.execute("SELECT id, started FROM batches WHERE seen=0 ORDER BY started").fetchall()
        if not rows:
            return None
        since = rows[0][1]
        n = self.db.execute("SELECT COUNT(*) FROM files WHERE added >= ? AND converted=0 "
                            "AND category != '_Review'", (since,)).fetchone()[0]
        if not n:
            self.db.execute("UPDATE batches SET seen=1")
            self.db.commit()
            return None
        return {"since": since, "n": n, "ids": [r[0] for r in rows]}

    def add_batch(self, started):
        n = self.db.execute("SELECT COUNT(*) FROM files WHERE added >= ? AND converted=0 "
                            "AND category != '_Review'", (started,)).fetchone()[0]
        if n:
            self.db.execute("INSERT INTO batches(started, finished, n) VALUES(?,?,?)",
                            (started, dt.datetime.now().isoformat(timespec="seconds"), n))
        self.db.commit()
        return n

    def set_reviewed(self, ids, on=True):
        for i in ids:
            if on:
                self.db.execute("INSERT OR IGNORE INTO reviewed(sha) VALUES(?)", (i,))
            else:
                self.db.execute("DELETE FROM reviewed WHERE sha=?", (i,))
        self.db.commit()

    def remove_tag(self, ids, tag):
        tag = self.clean_tag(tag)
        for i in ids:
            self.db.execute("DELETE FROM tags WHERE sha=? AND tag=?", (i, tag))
        self.db.commit()

    def rename_tag(self, old, new):
        old, new = self.clean_tag(old), self.clean_tag(new)
        if not old or not new or old == new:
            return 0
        self.db.execute("DELETE FROM tags WHERE tag=? AND sha IN (SELECT sha FROM tags WHERE tag=?)",
                        (old, new))
        n = self.db.execute("UPDATE tags SET tag=?, manual=1 WHERE tag=?", (new, old)).rowcount
        self.db.commit()
        return n

    def delete_tag(self, tag):
        self.db.execute("DELETE FROM tags WHERE tag=?", (self.clean_tag(tag),))
        self.db.commit()

    def tag_counts(self):
        return [{"tag": t, "n": n} for t, n in self.db.execute(
            "SELECT t.tag, COUNT(*) c FROM shown_tags t JOIN files f ON f.sha=t.sha "
            "WHERE f.converted=0 GROUP BY t.tag ORDER BY c DESC")]

    # --- faces
    def unfaced(self):
        return self.db.execute(
            "SELECT sha, orig_path, rel_path, category FROM files WHERE faced=0 AND converted=0 "
            "AND category IN ('Photos', ?) ORDER BY taken DESC", (HEIC_FOLDER,)).fetchall()

    def set_faced(self, key, flag):
        self.db.execute("UPDATE files SET faced=? WHERE sha=?", (flag, key))

    def add_face(self, key, rect, emb_bytes, cluster):
        cur = self.db.execute(
            "INSERT INTO faces(sha,x,y,w,h,emb,cluster) VALUES(?,?,?,?,?,?,?)",
            (key, rect[0], rect[1], rect[2], rect[3], emb_bytes, cluster))
        return cur.lastrowid

    def new_cluster(self):
        return self.db.execute("INSERT INTO clusters(name, ignored) VALUES(NULL, 0)").lastrowid

    def merge_clusters(self, keep, drop):
        a = self.db.execute("SELECT name FROM clusters WHERE id=?", (keep,)).fetchone()
        b = self.db.execute("SELECT name FROM clusters WHERE id=?", (drop,)).fetchone()
        if a is not None and b is not None and not a[0] and b[0]:
            self.db.execute("UPDATE clusters SET name=? WHERE id=?", (b[0], keep))
        self.db.execute("UPDATE faces SET cluster=? WHERE cluster=?", (keep, drop))
        self.db.execute("UPDATE OR IGNORE photo_people SET cluster=? WHERE cluster=?", (keep, drop))
        self.db.execute("DELETE FROM photo_people WHERE cluster=?", (drop,))
        self.db.execute("DELETE FROM clusters WHERE id=?", (drop,))

    def name_clusters(self, ids, name):
        name = (name or "").strip()
        for cid in ids:
            self.db.execute("UPDATE clusters SET name=?, ignored=0 WHERE id=?",
                            (name or None, int(cid)))
        self.db.commit()

    def _cluster_for_name(self, name):
        """The named group to add faces to (the biggest one with this name), or a new one."""
        name = name.strip()
        r = self.db.execute(
            "SELECT c.id FROM clusters c LEFT JOIN faces f ON f.cluster=c.id "
            "WHERE LOWER(TRIM(c.name))=? AND c.ignored=0 GROUP BY c.id ORDER BY COUNT(f.id) DESC LIMIT 1",
            (name.lower(),)).fetchone()
        if r:
            return r[0]
        cid = self.new_cluster()
        self.db.execute("UPDATE clusters SET name=? WHERE id=?", (name, cid))
        return cid

    def photo_faces(self, sha):
        return [{"id": fid, "name": (nm or "") if not ign else "", "skipped": bool(ign)}
                for fid, nm, ign in self.db.execute(
                    "SELECT f.id, c.name, c.ignored FROM faces f LEFT JOIN clusters c ON c.id=f.cluster "
                    "WHERE f.sha=? ORDER BY f.x", (sha,))]

    def ignore_face(self, face_id):
        """An unknown person you do not want to name: stop asking about this face."""
        cid = self.new_cluster()
        self.db.execute("UPDATE clusters SET ignored=1 WHERE id=?", (cid,))
        self.db.execute("UPDATE faces SET cluster=? WHERE id=?", (cid, int(face_id)))
        self.db.commit()

    def people_of(self, sha):
        names = {f["name"] for f in self.photo_faces(sha) if f["name"]}
        names |= {r[0].strip() for r in self.db.execute(
            "SELECT c.name FROM photo_people pp JOIN clusters c ON c.id=pp.cluster "
            "WHERE pp.sha=? AND c.name IS NOT NULL AND c.name != '' AND c.ignored=0", (sha,))}
        return ", ".join(sorted(names, key=str.casefold))

    def add_person_to_photo(self, sha, name):
        """Mark someone as being in this photo even though no face was found for them."""
        name = (name or "").strip()
        if not name:
            return {"error": "Type a name first."}
        cid = self._cluster_for_name(name)
        have = self.db.execute(
            "SELECT 1 FROM faces WHERE sha=? AND cluster=?", (sha, cid)).fetchone()
        if not have:
            self.db.execute("INSERT OR IGNORE INTO photo_people(sha, cluster) VALUES(?,?)", (sha, cid))
        self.db.commit()
        return {"ok": True, "people": self.people_of(sha)}

    def assign_face(self, face_id, name):
        name = (name or "").strip()
        if not name:    # empty name = take the name off this face (back to "Who is this?")
            cid = self.new_cluster()
        else:
            cid = self._cluster_for_name(name)
        self.db.execute("UPDATE faces SET cluster=? WHERE id=?", (cid, int(face_id)))
        self.db.commit()
        return True

    def assign_photos(self, shas, name):
        """Put `name` on the faces in these photos. One face (or one still-unnamed face) is
        unambiguous; photos with several unnamed faces are skipped; photos with no face are named directly."""
        name = (name or "").strip()
        if not name:
            return {"error": "Type a name first."}
        cid = self._cluster_for_name(name)
        done = skipped = tagged = 0
        notag = []
        for sha in shas:
            faces = self.db.execute(
                "SELECT f.id, c.name FROM faces f LEFT JOIN clusters c ON c.id=f.cluster WHERE f.sha=?",
                (sha,)).fetchall()
            if not faces:   # no face box: name the photo itself
                self.db.execute("INSERT OR IGNORE INTO photo_people(sha, cluster) VALUES(?,?)", (sha, cid))
                done += 1
                continue
            if any((n or "").strip().lower() == name.lower() for _, n in faces):
                done += 1
                continue
            cand = faces if len(faces) == 1 else [f for f in faces if not (f[1] or "").strip()]
            if len(cand) == 1:
                self.db.execute("UPDATE faces SET cluster=? WHERE id=?", (cid, cand[0][0]))
            else:       # several faces and no way to tell which: name the photo, not a face
                self.db.execute("INSERT OR IGNORE INTO photo_people(sha, cluster) VALUES(?,?)", (sha, cid))
            done += 1
        self.db.commit()
        return {"named": done, "skipped": skipped, "tagged": 0}

    def unlink_person(self, sha, name, new_name=""):
        """This photo's faces tagged `name` are not that person: move them to a fresh group
        (optionally already named `new_name`)."""
        name = (name or "").strip().lower()
        ids = [r[0] for r in self.db.execute(
            "SELECT f.id FROM faces f JOIN clusters c ON c.id=f.cluster "
            "WHERE f.sha=? AND LOWER(TRIM(c.name))=?", (sha, name))]
        manual = self.db.execute(
            "DELETE FROM photo_people WHERE sha=? AND cluster IN "
            "(SELECT id FROM clusters WHERE LOWER(TRIM(name))=?)", (sha, name)).rowcount
        if not ids:
            if manual and (new_name or "").strip():
                self.add_person_to_photo(sha, new_name)
            self.db.commit()
            return manual
        cid = self.new_cluster()
        self.db.execute("UPDATE clusters SET name=? WHERE id=?", ((new_name or "").strip() or None, cid))
        self.db.executemany("UPDATE faces SET cluster=? WHERE id=?", [(cid, i) for i in ids])
        self.db.commit()
        return len(ids)

    def delete_face(self, face_id):
        self.db.execute("DELETE FROM faces WHERE id=?", (int(face_id),))
        self.db.commit()

    def group_photos(self, cid):
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT f.sha FROM faces f JOIN files fl ON fl.sha=f.sha AND fl.converted=0 "
            "WHERE f.cluster=?", (int(cid),))]

    def ignore_cluster(self, cid):
        self.db.execute("UPDATE clusters SET ignored=1, name=NULL WHERE id=?", (int(cid),))
        self.db.commit()

    def people_overview(self, min_faces=1):
        rows = self.db.execute(
            "SELECT c.id, c.name, COUNT(f.id), COUNT(DISTINCT f.sha) FROM clusters c "
            "JOIN faces f ON f.cluster=c.id JOIN files fl ON fl.sha=f.sha AND fl.converted=0 "
            "WHERE c.ignored=0 GROUP BY c.id").fetchall()
        # people marked on photos by hand (no face found for them) count toward their photos too
        manual = {r[0]: r[1] for r in self.db.execute(
            "SELECT pp.cluster, COUNT(DISTINCT pp.sha) FROM photo_people pp "
            "JOIN files fl ON fl.sha=pp.sha AND fl.converted=0 JOIN clusters c ON c.id=pp.cluster "
            "WHERE c.ignored=0 AND c.name IS NOT NULL AND c.name != '' "
            "AND NOT EXISTS (SELECT 1 FROM faces f WHERE f.sha=pp.sha AND f.cluster=pp.cluster) "
            "GROUP BY pp.cluster")}
        seen = {r[0] for r in rows}
        rows = [(cid, nm, nf, nph + manual.get(cid, 0)) for cid, nm, nf, nph in rows]
        for cid, n in manual.items():
            if cid not in seen:
                nm = self.db.execute("SELECT name FROM clusters WHERE id=?", (cid,)).fetchone()[0]
                rows.append((cid, nm, 0, n))

        def samples(cid):
            return [r[0] for r in self.db.execute(
                "SELECT f.id FROM faces f JOIN files fl ON fl.sha=f.sha AND fl.converted=0 "
                "WHERE f.cluster=? ORDER BY f.w DESC LIMIT 8", (cid,))]

        named, unnamed, small = {}, [], 0
        for cid, name, nfaces, nphotos in rows:
            if name:
                g = named.setdefault(name.strip().lower(), {"name": name.strip(), "clusters": [],
                                                           "faces": [], "photos": 0})
                g["clusters"].append(cid)
                g["photos"] += nphotos
                if len(g["faces"]) < 8:
                    g["faces"] += samples(cid)[:8 - len(g["faces"])]
            elif nfaces >= min_faces:
                unnamed.append({"id": cid, "n": nfaces, "photos": nphotos})
            else:
                small += 1
        unnamed.sort(key=lambda u: -u["n"])
        for u in unnamed[:300]:
            u["faces"] = samples(u["id"])[:6]
        faces_total = self.db.execute("SELECT COUNT(*) FROM faces").fetchone()[0]
        return {"named": sorted(named.values(), key=lambda g: g["name"].casefold()),
                "unnamed": unnamed[:300], "unnamed_total": len(unnamed), "small": small,
                "faces_total": faces_total}

    # --- searching
    def _people_by_sha(self):
        d = {}
        for sha, names in self.db.execute(
                "SELECT sha, GROUP_CONCAT(DISTINCT name) FROM ("
                "SELECT f.sha AS sha, c.name AS name FROM faces f JOIN clusters c ON c.id=f.cluster "
                "WHERE c.name IS NOT NULL AND c.name != '' AND c.ignored=0 "
                "UNION SELECT pp.sha, c.name FROM photo_people pp JOIN clusters c ON c.id=pp.cluster "
                "WHERE c.name IS NOT NULL AND c.name != '' AND c.ignored=0) GROUP BY sha"):
            d[sha] = names
        return d

    def photos(self, query="", person="", offset=0, limit=60, trashed=False, tag="", cat="", named="", since=""):
        words = [w for w in re.split(r"\s+", query.strip().lower()) if w]
        person = (person or "").strip().lower()
        people = self._people_by_sha()
        in_group = None
        if person.startswith("group:"):
            try:
                gid = int(person[6:])
            except ValueError:
                gid = -1
            in_group = {r[0] for r in self.db.execute("SELECT sha FROM faces WHERE cluster=?", (gid,))}
            person = ""
        place_by = {r[0]: r[1] for r in self.db.execute("SELECT sha, place FROM files WHERE place IS NOT NULL AND place != ''")}
        tags_by = {}
        for sha, t in self.db.execute("SELECT sha, tag FROM shown_tags ORDER BY manual DESC, score DESC"):
            tags_by.setdefault(sha, []).append(t)
        manual_by = {}
        for sha, t in self.db.execute("SELECT sha, tag FROM tags WHERE manual=1 ORDER BY score DESC"):
            manual_by.setdefault(sha, t)
        reviewed = {r[0] for r in self.db.execute("SELECT sha FROM reviewed")}
        want = (tag or "").strip().lower()
        rows = self.db.execute(
            "SELECT sha, rel_path, taken, category FROM files WHERE converted = ? "
            + ("AND category = ? " if cat else "") + ("AND added >= ? AND category != '_Review' " if since else "")
            + "ORDER BY taken DESC",
            tuple([2 if trashed else 0] + ([cat] if cat else []) + ([since] if since else []))).fetchall()
        out, total = [], 0
        for sha, rel, taken, cat in rows:
            tl = tags_by.get(sha, [])
            if want and want not in [x.lower() for x in tl]:
                continue
            tags = ", ".join(tl)
            ppl = people.get(sha, "")
            if in_group is not None and sha not in in_group:
                continue
            done = bool(ppl) or sha in manual_by or sha in reviewed
            if named == "1" and not done:
                continue
            if named == "0" and done:
                continue
            if named == "2" and not ppl:
                continue
            if person and person not in [p.strip().lower() for p in ppl.split(",")]:
                continue
            where = place_by.get(sha, "")
            hay = f"{rel} {taken or ''} {tags} {ppl} {where}".lower()
            if all(w in hay for w in words):
                if offset <= total < offset + limit:
                    out.append({"id": sha, "path": rel, "taken": (taken or "")[:10],
                                "category": cat, "tags": tags, "people": ppl, "place": where,
                                "done": 2 if ppl else (1 if (sha in manual_by or sha in reviewed) else 0),
                                "mark": manual_by.get(sha, "")})
                total += 1
        return {"items": out, "total": total}

    def search(self, query, limit=300):
        return self.photos(query, "", 0, limit)["items"]

    def stats(self):
        q = lambda sql, *a: self.db.execute(sql, a).fetchone()[0]
        return {
            "photos": q("SELECT COUNT(*) FROM files WHERE converted=0 AND category IN ('Photos', ?, 'Screenshots')", HEIC_FOLDER),
            "videos": q("SELECT COUNT(*) FROM files WHERE category='Videos'"),
            "review": q("SELECT COUNT(*) FROM files WHERE category='_Review' AND converted=0"),
            "heic_waiting": q("SELECT COUNT(*) FROM files WHERE category=? AND converted=0", HEIC_FOLDER),
            "tagged": q("SELECT COUNT(*) FROM files WHERE tagged=1 AND converted=0 AND category IN ('Photos', ?, 'Screenshots')", HEIC_FOLDER),
            "to_tag": q("SELECT COUNT(*) FROM files WHERE tagged=0 AND converted=0"),
            "faces": q("SELECT COUNT(*) FROM faces"),
            "trashed": q("SELECT COUNT(*) FROM trash"),
            "placed": q("SELECT COUNT(*) FROM files WHERE converted=0 AND place IS NOT NULL AND place != ''"),
            "kinds": [r[0] for r in self.db.execute("SELECT DISTINCT category FROM files WHERE converted=0 AND category IN ('Photos','Screenshots','Videos',?)", (HEIC_FOLDER,))],
            "kinds_trash": [r[0] for r in self.db.execute("SELECT DISTINCT category FROM files WHERE converted=2 AND category IN ('Photos','Screenshots','Videos',?)", (HEIC_FOLDER,))],
            "people": q("SELECT COUNT(DISTINCT lower(name)) FROM clusters WHERE name IS NOT NULL AND name != ''"),
            "gb": round((q("SELECT COALESCE(SUM(size),0) FROM files WHERE converted=0")) / 1e9, 2),
        }

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    def close(self, backup=True):
        self.db.commit()
        if backup:
            try:
                self.sync_dir.mkdir(parents=True, exist_ok=True)
                dst = sqlite3.connect(str(self.sync_dir / "catalog_backup.db"))
                self.db.backup(dst)
                dst.close()
            except Exception:
                pass
        self.db.close()


def unique_target(folder, name, reserved):
    """A path in `folder` that does not exist yet (adds _1, _2 ... if needed)."""
    stem, suffix = os.path.splitext(name)
    target = Path(folder) / name
    n = 1
    while target.exists() or str(target).lower() in reserved:
        target = Path(folder) / f"{stem}_{n}{suffix}"
        n += 1
    reserved.add(str(target).lower())
    return target


def fmt_eta(seconds):
    seconds = int(max(0, seconds))
    if seconds < 90:
        return f"{seconds} sec"
    if seconds < 5400:
        return f"{seconds // 60} min"
    return f"{seconds / 3600:.1f} hours"


# --------------------------------------------------------------------------
# the organizer
# --------------------------------------------------------------------------
class Organizer:
    CHUNK = 96

    def __init__(self, sources, dest, dry_run=False, on_progress=None, lookalikes=True,
                 naming="keep", heic_folder=True):
        self.naming = naming if naming in ("keep", "date", "datetime") else "keep"
        self.heic_folder = heic_folder
        self.sources = [Path(s).expanduser() for s in sources if str(s).strip()]
        self.dest = Path(dest).expanduser()
        self.dry = dry_run
        self.lookalikes = lookalikes
        self.cb = on_progress or (lambda **k: None)
        self.stop = False
        self.stats = dict(found=0, copied=0, exact_dups=0, near_dups=0,
                          skipped_small=0, errors=0, bytes=0, unreadable_heic=0,
                          Photos=0, Screenshots=0, Videos=0, **{HEIC_FOLDER: 0})
        self.problems = []
        self.library = self.dest / "Library"
        self.reserved = set()
        self.workers = max(4, min(16, (os.cpu_count() or 4) * 2))

    # -- discovery ---------------------------------------------------------
    def collect(self):
        files = []
        dest_resolved = None
        try:
            dest_resolved = self.dest.resolve()
        except Exception:
            pass
        for src in self.sources:
            if not src.is_dir():
                self.problems.append(f"Folder not found: {src}")
                continue
            for root, dirs, names in os.walk(src):
                rp = Path(root)
                try:
                    if dest_resolved and rp.resolve() == dest_resolved:
                        dirs[:] = []
                        continue
                except Exception:
                    pass
                dirs[:] = [d for d in dirs
                           if d.lower() not in SKIP_DIRS and not d.startswith(".")]
                for n in names:
                    if n.startswith(".") or n.startswith("._"):
                        continue
                    ext = os.path.splitext(n)[1].lower()
                    if ext in IMAGE_EXT or ext in VIDEO_EXT:
                        files.append(rp / n)
        files.sort(key=lambda p: str(p).lower())
        return files

    # -- classification ----------------------------------------------------
    @staticmethod
    def classify(path, ext, info):
        """Everything is a Photo unless it clearly is a video or a screenshot."""
        if ext in VIDEO_EXT:
            return "Videos"
        if not info.get("make") and (SCREENSHOT_NAME.search(path.name) or
                                     (ext == ".png" and info.get("size") in SCREEN_SIZES)):
            return "Screenshots"
        return "Photos"

    def final_name(self, orig, taken):
        if self.naming == "keep":
            return orig.name
        stamp = taken.strftime("%Y-%m-%d_%H%M%S")
        if self.naming == "date":
            return orig.name if orig.name.startswith(stamp) else f"{stamp}_{orig.name}"
        return f"{stamp}{orig.suffix.lower()}"

    def target_path(self, category, taken, orig):
        if category == "_Review":
            folder = self.library / "_Review" / "Possible Duplicates"
        else:
            folder = (self.library / category / f"{taken.year}" /
                      f"{taken.year}-{taken.month:02d} {MONTHS[taken.month - 1]}")
        return unique_target(folder, self.final_name(orig, taken), self.reserved)

    # -- worker-thread jobs ------------------------------------------------
    @staticmethod
    def _key_job(path):
        try:
            st = path.stat()
            if path.suffix.lower() in IMAGE_EXT and st.st_size < SMALL_IMAGE_BYTES:
                return path, st, "SMALL", None
            return path, st, quick_key(path, st.st_size), None
        except Exception as e:
            return path, None, None, e

    def _info_job(self, item):
        path, st, key = item
        ext = path.suffix.lower()
        if ext in IMAGE_EXT:
            info = inspect_image(path)
            if not self.lookalikes:
                info["phash"] = None
            return info
        return {"taken": None, "make": "", "size": None, "phash": None}

    @staticmethod
    def _copy_job(args):
        src, target, size = args
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
            if target.stat().st_size != size:
                raise IOError("copy size mismatch")
            return None
        except Exception as e:
            return e

    # -- main loop ---------------------------------------------------------
    def run(self):
        started = time.time()
        self.cb(phase="Looking for photos...", done=0, total=0, eta="")
        files = self.collect()
        total = len(files)
        self.stats["found"] = total
        if total == 0:
            self.problems.append("No photos or videos were found in the folders you chose.")
            return self.finish(started)

        if not self.dry:
            self.library.mkdir(parents=True, exist_ok=True)
        cat = Catalog(self.dest) if (not self.dry or catalog_exists(self.dest)) else None
        pending = set()
        buckets = [dict() for _ in range(4)]

        def bucket_add(key, ph):
            for i in range(4):
                buckets[i].setdefault((ph >> (16 * i)) & 0xFFFF, []).append((key, ph))

        def near_match(ph):
            for i in range(4):
                for key, other in buckets[i].get((ph >> (16 * i)) & 0xFFFF, ()):
                    if hamming(ph, other) <= 3:
                        return key
            return None

        if cat and self.lookalikes:
            for key, ph in cat.hashes():
                bucket_add(key, ph)

        done = 0
        with ThreadPoolExecutor(self.workers) as pool:
            for start in range(0, total, self.CHUNK):
                if self.stop:
                    self.problems.append("Stopped early by you. Run again to continue where it left off.")
                    break
                chunk = files[start:start + self.CHUNK]
                self.cb(phase="Sorting your photos...", done=done, total=total,
                        current=chunk[0].name, stats=dict(self.stats),
                        eta=fmt_eta((time.time() - started) / done * (total - done)) if done > 20 else "")

                # stage A: quick fingerprints (threads)
                fresh = []
                for path, st, key, err in pool.map(self._key_job, chunk):
                    if err is not None:
                        self.stats["errors"] += 1
                        if len(self.problems) < 200:
                            self.problems.append(f"{path}: {err}")
                    elif key == "SMALL":
                        self.stats["skipped_small"] += 1
                    elif key in pending or (cat and cat.has(key)):
                        self.stats["exact_dups"] += 1
                    else:
                        pending.add(key)
                        fresh.append((path, st, key))

                # stage B: read dates / camera info / look-alike hash (threads)
                infos = list(pool.map(self._info_job, fresh))

                # stage C: decide (one at a time so look-alike checks are consistent)
                jobs = []
                for (path, st, key), info in zip(fresh, infos):
                    ext = path.suffix.lower()
                    if ext in (".heic", ".heif") and not HAVE_HEIF:
                        self.stats["unreadable_heic"] += 1
                    taken, src = info["taken"], "photo date"
                    if not taken:
                        taken, src = date_from_name(path.name), "file name"
                    if not taken:
                        taken, src = dt.datetime.fromtimestamp(st.st_mtime), "file date"
                    category = self.classify(path, ext, info)
                    if self.heic_folder and ext in (".heic", ".heif") and category == "Photos":
                        category = HEIC_FOLDER
                    near = near_match(info["phash"]) if (
                        info["phash"] is not None and category != "Videos") else None
                    if near:
                        self.stats["near_dups"] += 1
                        store_cat = "_Review"
                    else:
                        store_cat = category
                        if info["phash"] is not None:
                            bucket_add(key, info["phash"])
                    target = self.target_path(store_cat, taken, path)
                    jobs.append((path, st, key, info, taken, src, category, store_cat, target))

                # stage D: copy (threads), then record
                if self.dry:
                    results = [None] * len(jobs)
                else:
                    results = list(pool.map(self._copy_job,
                                            [(j[0], j[8], j[1].st_size) for j in jobs]))
                for job, err in zip(jobs, results):
                    path, st, key, info, taken, src, category, store_cat, target = job
                    if err is not None:
                        self.stats["errors"] += 1
                        pending.discard(key)
                        if len(self.problems) < 200:
                            self.problems.append(f"{path}: {err}")
                        continue
                    if not self.dry:
                        cat.add(key, str(target.relative_to(self.dest)), str(path), taken,
                                src, store_cat, info["phash"], st.st_size)
                    self.stats["copied"] += 1
                    self.stats["bytes"] += st.st_size
                    if store_cat != "_Review":
                        self.stats[category] += 1
                if cat and not self.dry:
                    cat.commit()
                done += len(chunk)

        if cat:
            cat.close(backup=not self.dry)
        self.cb(phase="Done", done=total if not self.stop else done, total=total,
                stats=dict(self.stats), eta="")
        return self.finish(started)

    def finish(self, started):
        s = self.stats
        mode = "PREVIEW (nothing was copied)" if self.dry else "FINISHED"
        lines = [
            f"{APP_NAME} {mode}  -  {dt.datetime.now():%Y-%m-%d %H:%M}",
            f"Library: {self.library}",
            "",
            f"Photos/videos found:        {s['found']}",
            f"{'Would copy' if self.dry else 'Copied'} into library:   {s['copied']}  "
            f"({s['bytes'] / 1e9:.2f} GB)",
            f"   Photos:                  {s['Photos']}",
            f"   Screenshots:             {s['Screenshots']}",
            f"   Videos:                  {s['Videos']}",
            f"   iPhone HEIC (own folder): {s[HEIC_FOLDER]}",
            f"Exact duplicates skipped:   {s['exact_dups']}",
            f"Look-alikes for review:     {s['near_dups']}  (in Library/_Review)",
            f"Tiny images skipped:        {s['skipped_small']}  (icons/thumbnails)",
            f"Problems:                   {s['errors']}",
            f"Time: {fmt_eta(time.time() - started)}",
        ]
        if s["unreadable_heic"]:
            lines += ["", f"WARNING: {s['unreadable_heic']} iPhone (HEIC) photos could not be read for "
                          "dates because the 'pillow-heif' add-on is not installed. They were still "
                          "copied, but filed by file date. Close Memory Lane and run "
                          "Memory Lane Setup again to install it."]
        if self.problems:
            lines += ["", "Notes:"] + ["  " + p for p in self.problems[:50]]
        report = "\n".join(lines)
        if not self.dry:
            try:
                (self.dest / ".photokeeper").mkdir(parents=True, exist_ok=True)
                (self.dest / ".photokeeper" / "last_report.txt").write_text(report, encoding="utf-8")
            except Exception:
                pass
        return report


# --------------------------------------------------------------------------
# HEIC -> JPEG converter (separate button; keeps the HEIC originals)
# --------------------------------------------------------------------------
class HeicConverter:
    CHUNK = 24

    def __init__(self, dest, on_progress=None, quality=92):
        self.dest = Path(dest).expanduser()
        self.cb = on_progress or (lambda **k: None)
        self.quality = quality
        self.stop = False
        self.converted = 0
        self.failed = []
        self.reserved = set()

    def _convert(self, job):
        src, target = job
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(src) as im:
                icc = im.info.get("icc_profile")
                exif = None
                try:
                    exif = im.getexif()
                    for ifd in (0x8769, 0x8825):      # load location/camera blocks so they are saved too
                        try:
                            exif.get_ifd(ifd)
                        except Exception:
                            pass
                    if 0x0112 in exif:
                        exif[0x0112] = 1      # pixels are already upright
                except Exception:
                    exif = None
                rgb = im.convert("RGB")
                kw = {"quality": self.quality, "optimize": True}
                if icc:
                    kw["icc_profile"] = icc
                try:
                    rgb.save(target, "JPEG", exif=exif if exif is not None else b"", **kw)
                except Exception:
                    rgb.save(target, "JPEG", **kw)      # photo matters more than extra metadata
            if target.stat().st_size < 1000:
                raise IOError("converted file looks empty")
            st = src.stat()
            os.utime(target, (st.st_atime, st.st_mtime))
            return None
        except Exception as e:
            try:
                if target.exists():
                    target.unlink()      # remove only the half-written JPEG we just made
            except Exception:
                pass
            return e

    def run(self):
        started = time.time()
        if not HAVE_PIL or not HAVE_HEIF:
            return ("The HEIC add-on ('pillow-heif') is not installed, so iPhone photos can't be "
                    "converted yet. Close Memory Lane and run Memory Lane Setup again. "
                    "If it still fails, send me a photo of the message.")
        cat = Catalog(self.dest)
        rows = cat.db.execute(
            "SELECT sha, rel_path, taken, taken_src, phash, size, tagged FROM files "
            "WHERE category=? AND converted=0 ORDER BY taken", (HEIC_FOLDER,)).fetchall()
        total = len(rows)
        if total == 0:
            cat.close()
            return "No iPhone (HEIC) photos are waiting to be converted."
        library = self.dest / "Library"
        done = 0
        with ThreadPoolExecutor(max(2, min(6, os.cpu_count() or 2))) as pool:
            for start in range(0, total, self.CHUNK):
                if self.stop:
                    break
                chunk = rows[start:start + self.CHUNK]
                self.cb(phase="Converting to JPEG...", done=done, total=total,
                        current=Path(chunk[0][1]).name,
                        eta=fmt_eta((time.time() - started) / done * (total - done)) if done > 10 else "")
                jobs = []
                for key, rel, taken, tsrc, ph, size, tagged in chunk:
                    src = self.dest / rel
                    parts = Path(rel).parts                    # Library / iPhone HEIC / 2021 / 2021-06 June / x.heic
                    sub = Path(*parts[2:-1]) if len(parts) > 3 else Path()
                    name = Path(rel).stem + ".jpg"
                    jobs.append((src, unique_target(library / "Photos" / sub, name, self.reserved)))
                results = list(pool.map(self._convert, jobs))
                for row, (src, target), err in zip(chunk, jobs, results):
                    key, rel, taken, tsrc, ph, size, tagged = row
                    if err is not None:
                        self.failed.append(f"{rel}: {err}")
                        continue
                    jkey = "jpg:" + key
                    cat.db.execute(
                        "INSERT OR IGNORE INTO files(sha,rel_path,orig_path,taken,taken_src,category,"
                        "phash,size,added,tagged) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (jkey, str(target.relative_to(self.dest)), str(src), taken, tsrc, "Photos",
                         ph, target.stat().st_size, dt.datetime.now().isoformat(timespec="seconds"),
                         tagged))
                    # tags move to the JPEG so each photo shows up once in search
                    cat.db.execute("UPDATE tags SET sha=? WHERE sha=?", (jkey, key))
                    cat.db.execute("UPDATE files SET converted=1 WHERE sha=?", (key,))
                    self.converted += 1
                cat.commit()
                done += len(chunk)
        left = cat.db.execute("SELECT COUNT(*) FROM files WHERE category=? AND converted=0",
                              (HEIC_FOLDER,)).fetchone()[0]
        cat.close()
        self.cb(phase="Done", done=done, total=total, eta="")
        msg = (f"Conversion {'paused' if self.stop else 'finished'}: {self.converted} photos converted "
               f"to JPEG (in Library\\Photos). Your HEIC originals are still in Library\\{HEIC_FOLDER}; "
               f"delete that folder yourself once you've checked the JPEGs.\n"
               f"{len(self.failed)} could not be converted, {left} still to do.  "
               f"Time: {fmt_eta(time.time() - started)}")
        if self.failed:
            msg += "\n\nCould not convert:\n  " + "\n  ".join(self.failed[:30])
        return msg


# --------------------------------------------------------------------------
# smart tagging (AI that runs on THIS computer; photos never leave it)
# --------------------------------------------------------------------------
TAG_LABELS = [
    ("birthday", "a birthday party with cake or balloons"),
    ("wedding", "a wedding with bride and groom"),
    ("christmas", "christmas tree, lights and decorations"),
    ("halloween", "halloween costumes or pumpkins"),
    ("graduation", "a graduation ceremony with caps and gowns"),
    ("family", "a posed group photo of a family"),
    ("selfie", "a selfie of one or two people"),
    ("baby", "a baby"),
    ("kids", "children playing"),
    ("portrait", "a close-up portrait of a person"),
    ("beach", "a beach with sand and sea"),
    ("water", "a lake, river or ocean"),
    ("mountains", "mountains and scenic overlook"),
    ("hiking", "a forest or hiking trail"),
    ("snow", "a snowy winter scene"),
    ("sunset", "a sunset or sunrise sky"),
    ("flowers", "a garden or flowers"),
    ("city", "a city street or skyline"),
    ("landmark", "a famous landmark or monument"),
    ("pool", "a swimming pool"),
    ("camping", "a campsite with tents or campfire"),
    ("farm", "a farm or countryside with fields"),
    ("fireworks", "fireworks at night"),
    ("dog", "a dog"),
    ("cat", "a cat"),
    ("wildlife", "wild animals or birds"),
    ("food", "a plate of food or a meal"),
    ("restaurant", "inside a restaurant or cafe"),
    ("car", "a car or truck"),
    ("boat", "a boat or ship"),
    ("airplane", "an airplane or airport"),
    ("home", "inside a house, rooms and furniture"),
    ("church", "a church or place of worship"),
    ("sports", "a sports game or athletes"),
    ("concert", "a concert or stage performance"),
    ("mining", "an active industrial mine with haul trucks, drill rigs, conveyors or an underground mine tunnel"),
    ("national-park", "a national park landscape with dramatic scenery"),
    ("geyser", "a geyser, hot spring or steaming thermal pool"),
    ("waterfall", "a waterfall"),
    ("canyon", "a canyon, cliffs or red rock formations"),
    ("desert", "a desert landscape"),
    ("road-trip", "a scenic road or highway through the countryside"),
    ("bison", "bison, elk or deer"),
    ("document", "a photo of a paper document or letter"),
    ("receipt", "a store receipt"),
    ("slide", "a presentation slide or whiteboard"),
    ("sign", "a sign or poster with text"),
    ("map", "a map"),
    ("artwork", "a painting or artwork"),
    ("screen", "a photo of a computer or phone screen"),
    ("meme", "an internet meme or graphic with text"),
    ("old-photo", "an old faded scanned photograph"),
]


# Labels that are easy to confuse need a higher confidence before they are used.
TAG_MIN = {"mining": 0.40, "document": 0.25, "receipt": 0.25, "meme": 0.30, "sign": 0.25,
           "screen": 0.25, "old-photo": 0.25, "map": 0.25}


def active_labels(cfg=None):
    """Built-in labels you have not turned off, plus the ones you added."""
    cfg = cfg if cfg is not None else load_config()
    off = set(cfg.get("disabled_labels", []))
    labels = [(t, p) for t, p in TAG_LABELS if t not in off]
    have = {t for t, _ in labels}
    for c in cfg.get("custom_labels", []):
        t = Catalog.clean_tag(c.get("tag", ""))
        p = (c.get("prompt") or "").strip()
        if t and p and t not in have:
            labels.append((t, p))
            have.add(t)
    return labels


class FastEmbedBackend:
    """Real AI: OpenAI's CLIP model (small version) via fastembed + ONNX. Downloads
    about 350 MB the first time, then works offline."""

    def __init__(self):
        from fastembed import ImageEmbedding, TextEmbedding  # may raise ImportError
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        self.img = ImageEmbedding("Qdrant/clip-ViT-B-32-vision", cache_dir=str(MODEL_DIR))
        self.txt = TextEmbedding("Qdrant/clip-ViT-B-32-text", cache_dir=str(MODEL_DIR))

    def embed_images(self, images):
        return list(self.img.embed(images, batch_size=len(images)))

    def embed_texts(self, texts):
        return list(self.txt.embed(texts))


class FakeBackend:
    """Deterministic stand-in used only for automated tests."""

    def embed_images(self, images):
        import random
        out = []
        for im in images:
            r = random.Random(sum(im.resize((2, 2)).convert("RGB").tobytes()))
            out.append([r.uniform(-1, 1) for _ in range(32)])
        return out

    def embed_texts(self, texts):
        import random
        return [[random.Random(t).uniform(-1, 1) for _ in range(32)] for t in texts]


def _normalize(rows):
    out = []
    for v in rows:
        v = [float(x) for x in v]
        n = sum(x * x for x in v) ** 0.5 or 1.0
        out.append([x / n for x in v])
    return out


class Tagger:
    BATCH = 32

    def __init__(self, dest, on_progress=None, backend=None, redo=False):
        self.dest = Path(dest).expanduser()
        self.cb = on_progress or (lambda **k: None)
        self.backend = backend
        self.redo = redo
        self.labels = active_labels()
        self.stop = False
        self.tagged = 0
        self.failed = 0

    @staticmethod
    def _thumb(path):
        try:
            with Image.open(path) as im:
                if im.format == "JPEG":
                    im.draft("RGB", (448, 448))
                im = im.convert("RGB")
                im.thumbnail((448, 448))
                return im.copy()
        except Exception:
            return None

    def run(self):
        started = time.time()
        cat = Catalog(self.dest)
        if self.redo:      # start the AI tags over; your own corrections are kept
            cat.db.execute("DELETE FROM tags WHERE manual=0")
            cat.db.execute("UPDATE files SET tagged=0 WHERE converted=0")
            cat.commit()
        todo = cat.untagged()
        total = len(todo)
        if total == 0:
            cat.close()
            return "Everything is already tagged."
        if not self.labels:
            cat.close()
            return "All tag labels are turned off. Turn some on in the Tools page first."
        self.cb(phase="Loading the AI model (first time downloads about 350 MB)...",
                done=0, total=total, eta="")
        try:
            if self.backend is None:
                self.backend = (FakeBackend() if os.environ.get("PHOTOKEEPER_FAKE_AI")
                                else FastEmbedBackend())
            text_vecs = _normalize(self.backend.embed_texts(
                [f"a photo of {p}" for _, p in self.labels]))
        except ImportError:
            cat.close()
            return ("Smart tagging needs an add-on that is not installed.\n"
                    "Close Memory Lane and run Memory Lane Setup again; if it still fails, look for any "
                    "message about 'fastembed'. If it can't install, tell me what it says.")
        except Exception as e:
            cat.close()
            return (f"The AI model could not be loaded: {e}\n"
                    "The first run needs an internet connection to download it (about 350 MB).")

        import math
        with ThreadPoolExecutor(max(2, min(8, os.cpu_count() or 2))) as pool:
            for start in range(0, total, self.BATCH):
                if self.stop:
                    break
                batch = todo[start:start + self.BATCH]
                done = start
                self.cb(phase="Adding smart tags...", done=done, total=total,
                        current=Path(batch[0][2]).name,
                        eta=fmt_eta((time.time() - started) / done * (total - done)) if done > 40 else "")
                paths = []
                for key, orig, rel, category in batch:
                    if category == "Videos":
                        paths.append(None)
                    elif orig and os.path.exists(orig):
                        paths.append(orig)       # prefer the original: avoids iCloud downloads
                    else:
                        paths.append(str(self.dest / rel))
                thumbs = list(pool.map(lambda p: self._thumb(p) if p else None, paths))
                ok = [(b, t) for b, t in zip(batch, thumbs) if t is not None]
                for b, t in zip(batch, thumbs):
                    if t is None:
                        cat.set_tags(b[0], [], flag=2)     # nothing to tag (video/unreadable)
                        self.failed += 1
                if ok:
                    try:
                        vecs = _normalize(self.backend.embed_images([t for _, t in ok]))
                    except Exception as e:
                        cat.close()
                        return f"The AI stopped with an error: {e}"
                    for (b, _), v in zip(ok, vecs):
                        logits = [100.0 * sum(a * c for a, c in zip(v, tv)) for tv in text_vecs]
                        m = max(logits)
                        exps = [math.exp(x - m) for x in logits]
                        z = sum(exps)
                        probs = sorted(((e / z, i) for i, e in enumerate(exps)), reverse=True)
                        chosen = [(self.labels[i][0], p) for p, i in probs[:3]
                                  if p >= max(0.10, TAG_MIN.get(self.labels[i][0], 0))]
                        if not chosen and probs[0][0] >= max(0.10, TAG_MIN.get(self.labels[probs[0][1]][0], 0)):
                            chosen = [(self.labels[probs[0][1]][0], probs[0][0])]
                        cat.set_tags(b[0], chosen)
                        self.tagged += 1
                cat.commit()
        left = len(cat.untagged())
        cat.close()
        self.cb(phase="Done", done=total - left, total=total, eta="")
        msg = (f"Smart tagging {'paused' if self.stop else 'finished'}: {self.tagged} photos tagged, "
               f"{self.failed} had nothing to tag (videos or unreadable), "
               f"{left} still to do.  Time: {fmt_eta(time.time() - started)}")
        if self.stop and left:
            msg += "\nRun it again to continue where it left off."
        return msg


# --------------------------------------------------------------------------
# renaming photo files inside the library (with undo)
# --------------------------------------------------------------------------
def safe_stem(text):
    """Make text safe to use as a Windows/iCloud file name (without the extension)."""
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", text or "")
    t = re.sub(r"\s+", " ", t).strip(" .")
    return t[:100]


def rename_photos(dest, ids, base, style):
    """style: 'single' (exact new name), 'number' (Name 001), 'datenumber' (2019-07-04 Name 001).
    Only renames inside the same folder. Nothing is overwritten."""
    dest = Path(dest).expanduser()
    stem = safe_stem(base)
    if not stem:
        return {"error": "Please type a name."}
    if style not in ("single", "number", "datenumber"):
        return {"error": "Unknown naming style."}
    cat = Catalog(dest)
    try:
        rows = []
        for i in ids:
            r = cat.db.execute("SELECT sha, rel_path, taken FROM files WHERE sha=? AND converted=0",
                               (i,)).fetchone()
            if r:
                rows.append(r)
        if not rows:
            return {"error": "Those photos were not found."}
        if style == "single" and len(rows) > 1:
            return {"error": "Use a numbered style to rename several photos at once."}
        rows.sort(key=lambda r: (r[2] or "", r[1]))
        width = max(3, len(str(len(rows))))
        batch = (cat.db.execute("SELECT COALESCE(MAX(batch),0) FROM renames").fetchone()[0] or 0) + 1
        reserved, done, failed, example = set(), 0, 0, ""
        for n, (sha, rel, taken) in enumerate(rows, 1):
            src = dest / rel
            if not src.exists():
                failed += 1
                continue
            if style == "single":
                name = stem
            elif style == "number":
                name = f"{stem} {n:0{width}d}"
            else:
                name = f"{(taken or 'undated')[:10]} {stem} {n:0{width}d}"
            target = unique_target(src.parent, name + src.suffix, reserved)
            if target.name.lower() == src.name.lower():
                continue
            try:
                os.rename(src, target)
            except Exception:
                failed += 1
                continue
            new_rel = str(target.relative_to(dest))
            cat.db.execute("UPDATE files SET rel_path=? WHERE sha=?", (new_rel, sha))
            cat.db.execute("INSERT INTO renames(batch,sha,old_rel,new_rel,at) VALUES(?,?,?,?,?)",
                           (batch, sha, rel, new_rel, dt.datetime.now().isoformat(timespec="seconds")))
            done += 1
            example = example or target.name
        cat.commit()
        return {"renamed": done, "failed": failed, "batch": batch, "example": example,
                "path": (str(Path(rows[0][1]).parent / example) if (style == "single" and example) else "")}
    finally:
        cat.close()


def undo_last_rename(dest):
    dest = Path(dest).expanduser()
    cat = Catalog(dest)
    try:
        b = cat.db.execute("SELECT MAX(batch) FROM renames WHERE reverted=0").fetchone()[0]
        if not b:
            return {"error": "There is no rename to undo."}
        restored = failed = 0
        for rid, sha, old, new in cat.db.execute(
                "SELECT id, sha, old_rel, new_rel FROM renames WHERE batch=? AND reverted=0", (b,)).fetchall():
            src, dst = dest / new, dest / old
            if src.exists() and not dst.exists():
                try:
                    os.rename(src, dst)
                    cat.db.execute("UPDATE files SET rel_path=? WHERE sha=?", (old, sha))
                    parts = Path(old).parts
                    if len(parts) > 2 and parts[0] == "Library" and parts[1] in KINDS:
                        cat.db.execute("UPDATE files SET category=? WHERE sha=?", (parts[1], sha))
                    cat.db.execute("UPDATE renames SET reverted=1 WHERE id=?", (rid,))
                    restored += 1
                    continue
                except Exception:
                    pass
            failed += 1
        cat.commit()
        return {"restored": restored, "failed": failed}
    finally:
        cat.close()


def move_to_folder(dest, ids, name="", mode="name", add_tag=True):
    """Move photos into a named sub-folder of their own top folder (Library/Screenshots/Receipts ...).
    mode 'name': every photo goes to `name`; 'bytag': a folder per photo's main tag.
    Logged like a rename, so Tools > Undo puts them back."""
    dest = Path(dest).expanduser()
    stem = safe_stem(name)
    if mode == "name" and not stem:
        return {"error": "Please type a folder name."}
    cat = Catalog(dest)
    try:
        tags_by = {}
        if mode == "bytag":
            for sha, t in cat.db.execute("SELECT sha, tag FROM shown_tags ORDER BY manual DESC, score DESC"):
                tags_by.setdefault(sha, t)
        batch = (cat.db.execute("SELECT COALESCE(MAX(batch),0) FROM renames").fetchone()[0] or 0) + 1
        moved = failed = skipped = 0
        reserved, folders = set(), set()
        for i in ids:
            r = cat.db.execute("SELECT rel_path, category FROM files WHERE sha=? AND converted=0", (i,)).fetchone()
            if not r or r[1] == "_Review":
                skipped += 1
                continue
            rel = r[0]
            parts = Path(rel).parts
            top = parts[1] if len(parts) > 2 and parts[0] == "Library" else "Photos"
            sub = stem if mode == "name" else safe_stem(tags_by.get(i, "")) or "Untagged"
            src = dest / rel
            if not src.exists():
                failed += 1
                continue
            folder = dest / "Library" / top / sub
            if src.parent == folder:
                continue
            folder.mkdir(parents=True, exist_ok=True)
            target = unique_target(folder, src.name, reserved)
            try:
                os.rename(src, target)
            except Exception:
                failed += 1
                continue
            new_rel = str(target.relative_to(dest))
            cat.db.execute("UPDATE files SET rel_path=? WHERE sha=?", (new_rel, i))
            cat.db.execute("INSERT INTO renames(batch,sha,old_rel,new_rel,at) VALUES(?,?,?,?,?)",
                           (batch, i, rel, new_rel, dt.datetime.now().isoformat(timespec="seconds")))
            if add_tag and mode == "name":
                cat.add_tag([i], stem.lower())
            folders.add(sub)
            moved += 1
        cat.commit()
        return {"moved": moved, "failed": failed, "skipped": skipped, "folders": sorted(folders)[:20],
                "folder_count": len(folders)}
    finally:
        cat.close()


def change_kind(dest, ids, kind):
    """Move photos to another kind (e.g. Screenshots -> Photos) and file them by date like the rest.
    Logged like a rename, so Tools > Undo puts them back."""
    dest = Path(dest).expanduser()
    if kind not in ("Photos", "Screenshots"):
        return {"error": "Unknown kind."}
    cat = Catalog(dest)
    try:
        batch = (cat.db.execute("SELECT COALESCE(MAX(batch),0) FROM renames").fetchone()[0] or 0) + 1
        moved = failed = 0
        reserved = set()
        for i in ids:
            r = cat.db.execute("SELECT rel_path, taken, category FROM files WHERE sha=? AND converted=0",
                               (i,)).fetchone()
            if not r or r[2] in ("_Review", "Videos"):
                continue
            rel, taken, old_kind = r
            src = dest / rel
            target_kind = HEIC_FOLDER if (kind == "Photos" and src.suffix.lower() in (".heic", ".heif")) else kind
            if old_kind == target_kind or not src.exists():
                failed += 0 if src.exists() else 1
                continue
            try:
                d = dt.datetime.fromisoformat(taken) if taken else dt.datetime.fromtimestamp(src.stat().st_mtime)
            except ValueError:
                d = dt.datetime.fromtimestamp(src.stat().st_mtime)
            folder = dest / "Library" / target_kind / f"{d.year}" / f"{d.year}-{d.month:02d} {MONTHS[d.month - 1]}"
            try:
                folder.mkdir(parents=True, exist_ok=True)
                target = unique_target(folder, src.name, reserved)
                os.rename(src, target)
            except Exception:
                failed += 1
                continue
            new_rel = str(target.relative_to(dest))
            cat.db.execute("UPDATE files SET rel_path=?, category=? WHERE sha=?", (new_rel, target_kind, i))
            cat.db.execute("INSERT INTO renames(batch,sha,old_rel,new_rel,at) VALUES(?,?,?,?,?)",
                           (batch, i, rel, new_rel, dt.datetime.now().isoformat(timespec="seconds")))
            moved += 1
        cat.commit()
        return {"moved": moved, "failed": failed}
    finally:
        cat.close()


def sub_folders(dest):
    """Folder names you made (not the year folders) under Library/<top>."""
    out = set()
    lib = Path(dest).expanduser() / "Library"
    try:
        for top in lib.iterdir():
            if top.is_dir() and not top.name.startswith("_"):
                for d in top.iterdir():
                    if d.is_dir() and not re.fullmatch(r"\d{4}", d.name):
                        out.add(d.name)
    except OSError:
        pass
    return sorted(out, key=str.casefold)


# --------------------------------------------------------------------------
# deleting photos from the library (goes to a Trash folder first; originals untouched)
# --------------------------------------------------------------------------
TRASH_DIR = Path("Library") / "_Trash"


def trash_photos(dest, ids):
    dest = Path(dest).expanduser()
    cat = Catalog(dest)
    try:
        tdir = dest / TRASH_DIR
        tdir.mkdir(parents=True, exist_ok=True)
        moved = failed = 0
        reserved = set()
        for i in ids:
            r = cat.db.execute("SELECT rel_path FROM files WHERE sha=? AND converted=0", (i,)).fetchone()
            if not r:
                continue
            src = dest / r[0]
            if not src.exists():
                failed += 1
                continue
            target = unique_target(tdir, src.name, reserved)
            try:
                os.rename(src, target)
            except Exception:
                failed += 1
                continue
            cat.db.execute("UPDATE files SET rel_path=?, converted=2 WHERE sha=?",
                           (str(target.relative_to(dest)), i))
            cat.db.execute("INSERT OR REPLACE INTO trash(sha, old_rel, at) VALUES(?,?,?)",
                           (i, r[0], dt.datetime.now().isoformat(timespec="seconds")))
            moved += 1
        cat.commit()
        return {"moved": moved, "failed": failed}
    finally:
        cat.close()


def restore_trash(dest, ids=None):
    dest = Path(dest).expanduser()
    cat = Catalog(dest)
    try:
        if ids is None:
            ids = [r[0] for r in cat.db.execute("SELECT sha FROM trash")]
        restored = failed = 0
        reserved = set()
        for i in ids:
            r = cat.db.execute("SELECT t.old_rel, f.rel_path FROM trash t JOIN files f ON f.sha=t.sha "
                               "WHERE t.sha=? AND f.converted=2", (i,)).fetchone()
            if not r:
                continue
            old, cur = Path(r[0]), dest / r[1]
            if not cur.exists():
                failed += 1
                continue
            try:
                (dest / old.parent).mkdir(parents=True, exist_ok=True)
                target = unique_target(dest / old.parent, old.name, reserved)
                os.rename(cur, target)
            except Exception:
                failed += 1
                continue
            cat.db.execute("UPDATE files SET rel_path=?, converted=0 WHERE sha=?",
                           (str(target.relative_to(dest)), i))
            cat.db.execute("DELETE FROM trash WHERE sha=?", (i,))
            restored += 1
        cat.commit()
        return {"restored": restored, "failed": failed}
    finally:
        cat.close()


def empty_trash(dest):
    """Permanently delete the files in the Trash folder. The catalog remembers them so a later
    'Organize' does not bring them back."""
    dest = Path(dest).expanduser()
    cat = Catalog(dest)
    try:
        removed = failed = 0
        for sha, rel in cat.db.execute("SELECT f.sha, f.rel_path FROM files f JOIN trash t ON t.sha=f.sha "
                                       "WHERE f.converted=2").fetchall():
            p = dest / rel
            try:
                if p.exists() and TRASH_DIR.name in p.parts:
                    p.unlink()
                cat.db.execute("DELETE FROM trash WHERE sha=?", (sha,))
                removed += 1
            except Exception:
                failed += 1
        cat.commit()
        return {"removed": removed, "failed": failed}
    finally:
        cat.close()


# --------------------------------------------------------------------------
# look-alike review: compare each set-aside photo with the one it resembles
# --------------------------------------------------------------------------
def _photo_facts(dest, row):
    """row = (sha, rel_path, taken, size, phash). Adds pixel size for display."""
    sha, rel, taken, size, ph = row
    w = h = 0
    if HAVE_PIL:
        try:
            with Image.open(Path(dest) / rel) as im:
                w, h = im.size
        except Exception:
            pass
    return {"id": sha, "path": rel, "name": Path(rel).name, "taken": (taken or "")[:10],
            "size": size or 0, "w": w, "h": h}


def review_pair(dest, offset=0):
    dest = Path(dest).expanduser()
    cat = Catalog(dest)
    try:
        rows = cat.db.execute(
            "SELECT sha, rel_path, taken, size, phash FROM files WHERE category='_Review' AND converted=0 "
            "ORDER BY taken, rel_path").fetchall()
        total = len(rows)
        if not total:
            return {"total": 0}
        offset = max(0, min(int(offset), total - 1))
        row = rows[offset]
        out = {"total": total, "offset": offset, "copy": _photo_facts(dest, row), "match": None}
        if row[4]:
            mine = int(row[4], 16)
            best, bd = None, 99
            for r in cat.db.execute(
                    "SELECT sha, rel_path, taken, size, phash FROM files WHERE phash!='' AND converted=0 "
                    "AND category!='_Review'"):
                d = hamming(mine, int(r[4], 16))
                if d < bd:
                    best, bd = r, d
                    if d == 0:
                        break
            if best is not None and bd <= 10:
                out["match"] = _photo_facts(dest, best)
                out["distance"] = bd
        return out
    finally:
        cat.close(backup=False)


def _promote(cat, dest, sha, category=None):
    """Move a set-aside photo into the normal library folders (by its date)."""
    r = cat.db.execute("SELECT rel_path, taken FROM files WHERE sha=? AND category='_Review' AND converted=0",
                       (sha,)).fetchone()
    if not r:
        return False
    src = dest / r[0]
    if not src.exists():
        return False
    try:
        d = dt.datetime.fromisoformat(r[1]) if r[1] else dt.datetime.fromtimestamp(src.stat().st_mtime)
    except ValueError:
        d = dt.datetime.fromtimestamp(src.stat().st_mtime)
    category = category if category and category not in ("_Review", "Videos") else "Photos"
    folder = dest / "Library" / category / f"{d.year}" / f"{d.year}-{d.month:02d} {MONTHS[d.month - 1]}"
    folder.mkdir(parents=True, exist_ok=True)
    target = unique_target(folder, src.name, set())
    os.rename(src, target)
    cat.db.execute("UPDATE files SET rel_path=?, category=? WHERE sha=?",
                   (str(target.relative_to(dest)), category, sha))
    return True


def review_act(dest, action, sha="", match="", all_ids=False):
    dest = Path(dest).expanduser()
    if action == "delete_copy":
        return trash_photos(dest, [sha])
    if action == "delete_both":
        return trash_photos(dest, [i for i in (sha, match) if i])
    cat = Catalog(dest)
    try:
        if action == "keep_all" or action == "keep_both":
            ids = ([r[0] for r in cat.db.execute("SELECT sha FROM files WHERE category='_Review' AND converted=0")]
                   if action == "keep_all" else [sha])
            n = 0
            for i in ids:
                mcat = None
                if match and action == "keep_both":
                    m = cat.db.execute("SELECT category FROM files WHERE sha=?", (match,)).fetchone()
                    mcat = m[0] if m else None
                n += 1 if _promote(cat, dest, i, mcat) else 0
            cat.commit()
            return {"kept": n}
        if action == "keep_copy":       # keep the set-aside one, delete the library one
            mcat = None
            m = cat.db.execute("SELECT category FROM files WHERE sha=?", (match,)).fetchone()
            if m:
                mcat = m[0]
            ok = _promote(cat, dest, sha, mcat)
            cat.commit()
    finally:
        cat.close()
    if action == "keep_copy":
        return {"kept": 1 if ok else 0, **trash_photos(dest, [match] if match else [])}
    if action == "delete_all":
        cat = Catalog(dest)
        try:
            ids = [r[0] for r in cat.db.execute("SELECT sha FROM files WHERE category='_Review' AND converted=0")]
        finally:
            cat.close(backup=False)
        return trash_photos(dest, ids)
    return {"error": "Unknown action."}


# --------------------------------------------------------------------------
# thumbnails (made on demand, cached on this computer)
# --------------------------------------------------------------------------
def _open_upright(path, max_side):
    """Open an image, apply its camera rotation, shrink it, return RGB PIL image."""
    from PIL import ImageOps
    with Image.open(path) as im:
        if im.format == "JPEG":
            im.draft("RGB", (max_side, max_side))
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        return im.copy()


def thumb_file(dest, rel, size="sm"):
    """Path of a cached JPEG thumbnail for a library file (or None)."""
    dest = Path(dest).expanduser().resolve()
    src = (dest / rel).resolve()
    if dest not in src.parents or not src.is_file() or src.suffix.lower() in VIDEO_EXT:
        return None
    px = 1800 if size == "lg" else 360
    cache = lib_dir(dest) / "thumbs" / size
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / (hashlib.sha1(rel.encode("utf-8", "ignore")).hexdigest() + ".jpg")
    if not out.exists():
        try:
            _open_upright(src, px).save(out, "JPEG", quality=82 if size == "sm" else 88)
        except Exception:
            return None
    return out


# --------------------------------------------------------------------------
# find people (face detection + grouping; runs on this computer)
# --------------------------------------------------------------------------
def face_model_files():
    """Find dlib's model files WITHOUT importing face_recognition_models (that package
    needs 'pkg_resources', which newer Python versions on Windows do not include)."""
    import importlib.util
    spec = importlib.util.find_spec("face_recognition_models")
    if spec is None or not spec.submodule_search_locations:
        return None
    d = Path(list(spec.submodule_search_locations)[0]) / "models"
    five = d / "shape_predictor_5_face_landmarks.dat"
    rec = d / "dlib_face_recognition_resnet_model_v1.dat"
    return (str(five), str(rec)) if five.exists() and rec.exists() else None


def addon_status():
    """What optional add-ons are working on this computer."""
    out = {"heic": HAVE_HEIF}
    try:
        import importlib.util
        out["tags"] = importlib.util.find_spec("fastembed") is not None
    except Exception:
        out["tags"] = False
    try:
        import importlib.util
        import dlib  # noqa: F401
        out["faces"] = face_model_files() is not None and importlib.util.find_spec("numpy") is not None
    except Exception:
        out["faces"] = False
    try:
        import importlib.util
        out["places"] = importlib.util.find_spec("reverse_geocoder") is not None
    except Exception:
        out["places"] = False
    return out


class FaceFinder:
    """Uses dlib (a well-known open-source face library). Each face becomes 128
    numbers; faces whose numbers are close together are grouped as one person.
    You then name each group once."""
    BATCH = 24
    JOIN_DIST = 0.50      # a face joins an existing group if closer than this
    MERGE_DIST = 0.40     # groups closer than this are merged at the end
    MIN_FACE_PX = 36
    MIN_SCORE = 0.4       # detector confidence; real faces score ~1+, false alarms ~0

    def __init__(self, dest, on_progress=None):
        self.dest = Path(dest).expanduser()
        self.cb = on_progress or (lambda **k: None)
        self.stop = False
        self.faces_found = 0
        self.photos_done = 0
        self._tl = threading.local()

    def _models(self):
        m = getattr(self._tl, "m", None)
        if m is None:
            import dlib
            five, rec = face_model_files()
            m = (dlib.get_frontal_face_detector(), dlib.shape_predictor(five),
                 dlib.face_recognition_model_v1(rec))
            self._tl.m = m
        return m

    def _detect(self, path):
        """Worker thread: returns list of (rect, embedding, jpeg_bytes) or None if unreadable."""
        import numpy as np
        import io
        try:
            pil = _open_upright(path, 1600)
        except Exception:
            return None
        det, sp, rec = self._models()
        arr = np.array(pil)
        out = []
        rects, scores, _ = det.run(arr, 0, 0.0)
        for r, score in zip(rects, scores):
            w, h = r.right() - r.left(), r.bottom() - r.top()
            if min(w, h) < self.MIN_FACE_PX or score < self.MIN_SCORE:
                continue      # too small, or probably not a real face
            shape = sp(arr, r)
            emb = np.array(rec.compute_face_descriptor(arr, shape), dtype=np.float32)
            pad = int(0.35 * max(w, h))
            box = (max(0, r.left() - pad), max(0, r.top() - pad),
                   min(pil.width, r.right() + pad), min(pil.height, r.bottom() + pad))
            crop = pil.crop(box).resize((112, 112))
            buf = io.BytesIO()
            crop.save(buf, "JPEG", quality=85)
            out.append(((r.left(), r.top(), w, h), emb, buf.getvalue()))
        return out

    def run(self):
        started = time.time()
        try:
            import numpy as np
            import dlib  # noqa: F401
        except ImportError as e:
            return ("Finding people needs an add-on that is not installed (" + str(e) + ").\n"
                    "Close Memory Lane and run Memory Lane Setup again; if it still fails, look for any "
                    "message about 'dlib'. If it cannot install, tell me what it says.")
        if face_model_files() is None:
            return ("Finding people needs the face model files, which are not installed.\n"
                    "Close Memory Lane and run Memory Lane Setup again. If it still "
                    "fails, send the file memorylane.log from the .photokeeper folder in your user folder.")
        try:
            self._models()       # load now, so a problem is reported clearly instead of per photo
        except Exception as e:
            return f"The face models could not be loaded: {e}"
        cat = Catalog(self.dest)
        todo = cat.unfaced()
        total = len(todo)
        if total == 0:
            self.cb(phase="Grouping similar faces...", done=0, total=1, eta="")
            merged = self._refine(cat, np)
            cat.close()
            return "Everything has already been checked for faces." + (
                f" Tidied up the groups ({merged} look-alike groups merged)." if merged else "")
        facedir = lib_dir(self.dest) / "faces"
        facedir.mkdir(parents=True, exist_ok=True)

        # existing groups (so new photos join people you already named)
        sums, counts = {}, {}
        for cid, emb in cat.db.execute("SELECT cluster, emb FROM faces WHERE cluster IS NOT NULL"):
            v = np.frombuffer(emb, dtype=np.float32)
            sums[cid] = sums.get(cid, 0) + v
            counts[cid] = counts.get(cid, 0) + 1
        ids = list(sums.keys())
        cents = (np.stack([sums[i] / counts[i] for i in ids]) if ids
                 else np.zeros((0, 128), dtype=np.float32))
        cnts = [counts[i] for i in ids]

        def assign(emb):
            nonlocal cents
            if len(ids):
                d = np.linalg.norm(cents - emb, axis=1)
                j = int(np.argmin(d))
                if d[j] < self.JOIN_DIST:
                    cents[j] = (cents[j] * cnts[j] + emb) / (cnts[j] + 1)
                    cnts[j] += 1
                    return ids[j]
            cid = cat.new_cluster()
            ids.append(cid)
            cnts.append(1)
            cents = np.vstack([cents, emb[None, :]])
            return cid

        self.cb(phase="Looking for faces...", done=0, total=total, eta="")
        done = 0
        with ThreadPoolExecutor(max(2, min(4, os.cpu_count() or 2))) as pool:
            for start in range(0, total, self.BATCH):
                if self.stop:
                    break
                batch = todo[start:start + self.BATCH]
                self.cb(phase="Looking for faces...", done=done, total=total,
                        current=Path(batch[0][2]).name,
                        eta=fmt_eta((time.time() - started) / done * (total - done)) if done > 30 else "")
                paths = [(b[1] if b[1] and os.path.exists(b[1]) else str(self.dest / b[2])) for b in batch]
                for b, res in zip(batch, pool.map(self._detect, paths)):
                    if res is None:
                        cat.set_faced(b[0], 2)
                        continue
                    for rect, emb, jpg in res:
                        cid = assign(emb)
                        fid = cat.add_face(b[0], rect, emb.tobytes(), cid)
                        (facedir / f"{fid}.jpg").write_bytes(jpg)
                        self.faces_found += 1
                    cat.set_faced(b[0], 1)
                    self.photos_done += 1
                cat.commit()
                done += len(batch)

        self.cb(phase="Grouping similar faces...", done=total, total=total, current="", eta="")
        merged = self._refine(cat, np)
        groups = cat.db.execute("SELECT COUNT(*) FROM clusters c WHERE c.ignored=0 AND "
                                "(SELECT COUNT(*) FROM faces f WHERE f.cluster=c.id) >= 3").fetchone()[0]
        left = len(cat.unfaced())
        cat.close()
        self.cb(phase="Done", done=total - left, total=total, eta="")
        msg = (f"Finding people {'paused' if self.stop else 'finished'}: {self.faces_found} faces found in "
               f"{self.photos_done} photos, grouped into about {groups} people "
               f"({merged} look-alike groups merged).  Time: {fmt_eta(time.time() - started)}")
        if left:
            msg += f"\n{left} photos still to check. Run it again to continue."
        else:
            msg += "\nNext: open the People page and type a name under each face group."
        return msg

    def _refine(self, cat, np):
        """Merge groups that are clearly the same person (never two differently named people).
        Works from a list of close pairs, so it always finishes and stays fast."""
        import heapq
        rows = cat.db.execute("SELECT id, name FROM clusters WHERE ignored=0").fetchall()
        if len(rows) < 2:
            return 0
        sums, counts = {}, {}
        for cid, emb in cat.db.execute("SELECT cluster, emb FROM faces WHERE cluster IS NOT NULL"):
            v = np.frombuffer(emb, dtype=np.float32)
            sums[cid] = sums.get(cid, 0) + v
            counts[cid] = counts.get(cid, 0) + 1
        names = {cid: (n or "").strip().lower() for cid, n in rows}
        ids = [cid for cid, _ in rows if cid in sums]
        if len(ids) < 2:
            return 0
        cents = np.stack([sums[i] / counts[i] for i in ids]).astype(np.float32)
        n = np.array([counts[i] for i in ids], dtype=np.float64)
        nm = [names[i] for i in ids]
        alive = np.ones(len(ids), dtype=bool)
        ver = [0] * len(ids)
        M = self.MERGE_DIST

        def conflict(a, b):
            return bool(nm[a] and nm[b] and nm[a] != nm[b])

        heap = []
        CH = 1500
        sq = (cents ** 2).sum(1)
        for a in range(0, len(ids), CH):      # close pairs only, found a block at a time
            blk = cents[a:a + CH]
            d = np.sqrt(np.maximum(sq[a:a + CH, None] + sq[None, :] - 2 * blk @ cents.T, 0))
            for ii, jj in np.argwhere(d < M):
                i, j = a + int(ii), int(jj)
                if i < j and not conflict(i, j):
                    heapq.heappush(heap, (float(d[ii, jj]), i, j, 0, 0))
        merged = 0
        deadline = time.time() + 300           # never run longer than 5 minutes
        while heap and time.time() < deadline:
            dist, i, j, vi, vj = heapq.heappop(heap)
            if not (alive[i] and alive[j]) or ver[i] != vi or ver[j] != vj:
                continue
            keep, drop = (i, j) if n[i] >= n[j] else (j, i)
            cat.merge_clusters(ids[keep], ids[drop])
            cents[keep] = (cents[keep] * n[keep] + cents[drop] * n[drop]) / (n[keep] + n[drop])
            n[keep] += n[drop]
            if not nm[keep] and nm[drop]:
                nm[keep] = nm[drop]
            alive[drop] = False
            ver[keep] += 1
            merged += 1
            dd = np.sqrt(np.maximum(((cents - cents[keep]) ** 2).sum(1), 0))
            for k in np.nonzero((dd < M) & alive)[0]:
                k = int(k)
                if k != keep and not conflict(keep, k):
                    a, b = (keep, k) if keep < k else (k, keep)
                    heapq.heappush(heap, (float(dd[k]), a, b, ver[a], ver[b]))
        cat.commit()
        return merged


# --------------------------------------------------------------------------
# check before you delete (never deletes anything)
# --------------------------------------------------------------------------
class Verifier:
    def __init__(self, sources, dest, thorough=False, on_progress=None):
        self.org = Organizer(sources, dest)
        self.sources = self.org.sources
        self.dest = Path(dest).expanduser()
        self.thorough = thorough
        self.cb = on_progress or (lambda **k: None)
        self.stop = False
        self.result = []

    def run(self):
        started = time.time()
        if not catalog_exists(self.dest):
            return "There is no library here yet, so there is nothing to check against."
        self.cb(phase="Looking for photos...", done=0, total=0, eta="")
        files = self.org.collect()
        total = len(files)
        if total == 0:
            return "No photos or videos were found in the folders you chose."
        cat = Catalog(self.dest)
        per = {}
        unsafe_lines = []

        def root_of(p):
            for s in self.sources:
                try:
                    p.relative_to(s)
                    return str(s)
                except ValueError:
                    continue
            return str(p.parent)

        def work(path):
            try:
                st = path.stat()
                if path.suffix.lower() in IMAGE_EXT and st.st_size < SMALL_IMAGE_BYTES:
                    return path, "tiny", ""
                key = quick_key(path, st.st_size)
                return path, key, ""
            except Exception as e:
                return path, None, str(e)

        done = 0
        with ThreadPoolExecutor(max(4, min(12, (os.cpu_count() or 4) * 2))) as pool:
            for start in range(0, total, 128):
                if self.stop:
                    break
                chunk = files[start:start + 128]
                self.cb(phase="Checking your originals...", done=done, total=total,
                        current=chunk[0].name,
                        eta=fmt_eta((time.time() - started) / done * (total - done)) if done > 40 else "")
                for path, key, err in pool.map(work, chunk):
                    r = per.setdefault(root_of(path), {"total": 0, "safe": 0, "unsafe": 0, "tiny": 0})
                    r["total"] += 1
                    if key == "tiny":
                        r["tiny"] += 1
                        continue
                    if key is None:
                        r["unsafe"] += 1
                        unsafe_lines.append(f"{path}  (could not read: {err})")
                        continue
                    ok, why = self._in_library(cat, key, path)
                    if ok:
                        r["safe"] += 1
                    else:
                        r["unsafe"] += 1
                        unsafe_lines.append(f"{path}  ({why})")
                done += len(chunk)
        cat.close(backup=False)
        lines = ["CHECK BEFORE YOU DELETE   (nothing was changed or deleted)", ""]
        for folder, r in sorted(per.items()):
            status = "SAFE TO DELETE" if r["unsafe"] == 0 and not self.stop else "NOT YET SAFE"
            note = f", {r['tiny']} tiny icons/thumbnails not copied" if r["tiny"] else ""
            lines.append(f"{status}: {folder}")
            lines.append(f"     {r['safe']} of {r['total'] - r['tiny']} photos/videos verified in the library{note}")
            self.result.append({"folder": folder, "total": r["total"], "safe": r["safe"],
                                "unsafe": r["unsafe"], "tiny": r["tiny"],
                                "ok": r["unsafe"] == 0 and not self.stop})
        if unsafe_lines:
            lines += ["", f"{len(unsafe_lines)} file(s) are NOT safely in the library yet "
                          f"(first {min(40, len(unsafe_lines))} shown):"]
            lines += ["  " + u for u in unsafe_lines[:40]]
            try:
                (self.dest / ".photokeeper").mkdir(parents=True, exist_ok=True)
                (self.dest / ".photokeeper" / "not_safe_to_delete.txt").write_text(
                    "\n".join(unsafe_lines), encoding="utf-8")
            except Exception:
                pass
        lines += ["", "Only photos and videos are checked. Other files in those folders "
                      "(documents, etc.) are not.",
                  ("Method: full byte-for-byte comparison." if self.thorough
                   else "Method: quick check (matching fingerprint, and the library copy exists at the same size).") +
                  f"  Time: {fmt_eta(time.time() - started)}"]
        if self.stop:
            lines.insert(1, "Stopped early. This list is incomplete.")
        self.cb(phase="Done", done=done, total=total, eta="")
        return "\n".join(lines)

    def _in_library(self, cat, key, src_path):
        row = cat.db.execute("SELECT rel_path, converted, size FROM files WHERE sha=?", (key,)).fetchone()
        if not row:
            return False, "not in the library"
        rel, converted, size = row
        if converted == 2:
            return True, ""  # you deleted this from the library on purpose
        lib_file = self.dest / rel
        if not lib_file.exists():
            if converted:
                j = cat.db.execute("SELECT rel_path FROM files WHERE sha=?", ("jpg:" + key,)).fetchone()
                if j and (self.dest / j[0]).exists():
                    return True, ""
            return False, "library copy is missing"
        try:
            if lib_file.stat().st_size != src_path.stat().st_size:
                return False, "library copy is a different size"
            if self.thorough and sha256_of(src_path) != sha256_of(lib_file):
                return False, "library copy does not match exactly"
        except Exception as e:
            return False, f"could not compare: {e}"
        return True, ""


# --------------------------------------------------------------------------
# do everything (and the weekly automatic run)
# --------------------------------------------------------------------------
COUNTRIES = {
    "US": "USA", "CA": "Canada", "MX": "Mexico", "GB": "United Kingdom", "IE": "Ireland", "FR": "France",
    "DE": "Germany", "ES": "Spain", "PT": "Portugal", "IT": "Italy", "NL": "Netherlands", "BE": "Belgium",
    "CH": "Switzerland", "AT": "Austria", "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
    "IS": "Iceland", "PL": "Poland", "CZ": "Czechia", "GR": "Greece", "TR": "Turkey", "HR": "Croatia",
    "HU": "Hungary", "RO": "Romania", "RU": "Russia", "UA": "Ukraine", "IL": "Israel", "EG": "Egypt",
    "ZA": "South Africa", "KE": "Kenya", "MA": "Morocco", "AE": "United Arab Emirates", "IN": "India",
    "CN": "China", "JP": "Japan", "KR": "South Korea", "TH": "Thailand", "VN": "Vietnam", "ID": "Indonesia",
    "PH": "Philippines", "SG": "Singapore", "MY": "Malaysia", "AU": "Australia", "NZ": "New Zealand",
    "BR": "Brazil", "AR": "Argentina", "CL": "Chile", "CO": "Colombia", "PE": "Peru", "CR": "Costa Rica",
    "PA": "Panama", "JM": "Jamaica", "BS": "Bahamas", "DO": "Dominican Republic", "CU": "Cuba",
    "PR": "Puerto Rico", "VI": "U.S. Virgin Islands", "BM": "Bermuda", "KY": "Cayman Islands",
}


def gps_of(path):
    """(lat, lon) from a photo's embedded location, or None."""
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            g = exif.get_ifd(0x8825)
    except Exception:
        return None
    if not g:
        return None
    try:
        def deg(v):
            d, m, s = (float(x) for x in v)
            return d + m / 60.0 + s / 3600.0
        lat = deg(g[2])
        lon = deg(g[4])
        if str(g.get(1, "N")).upper().startswith("S"):
            lat = -lat
        if str(g.get(3, "E")).upper().startswith("W"):
            lon = -lon
    except Exception:
        return None
    if abs(lat) > 90 or abs(lon) > 180 or (abs(lat) < 1e-6 and abs(lon) < 1e-6):
        return None      # "0, 0" is a camera with no real fix
    return lat, lon


def place_name(hit):
    """'Gardiner, Montana' for the USA; 'Paris, France' elsewhere."""
    city = (hit.get("name") or "").strip()
    cc = hit.get("cc") or ""
    region = (hit.get("admin1") or "").strip()
    if cc == "US":
        tail = region or "USA"
    else:
        tail = COUNTRIES.get(cc, cc)
    return ", ".join(x for x in (city, tail) if x)


class Placer:
    """Reads the location stored inside each photo (when the camera had location on) and
    turns it into the nearest town, using a list of towns that lives on this computer.
    Nothing is sent over the internet."""
    BATCH = 200

    def __init__(self, dest, on_progress=None):
        self.dest = Path(dest).expanduser()
        self.cb = on_progress or (lambda **k: None)
        self.stop = False

    def run(self):
        started = time.time()
        if not HAVE_PIL:
            return "Places needs an add-on ('pillow') that is not installed."
        cat = Catalog(self.dest)
        todo = cat.db.execute(
            "SELECT sha, orig_path, rel_path FROM files WHERE placed=0 AND converted=0 "
            "AND category NOT IN ('Videos', '_Review')").fetchall()
        total = len(todo)
        if total == 0:
            cat.close()
            return "Every photo has already been checked for a location."
        try:
            import reverse_geocoder as rg
        except ImportError:
            cat.close()
            return ("Places needs an add-on ('reverse_geocoder') that is not installed.\n"
                    "Close Memory Lane and run Memory Lane Setup again; if it still fails, look for any "
                    "message about 'reverse_geocoder'.")
        found = 0
        with ThreadPoolExecutor(max(2, min(8, os.cpu_count() or 2))) as pool:
            for start in range(0, total, self.BATCH):
                if self.stop:
                    break
                batch = todo[start:start + self.BATCH]
                self.cb(phase="Finding where photos were taken...", done=start, total=total,
                        current=Path(batch[0][2]).name,
                        eta=fmt_eta((time.time() - started) / start * (total - start)) if start > 200 else "")
                paths = [o if (o and os.path.exists(o)) else str(self.dest / r) for _, o, r in batch]
                gps = list(pool.map(gps_of, paths))
                have = [(b[0], g) for b, g in zip(batch, gps) if g]
                names = {}
                if have:
                    try:
                        hits = rg.search([g for _, g in have], mode=1)
                    except Exception as e:
                        cat.close()
                        return f"The place list could not be used: {e}"
                    names = {sha: (place_name(h), g) for (sha, g), h in zip(have, hits)}
                for (sha, _, _), g in zip(batch, gps):
                    if sha in names:
                        nm, (la, lo) = names[sha]
                        cat.db.execute("UPDATE files SET placed=1, place=?, lat=?, lon=? WHERE sha=?",
                                       (nm, la, lo, sha))
                        found += 1
                    else:
                        cat.db.execute("UPDATE files SET placed=2 WHERE sha=?", (sha,))
                cat.db.commit()
        cat.close()
        left = ""
        self.cb(phase="Done", done=total, total=total, eta="")
        return (f"Checked {total:,} photos. {found} had a location and now show "
                "where they were taken; the rest have no location stored in them." + left)


class Pipeline:
    """Organize -> convert HEIC -> smart tags -> find people, in one go."""

    def __init__(self, cfg, on_progress=None):
        self.cfg = cfg
        self.cb = on_progress or (lambda **k: None)
        self.current = None
        self._stop = False
        self.stats = {}

    @property
    def stop(self):
        return self._stop

    @stop.setter
    def stop(self, v):
        self._stop = v
        if self.current is not None:
            self.current.stop = v

    def run(self):
        c = self.cfg
        dest = c.get("dest", "")
        steps = [
            ("Organize photos", lambda: Organizer(
                c.get("sources", []), dest, False, None, c.get("lookalikes", True),
                c.get("naming", "keep"), c.get("heic_folder", True))),
        ]
        if c.get("heic_folder", True):
            steps.append(("Convert iPhone photos to JPEG", lambda: HeicConverter(dest)))
        steps += [("Smart tags", lambda: Tagger(dest)),
                  ("Find places", lambda: Placer(dest)),
                  ("Find people", lambda: FaceFinder(dest))]
        reports = []
        n = len(steps)
        for i, (label, make) in enumerate(steps, 1):
            if self._stop:
                reports.append("Stopped before: " + label)
                break
            obj = make()
            self.current = obj

            def cb(i=i, **kw):
                if kw.get("phase") == "Done":
                    return
                if "phase" in kw:
                    kw["phase"] = f"Step {i} of {n}: {kw['phase']}"
                self.cb(**kw)
            obj.cb = cb
            try:
                rep = obj.run()
            except Exception as e:
                rep = f"This step hit a problem and was skipped: {e}"
            if isinstance(obj, Organizer):
                self.stats = dict(obj.stats)
            reports.append(f"=== {label} ===\n{rep}")
        self.cb(phase="Done", eta="")
        flags = ("needs an add-on", "could not be loaded", "stopped with an error",
                 "problem and was skipped", "face model files", "could not be used")
        bad = [r.split("===")[1].strip() for r in reports if any(f in r for f in flags)]
        head = ""
        if bad:
            head = ("ATTENTION: these steps could not finish: " + ", ".join(bad) +
                    ". Details are below. Everything else completed.\n\n")
        return head + "\n\n".join(reports)


TASK_NAME = "PhotoKeeperWeekly"
DAYS = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


NO_WINDOW = {"creationflags": 0x08000000} if os.name == "nt" else {}     # never flash a console window


def schedule_status():
    if os.name != "nt":
        return {"supported": False, "enabled": False}
    import subprocess
    try:
        r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"],
                           capture_output=True, text=True, timeout=20, **NO_WINDOW)
    except Exception:
        return {"supported": True, "enabled": False}
    if r.returncode != 0:
        return {"supported": True, "enabled": False}
    info = ""
    for line in r.stdout.splitlines():
        if line.lower().startswith("next run time"):
            info = line.split(":", 1)[1].strip()
    return {"supported": True, "enabled": True, "next": info}


def schedule_set(enabled, day="SUN", at="02:00"):
    if os.name != "nt":
        return {"error": "Automatic weekly runs are only available on Windows."}
    import subprocess
    if not enabled:
        r = subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                           capture_output=True, text=True, timeout=20, **NO_WINDOW)
        return {"ok": True}
    if day not in DAYS or not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", at or ""):
        return {"error": "Please choose a valid day and time."}
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    runner = str(pyw if pyw.exists() else exe)
    script = str(Path(__file__).resolve())
    workdir = str(Path(script).parent)
    # Preferred: PowerShell, so the task can wake the computer and catch up if a run was missed.
    q = lambda t: "'" + t.replace("'", "''") + "'"
    day_full = {"MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday",
                "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday"}[day]
    ps = (f"$a = New-ScheduledTaskAction -Execute {q(runner)} -Argument {q(chr(34) + script + chr(34) + ' --auto')} "
          f"-WorkingDirectory {q(workdir)}; "
          f"$t = New-ScheduledTaskTrigger -Weekly -DaysOfWeek {day_full} -At {q(at)}; "
          "$s = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable -AllowStartIfOnBatteries "
          "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12); "
          f"Register-ScheduledTask -TaskName {q(TASK_NAME)} -Action $a -Trigger $t -Settings $s -Force | Out-Null")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=60, **NO_WINDOW)
        if r.returncode == 0:
            return {"ok": True, "wakes": True}
    except Exception:
        pass
    r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "WEEKLY", "/D", day,
                        "/ST", at, "/TR", f'"{runner}" "{script}" --auto', "/F"],
                       capture_output=True, text=True, timeout=30, **NO_WINDOW)
    if r.returncode != 0:
        return {"error": "Windows would not create the schedule: " + (r.stderr or r.stdout).strip()}
    return {"ok": True, "wakes": False}


def run_auto():
    """Headless weekly run: uses the folders saved from the last time you used the app."""
    cfg = load_config()
    log = CONFIG_DIR / "auto_log.txt"
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    def note(text):
        with open(log, "a", encoding="utf-8") as f:
            f.write(f"[{dt.datetime.now():%Y-%m-%d %H:%M}] {text}\n")

    srcs = [s for s in cfg.get("sources", []) if s]
    if not srcs or not cfg.get("dest"):
        note("Skipped: no folders saved yet. Open Memory Lane and run it once first.")
        return
    if not any(Path(s).is_dir() for s in srcs) or not Path(cfg["dest"]).parent.exists():
        note("Skipped: the photo folders or the library drive are not available right now.")
        return
    try:
        started = dt.datetime.now().isoformat(timespec="seconds")
        p = Pipeline(cfg)
        rep = p.run()
        try:
            if catalog_exists(cfg["dest"]):
                bc = Catalog(cfg["dest"])
                try:
                    bc.add_batch(started)
                finally:
                    bc.close()
        except Exception as e:
            note(f"(could not record the new-photo list: {e})")
        cfg["last_run"] = {"time": dt.datetime.now().isoformat(timespec="minutes"),
                           "stats": p.stats, "auto": True}
        save_config(cfg)
        note("Finished.\n" + rep)
    except Exception as e:
        note(f"Stopped with an error: {e}")


# --------------------------------------------------------------------------
# local web app (runs only on this computer; nothing is uploaded anywhere)
# --------------------------------------------------------------------------
STATE = {"running": False, "job": "", "phase": "Ready", "done": 0, "total": 0,
         "current": "", "report": "", "stats": {}, "eta": "", "extra": None}
ORG = {"obj": None}
TOKEN = secrets.token_hex(16)
PORT = {"n": 0}
LIVE = {"ping": 0.0, "bye": 0.0}      # when the window last said hello / goodbye
UI_FILE = Path(__file__).with_name("ui.html")


def _progress(**kw):
    STATE.update({k: v for k, v in kw.items() if k in STATE})


def launch(kind, obj, after=None):
    if STATE["running"]:
        return {"error": "A job is already running. Please wait for it to finish or pause it."}
    obj.cb = _progress
    ORG["obj"] = obj
    STATE.update(running=True, job=kind, phase="Starting...", done=0, total=0, current="",
                 report="", stats={}, eta="", extra=None)

    def work():
        try:
            rep = obj.run()
        except Exception as e:
            rep = f"Something went wrong: {e}"
        try:
            if after:
                after(obj)
        except Exception:
            pass
        STATE.update(running=False, report=rep, phase="Done", extra=getattr(obj, "result", None))

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


_UPD = {"t": 0, "v": None}


def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:4])


def check_for_update():
    """Ask GitHub whether a newer Memory Lane release exists. Quiet if offline. Cached for 6 hours."""
    if _UPD["v"] is not None and time.time() - _UPD["t"] < 6 * 3600:
        return _UPD["v"]
    out = {"available": False}
    try:
        import urllib.request
        req = urllib.request.Request(
            os.environ.get("MEMORYLANE_UPDATE_URL") or f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest",
            headers={"User-Agent": "MemoryLane-update-check", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=6) as r:
            d = json.loads(r.read().decode("utf-8"))
        tag = str(d.get("tag_name", ""))
        if tag and _vtuple(tag) > _vtuple(VERSION):
            out = {"available": True, "version": tag.lstrip("vV"),
                   "url": d.get("html_url") or f"https://github.com/{UPDATE_REPO}/releases/latest"}
    except Exception:
        pass
    _UPD["t"], _UPD["v"] = time.time(), out
    return out


def library_missing(dest):
    """True when a library folder is set but no longer exists (deleted or drive unplugged)."""
    return bool(str(dest).strip()) and not Path(dest).expanduser().exists()


def reset_library(dest):
    """Forget the saved catalog, thumbnails and face data for this library.
    Never touches any photo files."""
    target = lib_dir(dest)
    if (CONFIG_DIR / "libs") not in target.parents:
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


def _need_library(dest):
    if not dest or not catalog_exists(dest):
        return {"error": "There is no library here yet. Run 'Organize everything' first."}
    if not HAVE_PIL:
        return {"error": "The Pillow package is missing. Run: pip install pillow pillow-heif"}
    return None


def _remember_run(stats, auto=False):
    cfg = load_config()
    cfg["last_run"] = {"time": dt.datetime.now().isoformat(timespec="minutes"),
                       "stats": stats, "auto": auto}
    save_config(cfg)


def _check_folders(sources, dest):
    sources = [s.strip() for s in sources if str(s).strip()]
    if not sources:
        return None, {"error": "Please add at least one folder that has your photos (Settings)."}
    if not str(dest).strip():
        return None, {"error": "Please choose where the clean library should go (Settings)."}
    if not HAVE_PIL:
        return None, {"error": "The Pillow package is missing. Run: pip install pillow pillow-heif"}
    try:
        dp = Path(dest).resolve()
        for s in sources:
            if Path(s).resolve() == dp:
                return None, {"error": "The library folder can't be the same as a photo folder."}
    except Exception:
        pass
    return sources, None


def pick_folder():
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="Choose a folder")
        root.destroy()
        return {"path": os.path.normpath(path) if path else ""}
    except Exception:
        return {"error": "The folder picker isn't available here. Please type the folder path instead."}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok_host(self):
        return (self.headers.get("Host") or "").split(":")[0] in ("127.0.0.1", "localhost")

    def _send(self, obj, code=200, ctype="application/json", cache=False):
        if isinstance(obj, bytes):
            data = obj
        elif isinstance(obj, str):
            data = obj.encode("utf-8")
        else:
            data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype if "image" in ctype else ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _auth(self, query=None):
        if not self._ok_host():
            return False
        return self.headers.get("X-Token") == TOKEN or (query or {}).get("tok", [""])[0] == TOKEN

    def _dest(self):
        return load_config().get("dest", "")

    def do_GET(self):
        if not self._ok_host():
            return self._send({"error": "bad host"}, 403)
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/update_check":
            return self._send(check_for_update())
        if u.path == "/api/alive":      # lets a second launch find the copy that is already open
            return self._send({"app": APP_NAME, "version": VERSION})
        if u.path == "/favicon.ico":
            return self._send(b"", 204, "image/x-icon")
        if u.path == "/":
            try:
                html = UI_FILE.read_text(encoding="utf-8")
            except Exception:
                return self._send("ui.html is missing. Please run Memory Lane Setup again.",
                                  500, "text/plain")
            return self._send(html.replace("__TOKEN__", TOKEN).replace("__VERSION__", VERSION),
                              ctype="text/html")
        if u.path in ("/api/ping", "/api/bye") and self._auth(q):
            LIVE["bye" if u.path == "/api/bye" else "ping"] = time.time()
            return self._send({"ok": True})
        if not self._auth(q):
            return self._send({"error": "unauthorized"}, 403)
        dest = self._dest()
        if u.path == "/api/config":
            cfg = load_config()
            if not cfg.get("dest"):
                ic = find_icloud_drive()
                if ic:
                    cfg["dest"] = str(Path(ic) / "Photo Library")
            cfg["missing"] = [x for x in cfg.get("sources", []) if x and not Path(x).exists()]
            cfg["dest_missing"] = library_missing(cfg.get("dest", "")) and catalog_exists(cfg.get("dest", ""))
            return self._send(cfg)
        if u.path == "/api/status":
            return self._send(STATE)
        if u.path == "/api/schedule":
            return self._send(schedule_status())
        if u.path == "/api/labels":
            cfg = load_config()
            off = set(cfg.get("disabled_labels", []))
            return self._send({"builtin": [{"tag": t, "prompt": p, "on": t not in off}
                                           for t, p in TAG_LABELS],
                               "custom": cfg.get("custom_labels", [])})
        if u.path == "/api/addons":
            return self._send(addon_status())
        if u.path == "/api/stats":
            if not dest or not catalog_exists(dest) or library_missing(dest):
                return self._send({"empty": True, "last_run": load_config().get("last_run"),
                                   "missing": library_missing(dest) and catalog_exists(dest)})
            c = Catalog(dest)
            try:
                st = c.stats()
            finally:
                c.close(backup=False)
            st["last_run"] = load_config().get("last_run")
            st["library"] = str(Path(dest) / "Library")
            return self._send(st)
        if u.path in ("/api/photos", "/api/tags", "/api/people"):
            if not dest or not catalog_exists(dest) or library_missing(dest):
                return self._send({"items": [], "total": 0, "tags": [], "named": [], "unnamed": [],
                                   "empty": True})
            c = Catalog(dest)
            try:
                if u.path == "/api/photos":
                    return self._send(c.photos(q.get("q", [""])[0], q.get("person", [""])[0],
                                               int(q.get("offset", ["0"])[0]),
                                               min(200, int(q.get("limit", ["60"])[0])),
                                               trashed=q.get("trash", ["0"])[0] == "1",
                                               tag=q.get("tag", [""])[0], cat=q.get("cat", [""])[0],
                                               named=q.get("named", [""])[0], since=q.get("since", [""])[0]))
                if u.path == "/api/tags":
                    return self._send({"tags": c.tag_counts()})
                return self._send(c.people_overview(int(q.get("min", ["1"])[0])))
            finally:
                c.close(backup=False)
        if u.path == "/api/thumb":
            p = thumb_file(dest, q.get("p", [""])[0], "lg" if q.get("s", [""])[0] == "lg" else "sm") \
                if dest else None
            if not p:
                return self._send({"error": "no image"}, 404)
            return self._send(p.read_bytes(), ctype="image/jpeg", cache=True)
        if u.path == "/api/video":
            try:
                root = Path(dest).expanduser().resolve()
                src = (root / q.get("p", [""])[0]).resolve()
                if root not in src.parents or not src.is_file() or src.suffix.lower() not in VIDEO_EXT:
                    return self._send({"error": "no video"}, 404)
                size = src.stat().st_size
                start, end = 0, size - 1
                rng = self.headers.get("Range", "")
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                code = 200
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                        if m.group(2):
                            end = min(int(m.group(2)), size - 1)
                    else:
                        start = max(0, size - int(m.group(2)))
                    code = 206
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                ctype = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
                         ".webm": "video/webm"}.get(src.suffix.lower(), "video/mp4")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                with open(src, "rb") as fh:
                    fh.seek(start)
                    left = end - start + 1
                    while left > 0:
                        buf = fh.read(min(262144, left))
                        if not buf:
                            break
                        self.wfile.write(buf)
                        left -= len(buf)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            return
        if u.path == "/api/batch":
            if not dest or not catalog_exists(dest) or library_missing(dest):
                return self._send({"batch": None})
            c = Catalog(dest)
            try:
                return self._send({"batch": c.unseen_batch()})
            finally:
                c.close(backup=False)
        if u.path == "/api/folders":
            return self._send({"folders": sub_folders(dest) if dest else []})
        if u.path == "/api/review":
            if not dest or not catalog_exists(dest):
                return self._send({"total": 0})
            return self._send(review_pair(dest, int(q.get("offset", ["0"])[0])))
        if u.path == "/api/photo_faces":
            if not dest or not catalog_exists(dest):
                return self._send({"faces": []})
            c = Catalog(dest)
            try:
                return self._send({"faces": c.photo_faces(q.get("id", [""])[0])})
            finally:
                c.close(backup=False)
        if u.path == "/api/face":
            try:
                fid = int(q.get("id", ["0"])[0])
                f = lib_dir(dest) / "faces" / f"{fid}.jpg"
                return self._send(f.read_bytes(), ctype="image/jpeg", cache=True)
            except Exception:
                return self._send({"error": "no face"}, 404)
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        if not self._auth():
            return self._send({"error": "unauthorized"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            body = {}
        path = urlparse(self.path).path
        cfg = load_config()
        dest = body.get("dest") or cfg.get("dest", "")

        if path == "/api/config":
            for k in ("sources", "dest", "naming", "heic_folder", "lookalikes"):
                if k in body:
                    cfg[k] = body[k]
            save_config(cfg)
            return self._send({"ok": True})

        if path in ("/api/start", "/api/everything"):
            sources, err = _check_folders(body.get("sources", cfg.get("sources", [])), dest)
            if err:
                return self._send(err)
            settings = {"sources": sources, "dest": dest,
                        "naming": body.get("naming", cfg.get("naming", "date")),
                        "heic_folder": bool(body.get("heic_folder", cfg.get("heic_folder", True))),
                        "lookalikes": bool(body.get("lookalikes", cfg.get("lookalikes", True)))}
            cfg.update(settings)
            save_config(cfg)
            if path == "/api/everything":
                return self._send(launch("everything", Pipeline(cfg),
                                         lambda o: _remember_run(o.stats)))
            dry = bool(body.get("dry"))
            org = Organizer(sources, dest, dry, None, settings["lookalikes"],
                            settings["naming"], settings["heic_folder"])
            return self._send(launch("organize-preview" if dry else "organize", org,
                                     (None if dry else lambda o: _remember_run(o.stats))))

        if path in ("/api/convert_start", "/api/tag_start", "/api/face_start", "/api/place_start"):
            err = _need_library(dest)
            if err:
                return self._send(err)
            if path == "/api/convert_start":
                return self._send(launch("convert", HeicConverter(dest)))
            if path == "/api/tag_start":
                return self._send(launch("tags", Tagger(dest, redo=bool(body.get("redo")))))
            if path == "/api/place_start":
                return self._send(launch("places", Placer(dest)))
            return self._send(launch("faces", FaceFinder(dest)))

        if path == "/api/verify_start":
            sources, err = _check_folders(body.get("sources", cfg.get("sources", [])), dest)
            if err:
                return self._send(err)
            err = _need_library(dest)
            if err:
                return self._send(err)
            return self._send(launch("verify", Verifier(sources, dest, bool(body.get("thorough")))))

        if path == "/api/labels":
            cfg["disabled_labels"] = [Catalog.clean_tag(t) for t in body.get("disabled", [])]
            cfg["custom_labels"] = [{"tag": Catalog.clean_tag(c.get("tag", "")),
                                     "prompt": (c.get("prompt") or "").strip()[:200]}
                                    for c in body.get("custom", [])
                                    if Catalog.clean_tag(c.get("tag", "")) and (c.get("prompt") or "").strip()]
            save_config(cfg)
            return self._send({"ok": True})
        if path == "/api/reset":
            if STATE["running"]:
                return self._send({"error": "A job is running. Pause it first."})
            d = cfg.get("dest", "")
            if d:
                reset_library(d)
            for k in ("sources", "dest", "last_run"):
                cfg.pop(k, None)
            save_config(cfg)
            STATE.update(report="", extra=None, job="", phase="Ready")
            return self._send({"ok": True})
        if path == "/api/stop":
            if ORG["obj"] is not None:
                ORG["obj"].stop = True
            return self._send({"ok": True})
        if path == "/api/browse":
            return self._send(pick_folder())
        if path == "/api/open":
            try:
                base = Path(dest).resolve()
                p = (base / body.get("path", "")).resolve()
                if base in p.parents and p.exists() and hasattr(os, "startfile"):
                    os.startfile(str(p.parent))
            except Exception:
                pass
            return self._send({"ok": True})
        if path in ("/api/people/assign", "/api/people/assign_face"):
            if not dest or not catalog_exists(dest):
                return self._send({"error": "No library yet."})
            c = Catalog(dest)
            try:
                if path.endswith("assign"):
                    return self._send(c.assign_photos([str(i) for i in body.get("ids", [])][:20000],
                                                      body.get("name", "")))
                ok = c.assign_face(body.get("face"), body.get("name", ""))
                return self._send({"ok": ok, "people": c.people_of(str(body.get("sha", "")))})
            finally:
                c.close()
        if path == "/api/people/ignore_face":
            if not dest or not catalog_exists(dest):
                return self._send({"error": "No library yet."})
            c = Catalog(dest)
            try:
                c.ignore_face(body.get("face"))
                return self._send({"ok": True})
            finally:
                c.close()
        if path == "/api/people/add_to_photo":
            if not dest or not catalog_exists(dest):
                return self._send({"error": "No library yet."})
            c = Catalog(dest)
            try:
                return self._send(c.add_person_to_photo(str(body.get("id", "")), body.get("name", "")))
            finally:
                c.close()
        if path in ("/api/people/delete_face", "/api/people/trash_group"):
            if STATE["running"]:
                return self._send({"error": "A job is running. Please wait for it to finish."})
            if not dest or not catalog_exists(dest) or library_missing(dest):
                return self._send({"error": "No library yet."})
            c = Catalog(dest)
            try:
                if path.endswith("delete_face"):
                    c.delete_face(body.get("face"))
                    return self._send({"ok": True})
                ids = c.group_photos(body.get("cluster"))
            finally:
                c.close()
            res = trash_photos(dest, ids)
            c = Catalog(dest)
            try:
                c.ignore_cluster(body.get("cluster"))
            finally:
                c.close()
            return self._send(res)
        if path == "/api/people/unlink":
            if not dest or not catalog_exists(dest):
                return self._send({"error": "No library yet."})
            c = Catalog(dest)
            try:
                n = c.unlink_person(str(body.get("id", "")), body.get("name", ""), body.get("new_name", ""))
            finally:
                c.close()
            return self._send({"ok": True, "moved": n})
        if path in ("/api/people/name", "/api/people/ignore"):
            if not dest or not catalog_exists(dest):
                return self._send({"error": "No library yet."})
            c = Catalog(dest)
            try:
                if path.endswith("name"):
                    c.name_clusters(body.get("clusters", []), body.get("name", ""))
                else:
                    c.ignore_cluster(body.get("cluster"))
            finally:
                c.close()
            return self._send({"ok": True})
        if path == "/api/review/act":
            if STATE["running"]:
                return self._send({"error": "A job is running. Please wait for it to finish."})
            if not dest or not catalog_exists(dest) or library_missing(dest):
                return self._send({"error": "No library yet."})
            return self._send(review_act(dest, body.get("action", ""), str(body.get("id", "")),
                                         str(body.get("match", ""))))
        if path in ("/api/trash", "/api/trash_restore", "/api/trash_empty"):
            if STATE["running"]:
                return self._send({"error": "A job is running. Please wait for it to finish."})
            if not dest or not catalog_exists(dest) or library_missing(dest):
                return self._send({"error": "No library yet."})
            ids = [str(i) for i in body.get("ids", [])][:20000]
            if path == "/api/trash":
                return self._send(trash_photos(dest, ids))
            if path == "/api/trash_restore":
                return self._send(restore_trash(dest, None if body.get("all") else ids))
            return self._send(empty_trash(dest))
        if path == "/api/kind":
            if STATE["running"]:
                return self._send({"error": "A job is running. Please wait for it to finish."})
            if not dest or not catalog_exists(dest) or library_missing(dest):
                return self._send({"error": "No library yet."})
            return self._send(change_kind(dest, [str(i) for i in body.get("ids", [])][:20000],
                                          body.get("kind", "")))
        if path == "/api/move":
            if STATE["running"]:
                return self._send({"error": "A job is running. Please wait for it to finish."})
            if not dest or not catalog_exists(dest) or library_missing(dest):
                return self._send({"error": "No library yet."})
            return self._send(move_to_folder(dest, [str(i) for i in body.get("ids", [])][:20000],
                                             body.get("name", ""), body.get("mode", "name"),
                                             bool(body.get("tag", True))))
        if path in ("/api/rename", "/api/rename_undo"):
            if STATE["running"]:
                return self._send({"error": "A job is running. Please wait for it to finish."})
            if not dest or not catalog_exists(dest) or library_missing(dest):
                return self._send({"error": "No library yet."})
            if path == "/api/rename_undo":
                return self._send(undo_last_rename(dest))
            return self._send(rename_photos(dest, [str(i) for i in body.get("ids", [])][:5000],
                                            body.get("base", ""), body.get("style", "single")))
        if path == "/api/batch_seen":
            if dest and catalog_exists(dest):
                c = Catalog(dest)
                try:
                    c.db.execute("UPDATE batches SET seen=1")
                    c.db.commit()
                finally:
                    c.close()
            return self._send({"ok": True})
        if path == "/api/reviewed":
            if not dest or not catalog_exists(dest):
                return self._send({"error": "No library yet."})
            c = Catalog(dest)
            try:
                c.set_reviewed([str(i) for i in body.get("ids", [])][:5000], bool(body.get("on", True)))
                return self._send({"ok": True})
            finally:
                c.close()
        if path in ("/api/tags/edit", "/api/tags/rename", "/api/tags/delete"):
            if not dest or not catalog_exists(dest):
                return self._send({"error": "No library yet."})
            c = Catalog(dest)
            try:
                if path.endswith("edit"):
                    ids = [str(i) for i in body.get("ids", [])][:5000]
                    if body.get("action") == "remove":
                        c.remove_tag(ids, body.get("tag", ""))
                    else:
                        c.add_tag(ids, body.get("tag", ""))
                elif path.endswith("rename"):
                    c.rename_tag(body.get("old", ""), body.get("new", ""))
                else:
                    c.delete_tag(body.get("tag", ""))
            finally:
                c.close()
            return self._send({"ok": True})
        if path == "/api/schedule":
            return self._send(schedule_set(bool(body.get("enabled")), body.get("day", "SUN"),
                                           body.get("time", "02:00")))
        return self._send({"error": "not found"}, 404)


def find_browser():
    """An Edge or Chrome that can open Memory Lane as its own window (no tabs or address bar)."""
    import shutil
    cands = []
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            cands += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                      Path(base) / "Google/Chrome/Application/chrome.exe"]
    for name in ("msedge", "chrome"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    for c in cands:
        if c.exists():
            return str(c)
    return None


def _work_area():
    """Usable screen (everything except the taskbar), so the window opens full size."""
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):   # SPI_GETWORKAREA
            w, h = r.right - r.left, r.bottom - r.top
            if w > 800 and h > 500:
                return r.left, r.top, w, h
    except Exception:
        pass
    return 0, 0, 1600, 950


def _maximize_window(title="Memory Lane", wait=20):
    """Windows only: find Memory Lane's window and make it fill the screen. Edge remembers
    its last size and can ignore the size we ask for, so we set it directly."""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        found = []
        EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def each(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    cls = ctypes.create_unicode_buffer(64)
                    user32.GetClassNameW(hwnd, cls, 64)
                    if buf.value.startswith(title) and cls.value.startswith("Chrome_WidgetWin"):
                        found.append(hwnd)
            return True

        end = time.time() + wait
        while time.time() < end:
            found.clear()
            user32.EnumWindows(EnumProc(each), 0)
            if found:
                user32.ShowWindow(found[0], 3)      # SW_MAXIMIZE
                return
            time.sleep(0.4)
    except Exception:
        pass


def open_window(url, app_mode):
    """Open Memory Lane in its own window; falls back to the normal browser."""
    if app_mode:
        exe = find_browser()
        if exe:
            try:
                import subprocess
                x, y, w, h = _work_area()
                subprocess.Popen([exe, f"--app={url}", f"--window-size={w},{h}",
                                  f"--window-position={x},{y}",
                                  "--no-first-run", "--disable-features=Translate"],
                                 close_fds=True)
                if os.name == "nt":
                    threading.Thread(target=_maximize_window, daemon=True).start()
                return
            except Exception:
                pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


RUNNING_FILE = CONFIG_DIR / "running.json"


def _already_running():
    """(url, version, pid) of a Memory Lane that is already open on this computer, or None."""
    try:
        import urllib.request
        info = json.loads(RUNNING_FILE.read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{int(info['port'])}/"
        with urllib.request.urlopen(url + "api/alive", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8"))
        if d.get("app") == APP_NAME:
            return url, d.get("version"), info.get("pid")
    except Exception:
        pass
    return None


def _retire_old_copy(pid):
    """An older copy is still running after an update: close it so the new one takes over."""
    try:
        pid = int(pid)
        if pid == os.getpid():
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, **NO_WINDOW)
        else:
            os.kill(pid, 15)
        for _ in range(25):
            time.sleep(0.2)
            if not _already_running():
                break
        try:
            RUNNING_FILE.unlink()
        except Exception:
            pass
    except Exception:
        pass


def serve(app_mode=False):
    class QuietServer(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            # the browser cancels pictures when you scroll fast; that is normal, not an error
            if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
                return
            super().handle_error(request, client_address)

    if app_mode:
        existing = _already_running()
        if existing and existing[1] != VERSION:      # left over from before an update
            _retire_old_copy(existing[2])
            existing = None
        if existing:             # already open: just bring up a window for it
            open_window(existing[0], True)
            return
    srv = QuietServer(("127.0.0.1", 0), Handler)
    PORT["n"] = srv.server_address[1]
    url = f"http://127.0.0.1:{PORT['n']}/"
    print(f"{APP_NAME} {VERSION} is running at {url}")
    if app_mode:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            RUNNING_FILE.write_text(json.dumps({"port": PORT["n"], "pid": os.getpid()}), encoding="utf-8")
        except Exception:
            pass
        LIVE["ping"] = time.time()

        def watchdog():
            # Quit by itself once the window is closed, but never in the middle of a job.
            while True:
                time.sleep(3)
                if STATE.get("running"):
                    LIVE["ping"] = max(LIVE["ping"], time.time() - 60)
                    continue
                now = time.time()
                closed = LIVE["bye"] > LIVE["ping"] and now - LIVE["bye"] > 12
                lost = now - LIVE["ping"] > 240
                if closed or lost:
                    srv.shutdown()
                    return

        threading.Thread(target=watchdog, daemon=True).start()
    else:
        print(f"Leave this window open while you use {APP_NAME}. Close it to quit.")
    open_window(url, app_mode)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if app_mode:
            try:
                RUNNING_FILE.unlink()
            except Exception:
                pass


def _go_quiet():
    """No console window: send messages to a log file instead (pythonw has no screen)."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        log = CONFIG_DIR / "memorylane.log"
        if log.exists() and log.stat().st_size > 2_000_000:
            log.replace(CONFIG_DIR / "memorylane.old.log")
        f = open(log, "a", encoding="utf-8", buffering=1)
        f.write(f"\n--- {dt.datetime.now():%Y-%m-%d %H:%M:%S} {APP_NAME} {VERSION} ---\n")
        sys.stdout = f
        sys.stderr = f
    except Exception:
        pass


def _fatal_box(message):
    """A plain Windows message box, since there is no console to read an error in."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x10)
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--app", action="store_true", help="open as a normal program window (used by the desktop icon)")
    ap.add_argument("--auto", action="store_true", help="weekly automatic run (no window)")
    ap.add_argument("--check", action="store_true", help="show which add-ons are working")
    ap.add_argument("--cli", action="store_true", help="run without the web page")
    ap.add_argument("--src", action="append", default=[], help="photo folder (repeatable)")
    ap.add_argument("--dest", default="", help="library folder")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-lookalikes", action="store_true", help="faster: skip look-alike check")
    ap.add_argument("--naming", choices=["keep", "date", "datetime"], default="keep")
    ap.add_argument("--no-heic-folder", action="store_true")
    ap.add_argument("--convert-heic", action="store_true", help="convert HEIC in the library to JPEG")
    ap.add_argument("--tag", action="store_true", help="add smart tags to the library at --dest")
    ap.add_argument("--faces", action="store_true", help="find and group faces in the library")
    ap.add_argument("--places", action="store_true", help="find where photos were taken (from stored location)")
    ap.add_argument("--verify", action="store_true", help="check originals are safely in the library")
    ap.add_argument("--thorough", action="store_true")
    ap.add_argument("--everything", action="store_true")
    a = ap.parse_args()
    if a.app or sys.stdout is None:
        _go_quiet()
    if a.app:
        if not HAVE_PIL:
            _fatal_box("Memory Lane is missing a needed add-on (Pillow). Run the Memory Lane Setup file again to repair it.")
            return
        try:
            serve(True)
        except Exception as e:
            print("Fatal:", repr(e))
            _fatal_box(f"Memory Lane could not start:\n\n{e}\n\nDetails are saved in:\n{CONFIG_DIR / 'memorylane.log'}")
        return
    if a.check:
        st = addon_status()
        names = {"heic": "iPhone (HEIC) photos", "tags": "Smart tags (AI)", "faces": "Find people (faces)", "places": "Photo locations (places)"}
        print("Add-on check:")
        for k in ("heic", "tags", "faces", "places"):
            print(f"   {'OK     ' if st[k] else 'MISSING'}  {names[k]}")
        return
    if a.auto:
        run_auto()
        return
    if not HAVE_PIL:
        print("Missing package. Run:  pip install pillow pillow-heif")
        sys.exit(1)
    if not a.cli:
        serve()
        return
    if a.convert_heic:
        print(HeicConverter(a.dest).run())
    elif a.tag:
        print(Tagger(a.dest).run())
    elif a.faces:
        print(FaceFinder(a.dest).run())
    elif a.places:
        print(Placer(a.dest).run())
    elif a.verify:
        print(Verifier(a.src, a.dest, a.thorough).run())
    elif a.everything:
        print(Pipeline({"sources": a.src, "dest": a.dest, "naming": a.naming,
                        "heic_folder": not a.no_heic_folder,
                        "lookalikes": not a.no_lookalikes}).run())
    else:
        if not a.src or not a.dest:
            ap.error("--cli needs --src and --dest")
        print(Organizer(a.src, a.dest, dry_run=a.dry_run, lookalikes=not a.no_lookalikes,
                        naming=a.naming, heic_folder=not a.no_heic_folder).run())


if __name__ == "__main__":
    main()
