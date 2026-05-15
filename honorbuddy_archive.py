#!/usr/bin/env python3
# ==============================================================================
#  HONORBUDDY ARCHIVE SYSTEM — Final Edition
#  Async Python engine (aiohttp) — replaces the 3 original PowerShell scripts
#
#  Improvements vs previous rewrite:
#    • Sync stdlib logging (no async log → useless overhead eliminated)
#    • Clean BFS crawl by levels (dedicated queue per depth)
#    • Real resume/checkpoint (--resume loads state and skips already done)
#    • Clean shutdown on SIGINT (partial save)
#    • Regex patterns compiled on module load (not on each call)
#    • --skip-meshes present (was in README but missing from code)
#    • Streamed downloads + atomic tmp-rename
#    • Aggressive deduplication (URL normalization before insert)
#    • Inline progress bar (no console flood)
#    • git clone: --depth=1 --no-tags, timeout 5 min
#    • Auto-install deps if missing
# ==============================================================================

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import signal
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlparse, urlunparse

# ─── Deps ────────────────────────────────────────────────────────────────────
def _ensure_deps() -> None:
    missing = []
    for pkg in ("aiohttp", "aiofiles"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    if missing:
        import subprocess
        print(f"[SETUP] Installation: {', '.join(missing)}")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])

_ensure_deps()

import aiohttp      # noqa: E402
import aiofiles     # type: ignore # noqa: E402

try:
    import colorama # type: ignore
    colorama.init(autoreset=True)
except ImportError:
    pass

# ─── Logging ─────────────────────────────────────────────────────────────────
_ANSI = {
    "reset": "\033[0m", "bold": "\033[1m",
    "cyan":  "\033[96m", "green":  "\033[92m",
    "yellow":"\033[93m", "red":    "\033[91m",
    "mag":   "\033[95m", "gray":   "\033[90m",
}

class _Fmt(logging.Formatter):
    _MAP = {
        logging.DEBUG:    _ANSI["gray"],
        logging.INFO:     _ANSI["cyan"],
        logging.WARNING:  _ANSI["yellow"],
        logging.ERROR:    _ANSI["red"],
        logging.CRITICAL: _ANSI["mag"],
    }
    _SUCCESS = 25
    def format(self, r: logging.LogRecord) -> str:
        lvl = r.levelname[:7].ljust(7)
        col = self._MAP.get(r.levelno, "")
        ts  = time.strftime("%H:%M:%S")
        return f"{col}[{ts}] [{lvl}] {r.getMessage()}{_ANSI['reset']}"

logging.addLevelName(25, "SUCCESS")

def _success(self, msg, *args, **kwargs):  # noqa: ANN001
    if self.isEnabledFor(25):
        self._log(25, msg, args, **kwargs)

logging.Logger.success = _success  # type: ignore[attr-defined]

def setup_logging(log_file: Optional[Path] = None) -> logging.Logger:
    logger = logging.getLogger("hba")
    logger.setLevel(logging.DEBUG)
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(_Fmt())
    logger.addHandler(ch)
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(fh)
    return logger

log = logging.getLogger("hba")

# ─── Progress bar (in-place, non-spammy) ─────────────────────────────────────
_last_pct = -1

def progress(done: int, total: int, label: str = "") -> None:
    global _last_pct
    if total == 0:
        return
    pct = int(done / total * 100)
    if pct == _last_pct:
        return
    _last_pct = pct
    bar = ("█" * (pct // 5)).ljust(20)
    print(f"\r  {_ANSI['cyan']}[{bar}] {pct:3d}% {done}/{total}  {label:<30}{_ANSI['reset']}",
          end="", flush=True)
    if done >= total:
        print()

# ══════════════════════════════════════════════════════════════════════════════
#  CONSTANTS & CONFIGURATION
# ══════════════════════════════════════════════════════════════════════════════

MODES: Dict[str, dict] = {
    "standard": {
        "crawl_depth": 3, "retries": 3, "timeout": 30,
        "max_per_search": 100,
        "sem_search": 25, "sem_download": 20, "sem_clone": 5,
        "desc": "Complete archive — balanced (~20-40 min)",
    },
    "aggressive": {
        "crawl_depth": 5, "retries": 4, "timeout": 45,
        "max_per_search": 150,
        "sem_search": 40, "sem_download": 30, "sem_clone": 8,
        "desc": "Max coverage (~40-70 min)",
    },
    "ultimate": {
        "crawl_depth": 7, "retries": 5, "timeout": 60,
        "max_per_search": 200,
        "sem_search": 60, "sem_download": 40, "sem_clone": 10,
        "desc": "EVERYTHING — leave no stone unturned (~70-120 min)",
    },
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]

@dataclass
class WoWVersion:
    name: str
    patch: str
    code: str
    builds: List[str]
    years: List[int]
    client_versions: List[str]

WOW_VERSIONS: List[WoWVersion] = [
    WoWVersion("Vanilla",               "1.12.1",  "vanilla", ["5875","6005"],  [2004,2005,2006], ["1.8","1.9","1.10","1.11","1.12"]),
    WoWVersion("Burning Crusade",        "2.4.3",   "tbc",     ["8606"],          [2007,2008],      ["2.0","2.1","2.2","2.3","2.4"]),
    WoWVersion("Wrath of the Lich King", "3.3.5a",  "wotlk",   ["12340"],         [2008,2009,2010], ["3.0","3.1","3.2","3.3"]),
    WoWVersion("Cataclysm",              "4.3.4",   "cata",    ["15595"],         [2010,2011],      ["4.0","4.1","4.2","4.3"]),
    WoWVersion("Mists of Pandaria",      "5.4.8",   "mop",     ["18414"],         [2012,2013],      ["5.0","5.1","5.2","5.3","5.4"]),
    WoWVersion("Warlords of Draenor",    "6.2.4",   "wod",     ["21742"],         [2014,2015],      ["6.0","6.1","6.2"]),
    WoWVersion("Legion",                 "7.3.5",   "legion",  ["26972"],         [2016,2017],      ["7.0","7.1","7.2","7.3"]),
    WoWVersion("Battle for Azeroth",     "8.3.7",   "bfa",     ["34220"],         [2018,2019],      ["8.0","8.1","8.2","8.3"]),
    WoWVersion("Shadowlands",            "9.2.7",   "sl",      ["45779"],         [2020,2021],      ["9.0","9.1","9.2"]),
    WoWVersion("Dragonflight",           "10.2.7",  "df",      ["54505"],         [2022,2023],      ["10.0","10.1","10.2"]),
]

PRIVATE_SERVERS = [
    {"name": "Northrend", "type": "WOTLK"},
    {"name": "Warmane",   "type": "Multi"},
    {"name": "Kronos",    "type": "Vanilla"},
    {"name": "Atlantiss", "type": "Cata"},
    {"name": "Tauri",     "type": "MOP"},
]

# ─── Patterns compiled once at module load ───────────────────────────────────

# Relevance scoring: (compiled pattern, weight)
_SCORE_PATS: List[Tuple[re.Pattern, int]] = [
    (re.compile(r"honorbuddy",                                             re.I), 100),
    (re.compile(r"hb\d+",                                                  re.I),  80),
    (re.compile(r"github\.com/[^/]+/(honorbuddy|hbrelog|singular)",        re.I),  90),
    (re.compile(r"singular(?!ity)|kicksprofile",                           re.I),  70),
    (re.compile(r"hbrelog",                                                re.I),  75),
    (re.compile(r"wow.{0,10}bot|bot.{0,10}wow",                           re.I),  70),
    (re.compile(r"quest.{0,10}profile|combat.{0,10}routine",              re.I),  60),
    (re.compile(r"nav(?:igation)?mesh|navmesh|hbmeshes|hbmesh",           re.I),  65),
    (re.compile(r"wotlk|cataclysm|mop|wod|legion|vanilla|tbc",           re.I),  50),
    (re.compile(r"archive\.org",                                           re.I),  40),
    (re.compile(r"wayback",                                                re.I),  35),
    (re.compile(r"download|archive|backup",                                re.I),  20),
    (re.compile(r"version|release|changelog",                             re.I),  15),
]

_LINK_RE     = re.compile(r'href=["\']([^"\'\s#][^"\']*)["\']', re.I)
_SKIP_DOM    = re.compile(r"(facebook|twitter|youtube|reddit|instagram|linkedin|tiktok|amazon)", re.I)
_SANITIZE_RE = re.compile(r'[<>:"/\\|?*=&%\x00-\x1f]+')
_DL_RE       = re.compile(r'\.(zip|7z|rar|exe|hbs|lua|xml)(?:[?#]|$)|\.git(?:/|$)', re.I)
_GIT_RE      = re.compile(r'\.git(?:/|$)|github\.com/[^/]+/[^/?#]+(?:\.git)?$', re.I)

# WoW code version detection pattern (compiled per version)
_VER_PATS: List[Tuple[WoWVersion, re.Pattern]] = []
for _v in WOW_VERSIONS:
    _parts = (
        [re.escape(_v.code), re.escape(_v.patch), re.escape(_v.name)]
        + [re.escape(b) for b in _v.builds]
    )
    _VER_PATS.append((_v, re.compile("|".join(_parts), re.I)))

# ══════════════════════════════════════════════════════════════════════════════
#  DATA MODEL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Asset:
    url:        str
    title:      str       = ""
    source:     str       = ""
    asset_type: str       = "Unknown"
    versions:   List[str] = field(default_factory=list)
    score:      int       = 0
    local_path: str       = ""
    file_size:  int       = 0

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Asset":
        return Asset(**{k: v for k, v in d.items() if k in Asset.__dataclass_fields__})  # type: ignore

@dataclass
class State:
    """Complete checkpoint — serializable to JSON."""
    phase:     int             = 0
    assets:    Dict[str, dict] = field(default_factory=dict)
    crawled:   Set[str]        = field(default_factory=set)
    downloaded:Set[str]        = field(default_factory=set)

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        data = {
            "phase":      self.phase,
            "assets":     self.assets,
            "crawled":    list(self.crawled),
            "downloaded": list(self.downloaded),
        }
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def load(path: Path) -> "State":
        data = json.loads(path.read_text(encoding="utf-8"))
        return State(
            phase=data.get("phase", 0),
            assets=data.get("assets", {}),
            crawled=set(data.get("crawled", [])),
            downloaded=set(data.get("downloaded", [])),
        )

# ══════════════════════════════════════════════════════════════════════════════
#  UTILITAIRES
# ══════════════════════════════════════════════════════════════════════════════

def score(url: str, content: str = "") -> int:
    text = url + " " + content
    return sum(w for pat, w in _SCORE_PATS if pat.search(text))

def classify(url: str) -> str:
    u = url.lower()
    if _GIT_RE.search(u):                                                return "Repository"
    if re.search(r'\.(zip|7z|rar)$', u) and re.search(r'mesh|nav|grid', u): return "MeshArchive"
    if re.search(r'\.(zip|7z)$',     u) and re.search(r'profile|hbs',   u): return "ProfileArchive"
    if re.search(r'\.(zip|7z)$',     u) and re.search(r'addon|lua',     u): return "AddonArchive"
    if re.search(r'\.(exe|zip|7z)$', u) and re.search(r'setup|install|honorbuddy', u): return "Installer"
    if re.search(r'\.(hbs|lua|xml)$',u):                                  return "ScriptFile"
    return "Archive"

def detect_versions(url: str, content: str = "") -> List[str]:
    text = url + " " + content
    return [v.name for v, pat in _VER_PATS if pat.search(text)]

def subdir(asset_type: str) -> str:
    return {
        "Repository":    "Repositories",
        "ProfileArchive":"Profiles",
        "MeshArchive":   "Meshes",
        "AddonArchive":  "Addons",
        "Installer":     "Installers",
        "ScriptFile":    "Profiles",
    }.get(asset_type, "Tools")

def safe_name(raw: str, fallback: str = "file") -> str:
    n = _SANITIZE_RE.sub("_", raw).strip("_. ")
    return (n[:200] if n else f"{fallback}_{random.randint(10000,99999)}.bin")

def normalize_url(url: str) -> str:
    """Normalizes for deduplication: strips trailing /, query, fragment."""
    try:
        p = urlparse(url)
        return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), "", "", ""))
    except Exception:
        return url.rstrip("/").lower()

def extract_links(html: str, base_url: str) -> List[str]:
    base = urlparse(base_url)
    out: List[str] = []
    for m in _LINK_RE.finditer(html):
        href = m.group(1).strip()
        if not href or href.startswith(("javascript:", "mailto:", "data:")):
            continue
        if _SKIP_DOM.search(href):
            continue
        if href.startswith("http"):
            out.append(href)
        elif href.startswith("//"):
            out.append(f"{base.scheme}:{href}")
        elif href.startswith("/"):
            out.append(f"{base.scheme}://{base.netloc}{href}")
    return out

# ══════════════════════════════════════════════════════════════════════════════
#  HTTP ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class Http:
    """
    Shared aiohttp session:
      - Global pool + per-host limits
      - Semaphore per domain (precise rate-limit)
      - Exponential retry + jitter
      - User-Agent rotation
      - Streamed download chunk-by-chunk, atomic rename
    """

    # Concurrency limits specific per host
    _HOST_LIMITS = {
        "api.github.com":   2,
        "archive.org":      4,
        "web.archive.org":  4,
    }

    def __init__(self, timeout: int, retries: int, gh_token: str = "") -> None:
        self._timeout  = aiohttp.ClientTimeout(total=timeout, connect=10, sock_read=timeout)
        self._retries  = retries
        self._gh_token = gh_token
        self._conn: Optional[aiohttp.TCPConnector] = None
        self._session: Optional[aiohttp.ClientSession] = None
        # Semaphores created in __aenter__ (requires an active event loop)
        self._sems: Optional[Dict[str, asyncio.Semaphore]] = None

    async def __aenter__(self) -> "Http":
        self._conn = aiohttp.TCPConnector(
            limit=150, limit_per_host=8,
            ttl_dns_cache=300, enable_cleanup_closed=True, ssl=False,
        )
        self._sems = defaultdict(lambda: asyncio.Semaphore(10))
        for host, lim in self._HOST_LIMITS.items():
            self._sems[host] = asyncio.Semaphore(lim)
        self._session = aiohttp.ClientSession(connector=self._conn, timeout=self._timeout)
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()
        await asyncio.sleep(0.25)

    def _sem(self, url: str) -> asyncio.Semaphore:
        assert self._sems is not None
        host = urlparse(url).netloc
        return self._sems[host]

    def _headers(self, url: str) -> dict:
        h: dict = {"User-Agent": random.choice(USER_AGENTS)}
        if "api.github.com" in url and self._gh_token:
            h["Authorization"] = f"token {self._gh_token}"
            h["Accept"]        = "application/vnd.github+json"
        return h

    async def get(self, url: str, *, json_mode: bool = False,
                  max_bytes: int = 512_000) -> Optional[str]:
        assert self._session is not None
        for attempt in range(1, self._retries + 1):
            try:
                async with self._sem(url):
                    async with self._session.get(url, headers=self._headers(url),
                                                  allow_redirects=True) as r:
                        if r.status == 429:
                            await asyncio.sleep(min(2 ** attempt, 60) + random.random())
                            continue
                        if r.status >= 400:
                            return None
                        if json_mode:
                            return await r.text(encoding="utf-8", errors="replace")
                        data = await r.content.read(max_bytes)
                        return data.decode("utf-8", errors="replace")
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                if attempt < self._retries:
                    await asyncio.sleep(2 ** attempt + random.random())
        return None

    async def download(self, url: str, dest: Path, chunk: int = 65_536) -> bool:
        assert self._session is not None
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        for attempt in range(1, self._retries + 1):
            try:
                async with self._sem(url):
                    async with self._session.get(url, headers=self._headers(url),
                                                  allow_redirects=True) as r:
                        if r.status != 200:
                            return False
                        async with aiofiles.open(tmp, "wb") as fh:
                            async for data in r.content.iter_chunked(chunk):
                                await fh.write(data)
                tmp.rename(dest)
                return True
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
                if attempt < self._retries:
                    await asyncio.sleep(2 ** attempt)
        return False

# ══════════════════════════════════════════════════════════════════════════════
#  QUERY GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def build_queries(include_addons: bool, include_private: bool,
                  include_meshes: bool) -> List[str]:
    q: Set[str] = set()
    for v in WOW_VERSIONS:
        for tmpl in [
            f"Honorbuddy {v.name} {v.patch} profiles",
            f"{v.code} WoW bot profiles repository",
            f"Honorbuddy {v.name} combat routines",
            f"{v.code} quest profiles Honorbuddy",
            f"Honorbuddy {v.patch} profiles archive",
            f"WoW {v.patch} Honorbuddy profiles",
            f"{v.code} dungeon profiles Honorbuddy",
            f"Honorbuddy {v.name} PvP profiles",
            f"Honorbuddy {v.name} leveling bot",
        ]:
            q.add(tmpl)
        for build in v.builds:
            q.add(f"Honorbuddy build {build} profiles")
            if include_meshes:
                q.add(f"hbmeshes {build} download")

    if include_meshes:
        q.update([
            "hbmeshes all versions download",
            "Honorbuddy navigation meshes archive",
            "WoW navmesh all patches zones",
            "hbmeshes github archive",
            "meshcompiler Honorbuddy versions",
        ])
    if include_addons:
        q.update([
            "Honorbuddy addons all versions",
            "bot addons Lua scripts WoW",
            "gathering addons bot integration",
        ])
    if include_private:
        for srv in PRIVATE_SERVERS:
            q.add(f"Honorbuddy {srv['name']} profiles {srv['type']}")
            q.add(f"Honorbuddy {srv['type']} server profiles")

    q.update([
        "Honorbuddy profiles .hbs github",
        "Singular combat routine WoW github",
        "HBRelog manager releases github",
        "QuestBehaviors Honorbuddy github",
        "Bossland GmbH Honorbuddy archive",
        "honorbuddy bot profiles archive",
        "honorbuddy installer download archive",
        "Honorbuddy Singular WoW bot",
        "honorbuddy quest profiles",
    ])
    return sorted(q)

# ══════════════════════════════════════════════════════════════════════════════
#  PHASE 0 — DISCOVERY
# ══════════════════════════════════════════════════════════════════════════════

async def _github(http: Http, query: str, per_page: int) -> List[Asset]:
    url  = f"https://api.github.com/search/repositories?q={quote(query)}&per_page={min(per_page,100)}&sort=updated"
    raw  = await http.get(url, json_mode=True)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        out: List[Asset] = []
        for item in data.get("items", []):
            clone = item.get("clone_url", "")
            if clone:
                out.append(Asset(url=clone, title=item.get("full_name",""),
                                 source="GitHub", score=score(clone)))
        return out
    except (json.JSONDecodeError, KeyError):
        return []


async def _archive(http: Http, query: str, rows: int) -> List[Asset]:
    url = f"https://archive.org/advancedsearch.php?q={quote(query)}&output=json&rows={min(rows,100)}&fl=identifier,title"
    raw = await http.get(url, json_mode=True)
    if not raw:
        return []
    try:
        data = json.loads(raw)
        out: List[Asset] = []
        for item in data.get("response", {}).get("docs", []):
            ident = item.get("identifier", "")
            if ident:
                iurl = f"https://archive.org/details/{ident}"
                out.append(Asset(url=iurl, title=item.get("title", ident),
                                 source="Archive.org", score=score(iurl)))
        return out
    except (json.JSONDecodeError, KeyError):
        return []


async def _wayback(http: Http, domain: str) -> List[Asset]:
    url = (
        f"https://web.archive.org/cdx/search/cdx"
        f"?url={domain}*&output=json&fl=timestamp,original"
        f"&filter=statuscode:200&collapse=urlkey&limit=300"
    )
    raw = await http.get(url, json_mode=True)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
        out: List[Asset] = []
        for row in rows[1:]:
            ts, orig = row[0], row[1]
            s = score(orig)
            if s > 10:
                wb = f"https://web.archive.org/web/{ts}/{orig}"
                out.append(Asset(url=wb, title=f"{orig} [{ts}]",
                                 source="Wayback", score=s))
        return out
    except Exception:
        return []


async def phase0_discovery(
    http: Http,
    queries: List[str],
    max_per_search: int,
    sem_search: int,
    state: State,
) -> None:
    log.info("━" * 62)
    log.info("  PHASE 0 : PARALLEL DISCOVERY")
    log.info(f"  {len(queries)} queries — concurrency {sem_search}")
    log.info("━" * 62)

    sem   = asyncio.Semaphore(sem_search)
    known = set(state.assets.keys())

    # Wayback domains
    wb_domains = [
        "downloads.buddyauth.com",
        "code.google.com/p/hbmeshes",
        "bosslandgmbh.eu",
        "honorbud.com",
    ]

    tasks: List[asyncio.Task] = []
    loop = asyncio.get_event_loop()

    async def _run(coro) -> List[Asset]:
        async with sem:
            return await coro

    for q in queries:
        tasks.append(loop.create_task(_run(_github(http, q, max_per_search))))
        if re.search(r"archive|download|installer|mesh|backup", q, re.I):
            tasks.append(loop.create_task(_run(_archive(http, q, max_per_search))))
    for d in wb_domains:
        tasks.append(loop.create_task(_run(_wayback(http, d))))

    total = len(tasks)
    done  = 0
    buf: List[Asset] = []

    for coro in asyncio.as_completed(tasks):
        result = await coro
        done  += 1
        progress(done, total, "Discovery")
        if isinstance(result, list):
            buf.extend(result)

    # Deduplicate and insert into state
    added = 0
    for a in buf:
        key = normalize_url(a.url)
        if key not in known and key not in state.assets:
            state.assets[key] = a.to_dict()
            known.add(key)
            added += 1

    log.success(f"  Discovery : {added} new targets ({len(state.assets)} total)")  # type: ignore[attr-defined]

# ══════════════════════════════════════════════════════════════════════════════
#  PHASE 1 — CRAWL BFS BY LEVELS
# ══════════════════════════════════════════════════════════════════════════════

async def _crawl_one(http: Http, url: str, min_link_score: int = 15) -> Tuple[str, str, List[str]]:
    """
    Crawls an HTML URL.
    Returns (url, content_head, new_links).
    Does not attempt to fetch .git or binary files.
    """
    if _DL_RE.search(url):
        return url, "", []  # Directly downloadable, no need to crawl

    raw = await http.get(url, max_bytes=200_000)
    if not raw:
        return url, "", []

    head    = raw[:5_000]
    # Filter: page must contain at least one Honorbuddy term to be worth crawling
    if not re.search(r"honorbuddy|hbrelog|singular|navmesh|hbmeshes", raw, re.I):
        return url, "", []

    links: List[str] = []
    for link in extract_links(raw, url):
        if score(link) >= min_link_score:
            links.append(link)

    return url, head, links


async def phase1_crawl(
    http: Http,
    max_depth: int,
    sem_crawl: int,
    state: State,
    checkpoint_path: Path,
) -> None:
    log.info("━" * 62)
    log.info("  PHASE 1 : CRAWL BFS")
    log.info(f"  {len(state.assets)} URLs — max depth {max_depth} — concurrency {sem_crawl}")
    log.info("━" * 62)

    # BFS Queue: all un-crawled URLs from the state
    sem    = asyncio.Semaphore(sem_crawl)
    known  = set(state.assets.keys())

    current_level: List[str] = [
        url for url in state.assets
        if url not in state.crawled and not _GIT_RE.search(url)
    ]

    for depth in range(1, max_depth + 1):
        if not current_level:
            break

        log.info(f"  Depth {depth}/{max_depth} — {len(current_level)} URLs")

        async def _bounded(url: str):
            async with sem:
                return await _crawl_one(http, url)

        results = await asyncio.gather(
            *[_bounded(u) for u in current_level],
            return_exceptions=True,
        )

        next_level: List[str] = []

        for res in results:
            if not isinstance(res, tuple):
                continue
            url, head, new_links = res
            state.crawled.add(normalize_url(url))

            # Enrich asset with content
            norm = normalize_url(url)
            if norm in state.assets:
                a = Asset.from_dict(state.assets[norm])
                if a.asset_type == "Unknown" or not a.asset_type:
                    a.asset_type = classify(url)
                if not a.versions:
                    a.versions = detect_versions(url, head)
                if head:
                    a.score = max(a.score, score(url, head))
                state.assets[norm] = a.to_dict()

            # New links to crawl
            for link in new_links:
                nkey = normalize_url(link)
                if nkey not in known:
                    a = Asset(url=link, title=f"D{depth}←{urlparse(url).netloc}",
                              source=f"Crawl-D{depth}", score=score(link),
                              asset_type=classify(link))
                    state.assets[nkey] = a.to_dict()
                    known.add(nkey)
                    if not _GIT_RE.search(link):
                        next_level.append(nkey)

        log.info(f"  → {len(state.assets)} assets, {len(next_level)} new links")
        # Intermediate checkpoint after each level
        state.phase = 1
        state.save(checkpoint_path)
        current_level = next_level

    # Final enrichment: types/versions from URL alone for non-crawled
    for key, d in state.assets.items():
        a = Asset.from_dict(d)
        if a.asset_type in ("Unknown", ""):
            a.asset_type = classify(a.url)
        if not a.versions:
            a.versions = detect_versions(a.url)
        state.assets[key] = a.to_dict()

    log.success(f"  Crawl finished — {len(state.assets)} structured assets")  # type: ignore[attr-defined]

# ══════════════════════════════════════════════════════════════════════════════
#  PHASE 2 — ACQUISITION
# ══════════════════════════════════════════════════════════════════════════════

async def _git_clone(url: str, dest: Path, sem: asyncio.Semaphore) -> bool:
    async with sem:
        if dest.exists() and any(dest.iterdir()):
            return True
        dest.mkdir(parents=True, exist_ok=True)
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "clone", "--depth=1", "--no-tags", "-q", url, str(dest),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=300)
            ok = proc.returncode == 0
            if not ok and dest.exists():
                import shutil
                shutil.rmtree(dest, ignore_errors=True)
            return ok
        except Exception:
            return False


def _build_dest(output: Path, a: Asset) -> Path:
    """Builds destination path based on version and type."""
    versions = a.versions or ["Unknown"]
    v_obj = next((v for v in WOW_VERSIONS if v.name in versions), None)
    ver_dir = (
        f"{v_obj.code}_{v_obj.patch}_{v_obj.name.replace(' ','_')}"
        if v_obj else "Unknown"
    )
    return output / ver_dir / subdir(a.asset_type)


async def _download_one(
    http: Http,
    a: Asset,
    output: Path,
    dl_sem: asyncio.Semaphore,
    git_sem: asyncio.Semaphore,
    state: State,
    checkpoint_path: Path,
) -> bool:
    norm = normalize_url(a.url)
    if norm in state.downloaded:
        return True

    dest_dir = _build_dest(output, a)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if a.asset_type == "Repository" and _GIT_RE.search(a.url):
        raw_name  = re.sub(r'\.git$', '', urlparse(a.url).path.rstrip("/").rsplit("/", 1)[-1])
        repo_name = safe_name(raw_name, "repo")
        dest      = dest_dir / repo_name
        ok = await _git_clone(a.url, dest, git_sem)
        if ok:
            a.local_path = str(dest)
            log.success(f"  ✓ Clone  {repo_name}")  # type: ignore[attr-defined]
    else:
        async with dl_sem:
            raw_name = urlparse(a.url).path.rstrip("/").rsplit("/", 1)[-1] or "payload"
            filename = safe_name(raw_name, "file")
            dest     = dest_dir / filename
            if dest.exists():
                a.local_path = str(dest)
                a.file_size  = dest.stat().st_size
                ok = True
            else:
                ok = await http.download(a.url, dest)
                if ok:
                    a.local_path = str(dest)
                    a.file_size  = dest.stat().st_size
                    log.success(f"  ✓ DL  {filename} ({a.file_size/1048576:.2f} MB)")  # type: ignore[attr-defined]

    if ok:
        state.assets[norm] = a.to_dict()
        state.downloaded.add(norm)
        state.save(checkpoint_path)
    return ok


async def phase2_download(
    http: Http,
    output: Path,
    min_score: int,
    sem_download: int,
    sem_clone: int,
    state: State,
    checkpoint_path: Path,
) -> int:
    log.info("━" * 62)
    log.info("  PHASE 2 : PARALLEL ACQUISITION")
    log.info("━" * 62)

    candidates = [
        Asset.from_dict(d) for d in state.assets.values()
        if d.get("score", 0) >= min_score
        and normalize_url(d["url"]) not in state.downloaded
    ]
    candidates.sort(key=lambda a: a.score, reverse=True)
    log.info(f"  {len(candidates)} eligible assets (score ≥ {min_score})")

    dl_sem  = asyncio.Semaphore(sem_download)
    git_sem = asyncio.Semaphore(sem_clone)

    results = await asyncio.gather(
        *[_download_one(http, a, output, dl_sem, git_sem, state, checkpoint_path)
          for a in candidates],
        return_exceptions=True,
    )
    count = sum(1 for r in results if r is True)
    log.success(f"  Acquisition : {count}/{len(candidates)} objects secured")  # type: ignore[attr-defined]
    return count

# ══════════════════════════════════════════════════════════════════════════════
#  PHASE 3 — DUAL-FORMAT DATABASE
# ══════════════════════════════════════════════════════════════════════════════

async def phase3_database(output: Path, state: State, elapsed: float) -> None:
    log.info("━" * 62)
    log.info("  PHASE 3 : DATABASE GENERATION")
    log.info("━" * 62)

    assets   = [Asset.from_dict(d) for d in state.assets.values()]
    by_ver   = defaultdict(list)
    by_type: Dict[str, int] = defaultdict(int)

    for a in assets:
        by_type[a.asset_type] += 1
        for v in (a.versions or ["Unknown"]):
            by_ver[v].append(a)

    # ── TXT ───────────────────────────────────────────────────────────────────
    txt = output / "VERSION_MAPPING_DATABASE.txt"
    m, s = divmod(int(elapsed), 60)
    async with aiofiles.open(txt, "w", encoding="utf-8") as f:
        await f.write("=" * 78 + "\n")
        await f.write("        HONORBUDDY MASTER INDEX\n")
        await f.write(f"        Generated on {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        await f.write(f"        Duration: {m}m {s}s | Assets: {len(assets)}\n")
        await f.write("=" * 78 + "\n\n")

        for ver in sorted(by_ver.keys()):
            ver_assets = by_ver[ver]
            by_t: Dict[str, List[Asset]] = defaultdict(list)
            for a in ver_assets:
                by_t[a.asset_type].append(a)
            await f.write(f"\n{'─'*78}\n{ver} ({len(ver_assets)} assets)\n{'─'*78}\n")
            for atype, group in sorted(by_t.items()):
                await f.write(f"  [{atype}] — {len(group)} files\n")
                for a in sorted(group, key=lambda x: x.score, reverse=True):
                    await f.write(f"    • {a.title or a.url}\n")
                    await f.write(f"      URL    : {a.url}\n")
                    if a.local_path:
                        await f.write(f"      Local  : {a.local_path}\n")
                    await f.write(f"      Source : {a.source}  Score : {a.score}\n")

    log.success(f"  TXT  → {txt}")  # type: ignore[attr-defined]

    # ── JSON ──────────────────────────────────────────────────────────────────
    json_path = output / "VERSION_MAPPING_DATABASE.json"
    payload = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "elapsed_seconds": round(elapsed),
        "total_assets": len(assets),
        "by_version": {
            ver: [a.to_dict() for a in lst]
            for ver, lst in sorted(by_ver.items())
        },
    }
    async with aiofiles.open(json_path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(payload, indent=2, ensure_ascii=False))

    log.success(f"  JSON → {json_path}")  # type: ignore[attr-defined]

    # ── STATS ─────────────────────────────────────────────────────────────────
    stats = output / "STATS.txt"
    async with aiofiles.open(stats, "w", encoding="utf-8") as f:
        await f.write(f"HonorbuddyArchive — Statistics\n")
        await f.write(f"Generated on {time.strftime('%Y-%m-%d %H:%M:%S')} — Duration {m}m {s}s\n\n")
        await f.write("BY TYPE:\n")
        for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
            await f.write(f"  {t:<20} {c:>5}\n")
        await f.write("\nBY VERSION:\n")
        for ver, lst in sorted(by_ver.items()):
            await f.write(f"  {ver:<30} {len(lst):>5}\n")
        await f.write(f"\nTOTAL : {len(assets)}\n")
        await f.write(f"DL OK  : {len(state.downloaded)}\n")

    log.success(f"  Stats → {stats}")  # type: ignore[attr-defined]

# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

async def main(args: argparse.Namespace) -> int:
    cfg = MODES[args.mode]
    output = Path(
        args.output_dir or
        f"Honorbuddy_ARCHIVE_{time.strftime('%Y%m%d_%H%M%S')}"
    )
    output.mkdir(parents=True, exist_ok=True)

    checkpoint = output / "checkpoint.json"
    log_file   = output / f"run_{time.strftime('%Y%m%d_%H%M%S')}.log"
    setup_logging(log_file)

    # ── Banner ────────────────────────────────────────────────────────────────
    ew = 62
    print(f"{_ANSI['mag']}╔{'═'*ew}╗")
    print(f"║{'HONORBUDDY ARCHIVE — Final Edition':^{ew}}║")
    print(f"║{'Mode: ' + args.mode + '  ' + cfg['desc']:^{ew}}║")
    print(f"╚{'═'*ew}╝{_ANSI['reset']}\n")

    # ── Resume or new start ───────────────────────────────────────────────────
    if args.resume and checkpoint.exists():
        log.info("  Resuming from checkpoint...")
        state = State.load(checkpoint)
        log.info(f"  {len(state.assets)} assets, {len(state.downloaded)} already downloaded")
    else:
        state = State()

    if not args.github_token:
        log.warning("  No GitHub token — expect API rate-limiting (10 req/min)")
        log.warning("  Tip: export GITHUB_TOKEN=ghp_xxxx  or --github-token")
    else:
        log.info("  GitHub token active — 5000 req/h unlocked")

    t0 = time.time()

    # ── Handle Ctrl+C ─────────────────────────────────────────────────────────
    _shutdown = asyncio.Event()

    def _sigint(*_):
        log.warning("\n  [SIGINT] Stop requested — saving in progress...")
        _shutdown.set()

    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, _sigint)
    except (NotImplementedError, RuntimeError):
        pass  # Windows fallback

    async with Http(cfg["timeout"], cfg["retries"], args.github_token) as http:

        # PHASE 0
        if state.phase < 1:
            queries = build_queries(
                include_addons=not args.skip_addons,
                include_private=not args.skip_private,
                include_meshes=not args.skip_meshes,
            )
            log.info(f"  {len(queries)} search patterns compiled")
            await phase0_discovery(http, queries, cfg["max_per_search"],
                                   cfg["sem_search"], state)
            state.phase = 1
            state.save(checkpoint)

        if _shutdown.is_set():
            state.save(checkpoint)
            return 130

        # PHASE 1
        if not args.skip_crawl and state.phase < 2:
            await phase1_crawl(http, cfg["crawl_depth"], cfg["sem_search"],
                               state, checkpoint)
            state.phase = 2
            state.save(checkpoint)

        if _shutdown.is_set():
            state.save(checkpoint)
            return 130

        # PHASE 2
        if not args.skip_download:
            await phase2_download(http, output, args.min_score,
                                   cfg["sem_download"], cfg["sem_clone"],
                                   state, checkpoint)
        else:
            log.warning("  Downloads skipped (--skip-download)")

        if _shutdown.is_set():
            state.save(checkpoint)
            return 130

    # PHASE 3
    elapsed = time.time() - t0
    await phase3_database(output, state, elapsed)

    # ── Final resume ──────────────────────────────────────────────────────────
    m, s = divmod(int(elapsed), 60)
    print(f"\n{_ANSI['green']}╔{'═'*ew}╗")
    print(f"║{'ARCHIVING COMPLETE':^{ew}}║")
    print(f"╚{'═'*ew}╝{_ANSI['reset']}")
    log.success(f"  Duration      : {m}m {s}s")  # type: ignore[attr-defined]
    log.success(f"  Assets        : {len(state.assets)}")  # type: ignore[attr-defined]
    log.success(f"  Secured       : {len(state.downloaded)}")  # type: ignore[attr-defined]
    log.info(f"  Vault         : {output.resolve()}")
    log.info(f"  Checkpoint    : {checkpoint}")
    return 0


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Honorbuddy Archive System — Final Edition",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(f"  {m:<12} {c['desc']}" for m, c in MODES.items()),
    )
    p.add_argument("--mode",         choices=list(MODES), default="standard",
                   help="Archive mode")
    p.add_argument("--output-dir",   default=None,
                   help="Output directory (default: auto-dated)")
    p.add_argument("--github-token", default=os.environ.get("GITHUB_TOKEN", ""),
                   metavar="TOKEN", help="GitHub API Token (or GITHUB_TOKEN env var)")
    p.add_argument("--min-score",    type=int, default=20,
                   help="Minimum relevance score to download (default: 20)")
    p.add_argument("--skip-private", action="store_true", help="Exclude private servers")
    p.add_argument("--skip-addons",  action="store_true", help="Exclude addons")
    p.add_argument("--skip-meshes",  action="store_true", help="Exclude meshes")
    p.add_argument("--skip-crawl",   action="store_true", help="Disable deep crawl")
    p.add_argument("--skip-download",action="store_true", help="Discovery + mapping only")
    p.add_argument("--resume",       action="store_true",
                   help="Resume from existing checkpoint in --output-dir")
    return p


if __name__ == "__main__":
    args = _parser().parse_args()
    try:
        sys.exit(asyncio.run(main(args)))
    except KeyboardInterrupt:
        sys.exit(130)
