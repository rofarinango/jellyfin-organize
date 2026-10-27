#!/usr/bin/env python3
import json
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).parent
SOURCE_DIR = Path.home() / "Documents" / "Jellyfin Content"
SCRIPT = ROOT / "organize_jellyfin.sh"
CONFIG_FILE = ROOT / "config.json"
CACHE_FILE = ROOT / ".infer_cache.json"
REGISTRY_FILE = ROOT / ".sent_registry.json"
SSH_HOST = "jellyfin-pi"
PORT = 8787

JOBS = {}
JOBS_LOCK = threading.Lock()

IGNORE_FILES = {".DS_Store"}
RELEASE_TAGS = re.compile(
    r"\b(1080p|720p|2160p|4k|uhd|x264|x265|h264|h265|hevc|web-?dl|webrip|"
    r"bluray|blu-ray|hdtv|dvdrip|aac\d?|ac3|dts(?:-hd)?|remux|proper|repack|"
    r"amzn|nf|hulu|dsnp)\b",
    re.IGNORECASE,
)
SEASON_EP = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,3})")
YEAR = re.compile(r"\b(19\d{2}|20\d{2})\b")
FUZZY_SEASON = re.compile(r"(\d{1,2})([a-zA-Z]{4,})")
TRAILING_EP = re.compile(r"-(\d{1,3})\s*$")

INFER_SCHEMA = json.dumps(
    {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["movie", "show"]},
            "title": {"type": "string"},
            "year": {"type": "string"},
            "season": {"type": "integer"},
            "episode": {"type": "integer"},
            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        },
        "required": ["type", "title", "confidence"],
    }
)


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, indent=2))


def load_registry() -> dict:
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_registry(registry: dict):
    REGISTRY_FILE.write_text(json.dumps(registry, indent=2))


def mark_sent(filename: str, dest: str):
    registry = load_registry()
    registry[filename] = {
        "dest": dest,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }
    save_registry(registry)


def dedupe_repeats(s: str) -> str:
    return re.sub(r"(.)\1{2,}", r"\1", s)


def subsequence(small: str, big: str) -> bool:
    it = iter(big)
    return all(ch in it for ch in small)


def clean_title(raw: str) -> str:
    text = raw.replace(".", " ").replace("_", " ")
    text = RELEASE_TAGS.sub(" ", text)
    text = re.sub(r"[\[\(\{][^\]\)\}]*[\]\)\}]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -._")
    return text.strip()


def fuzzy_show_match(stem: str):
    deduped = dedupe_repeats(stem)
    ep_match = TRAILING_EP.search(deduped) or TRAILING_EP.search(stem)
    for m in FUZZY_SEASON.finditer(deduped):
        word = m.group(2).lower()
        if subsequence("season", word):
            season = int(m.group(1))
            episode = int(ep_match.group(1)) if ep_match else None
            title_source = deduped[: m.start()]
            return season, episode, title_source
    return None


def propose(filename: str) -> dict:
    stem = Path(filename).stem
    ext = Path(filename).suffix.lstrip(".")

    se_match = SEASON_EP.search(stem)
    if se_match:
        season, episode = int(se_match.group(1)), int(se_match.group(2))
        before = stem[: se_match.start()]
        year_match = YEAR.search(before)
        year = year_match.group(0) if year_match else ""
        title_source = before[: year_match.start()] if year_match else before
        title = clean_title(title_source)
        return {
            "type": "show",
            "title": title,
            "year": year,
            "season": season,
            "episode": episode,
            "ext": ext,
            "confidence": "high",
        }

    fuzzy = fuzzy_show_match(stem)
    if fuzzy and fuzzy[1] is not None:
        season, episode, title_source = fuzzy
        return {
            "type": "show",
            "title": clean_title(title_source),
            "year": "",
            "season": season,
            "episode": episode,
            "ext": ext,
            "confidence": "low",
        }

    year_match = YEAR.search(stem)
    if year_match:
        title = clean_title(stem[: year_match.start()])
        year = year_match.group(0)
        confidence = "high"
    else:
        title = clean_title(dedupe_repeats(stem))
        year = ""
        confidence = "low"

    return {
        "type": "movie",
        "title": title,
        "year": year,
        "season": "",
        "episode": "",
        "ext": ext,
        "confidence": confidence,
    }


def list_files() -> list:
    if not SOURCE_DIR.exists():
        return []
    registry = load_registry()
    out = []
    for p in sorted(SOURCE_DIR.iterdir()):
        if not p.is_file() or p.name in IGNORE_FILES:
            continue
        sent_info = registry.get(p.name)
        out.append(
            {
                "filename": p.name,
                "size_mb": round(p.stat().st_size / (1024 * 1024), 1),
                "proposal": propose(p.name),
                "sent": sent_info is not None,
                "sent_dest": sent_info["dest"] if sent_info else None,
            }
        )
    return out


def build_send_args(payload: dict):
    kind = payload.get("type")
    filename = payload.get("filename")
    title = payload.get("title", "").strip()
    year = str(payload.get("year", "")).strip()

    if not filename or not title or not year:
        return None, "Faltan campos obligatorios (archivo, título, año)."
    if not (SOURCE_DIR / filename).is_file():
        return None, f"No se encontró el archivo: {filename}"

    ext = Path(filename).suffix.lstrip(".")

    if kind == "movie":
        folder_name = f"{title} ({year})"
        dest = f"Movies/{folder_name}/{folder_name}.{ext}"
        return ["movie", filename, title, year], dest
    elif kind == "show":
        season = str(payload.get("season", "")).strip()
        episode = str(payload.get("episode", "")).strip()
        if not season or not episode:
            return None, "Faltan temporada/episodio."
        sp = f"{int(season):02d}"
        ep = f"{int(episode):02d}"
        folder_name = f"{title} ({year})"
        dest = f"Shows/{folder_name}/Season {sp}/{title} S{sp}E{ep}.{ext}"
        return ["show", filename, title, year, season, episode], dest
    else:
        return None, f"Tipo inválido: {kind}"


def run_send_job(job_id: str, filename: str, args: list, dest: str):
    job = JOBS[job_id]
    local_path = SOURCE_DIR / filename
    total_bytes = local_path.stat().st_size
    job["bytes_total"] = total_bytes

    try:
        proc = subprocess.Popen(
            [str(SCRIPT), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        job["status"] = "error"
        job["log"] += "No se encontró organize_jellyfin.sh\n"
        return

    tmp_remote = None
    log_lines = []

    while True:
        line = proc.stdout.readline()
        if not line:
            break
        log_lines.append(line)
        job["log"] = "".join(log_lines)
        if line.startswith("TMP_REMOTE:"):
            tmp_remote = line.strip().split("TMP_REMOTE:", 1)[1]
            break

    if tmp_remote:
        while proc.poll() is None:
            time.sleep(1)
            try:
                r = subprocess.run(
                    ["ssh", SSH_HOST, f"stat -c%s '{tmp_remote}' 2>/dev/null"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                size_str = r.stdout.strip()
                if size_str.isdigit():
                    done = int(size_str)
                    job["bytes_done"] = done
                    job["percent"] = min(99, round(done / total_bytes * 100)) if total_bytes else 0
            except Exception:
                pass

    remaining = proc.stdout.read()
    if remaining:
        log_lines.append(remaining)
        job["log"] = "".join(log_lines)
    proc.wait()

    if proc.returncode == 0:
        job["status"] = "done"
        job["percent"] = 100
        job["bytes_done"] = total_bytes
        mark_sent(filename, dest)
    else:
        job["status"] = "error"


def start_send_job(payload: dict) -> dict:
    args, dest_or_error = build_send_args(payload)
    if args is None:
        return {"success": False, "log": dest_or_error}

    job_id = str(uuid.uuid4())
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running",
            "percent": 0,
            "bytes_done": 0,
            "bytes_total": 0,
            "log": "",
        }

    thread = threading.Thread(
        target=run_send_job,
        args=(job_id, payload["filename"], args, dest_or_error),
        daemon=True,
    )
    thread.start()
    return {"success": True, "job_id": job_id}


def get_job_status(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if not job:
        return {"success": False, "log": "job_id desconocido"}
    return {"success": True, **job}


def delete_source(filename: str) -> dict:
    registry = load_registry()
    if filename not in registry:
        return {"success": False, "log": "Este archivo no está registrado como ya enviado, no se borra por seguridad."}

    path = SOURCE_DIR / filename
    if not path.is_file():
        return {"success": False, "log": "El archivo ya no existe en la carpeta local."}

    path.unlink()
    return {"success": True}


def infer_with_claude(filename: str) -> dict:
    cache = load_cache()
    if filename in cache:
        cached = cache[filename]
        cached["cached"] = True
        return cached

    prompt = (
        "Analiza este nombre de archivo de video, puede estar ofuscado a propósito "
        "con letras repetidas o caracteres de relleno: '" + filename + "'. "
        "Infiere si es película o episodio de serie, el título real de la obra, "
        "el año si se puede inferir con certeza razonable (si no, deja year vacío), "
        "y número de temporada/episodio si aplica. Responde solo los datos inferidos."
    )

    try:
        result = subprocess.run(
            [
                "claude", "-p", prompt,
                "--output-format", "json",
                "--json-schema", INFER_SCHEMA,
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "log": "Tiempo de espera agotado en la inferencia con IA."}
    except FileNotFoundError:
        return {"success": False, "log": "No se encontró el comando 'claude' en el PATH."}

    if result.returncode != 0:
        return {"success": False, "log": result.stderr or "Error ejecutando claude -p"}

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"success": False, "log": "No se pudo interpretar la respuesta de Claude."}

    structured = data.get("structured_output")
    if not structured:
        return {"success": False, "log": "Claude no devolvió datos estructurados."}

    response = {
        "success": True,
        "proposal": structured,
        "cost_usd": data.get("total_cost_usd"),
        "cached": False,
    }
    cache[filename] = response
    save_cache(cache)
    return response


def tmdb_request(path: str, params: dict) -> dict:
    config = load_config()
    api_key = config.get("tmdb_api_key", "").strip()
    if not api_key:
        return {"success": False, "log": "Falta configurar tmdb_api_key en config.json"}

    params = {**params, "api_key": api_key, "language": "en-US"}
    url = f"https://api.themoviedb.org/3{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return {"success": False, "log": f"Error consultando TMDB: {e}"}

    return {"success": True, "data": data}


def tmdb_search(query: str, kind: str) -> dict:
    endpoint = "/search/movie" if kind == "movie" else "/search/tv"
    result = tmdb_request(endpoint, {"query": query})
    if not result["success"]:
        return result

    results = []
    for r in result["data"].get("results", [])[:6]:
        if kind == "movie":
            title = r.get("title")
            date = r.get("release_date", "")
        else:
            title = r.get("name")
            date = r.get("first_air_date", "")
        year = date[:4] if date else ""
        results.append({"id": r.get("id"), "title": title, "year": year})

    return {"success": True, "results": results}


def tmdb_show_seasons(tv_id: int) -> dict:
    result = tmdb_request(f"/tv/{tv_id}", {})
    if not result["success"]:
        return result

    data = result["data"]
    seasons = [
        {
            "season_number": s.get("season_number"),
            "name": s.get("name"),
            "episode_count": s.get("episode_count"),
            "air_date": s.get("air_date"),
        }
        for s in data.get("seasons", [])
        if s.get("season_number") is not None
    ]
    first_air_year = (data.get("first_air_date") or "")[:4]
    return {
        "success": True,
        "series_title": data.get("name"),
        "first_air_year": first_air_year,
        "seasons": seasons,
    }


class Handler(BaseHTTPRequestHandler):
    def _json(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/api/files":
            self._json(200, {"source_dir": str(SOURCE_DIR), "files": list_files()})
        elif path == "/api/tmdb_search":
            query = qs.get("query", [""])[0]
            kind = qs.get("type", ["movie"])[0]
            if not query:
                self._json(400, {"success": False, "log": "Falta query"})
                return
            self._json(200, tmdb_search(query, kind))
        elif path == "/api/tmdb_seasons":
            tv_id = qs.get("tv_id", [""])[0]
            if not tv_id.isdigit():
                self._json(400, {"success": False, "log": "tv_id inválido"})
                return
            self._json(200, tmdb_show_seasons(int(tv_id)))
        elif path == "/api/send_status":
            job_id = qs.get("job_id", [""])[0]
            if not job_id:
                self._json(400, {"success": False, "log": "Falta job_id"})
                return
            self._json(200, get_job_status(job_id))
        elif path == "/" or path == "/index.html":
            html = (ROOT / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/send":
            self._json(200, start_send_job(self._read_json_body()))
        elif path == "/api/infer":
            payload = self._read_json_body()
            filename = payload.get("filename", "")
            if not filename:
                self._json(400, {"success": False, "log": "Falta filename"})
                return
            self._json(200, infer_with_claude(filename))
        elif path == "/api/delete_source":
            payload = self._read_json_body()
            filename = payload.get("filename", "")
            if not filename:
                self._json(400, {"success": False, "log": "Falta filename"})
                return
            self._json(200, delete_source(filename))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print(f"Carpeta fuente: {SOURCE_DIR}")
    print(f"Abrir en el navegador: http://localhost:{PORT}")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
