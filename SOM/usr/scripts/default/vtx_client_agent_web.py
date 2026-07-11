# vtx_client_agent.py — full agent (restored + hardened)
# - Loads INI from default/custom
# - Builds Authorization: Bearer + optional X-API-Key
# - Pulls configured files/dirs
# - Pushes files from var/customerdata
# - Tries multiple upload variants for server compatibility
from __future__ import annotations

import os
import sys
import time
import json
import typing as t
import urllib.parse
from pathlib import Path
from datetime import datetime

import requests
import configparser

# ---------- logging ----------
def debug(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[vtx-agent] {ts} {msg}", flush=True)

# ---------- config ----------
DEFAULT_ROOT = Path("/Applications/VTX Client/VTX")
DEFAULT_CFG_DEFAULT_WEB = DEFAULT_ROOT / "usr" / "config" / "default" / "vtx_client_web.ini"
DEFAULT_CFG_CUSTOM_WEB = DEFAULT_ROOT / "usr" / "config" / "custom" / "vtx_client_web.ini"
DEFAULT_CFG_DEFAULT_LEGACY = DEFAULT_ROOT / "usr" / "config" / "default" / "vtx_client.ini"
DEFAULT_CFG_CUSTOM_LEGACY = DEFAULT_ROOT / "usr" / "config" / "custom" / "vtx_client.ini"
DEFAULT_TOKEN_CACHE = DEFAULT_ROOT / "usr" / "config" / "default" / "auth_token.txt"

def load_cfg(explicit: Path | None = None) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=configparser.ExtendedInterpolation())
    path_order = []
    if explicit:
        path_order.append(Path(explicit))
    path_order.extend(
        [
            DEFAULT_CFG_CUSTOM_WEB,
            DEFAULT_CFG_DEFAULT_WEB,
            DEFAULT_CFG_CUSTOM_LEGACY,
            DEFAULT_CFG_DEFAULT_LEGACY,
        ]
    )

    for p in path_order:
        if p.is_file():
            cfg.read(p, encoding="utf-8")
            debug(f"Loaded config: {p}")
            return cfg

    # Last resort: tell the operator where we looked
    searched = ", ".join(str(p) for p in path_order)
    raise FileNotFoundError(f"client config not found (searched: {searched})")

def as_bool(val: str | bool | None, default: bool) -> bool:
    if isinstance(val, bool):
        return val
    if val is None:
        return default
    v = val.strip().lower()
    if v in ("1", "true", "yes", "on"): return True
    if v in ("0", "false", "no", "off"): return False
    return default

# ---------- HTTP client ----------
class ApiClient:
    def __init__(
        self,
        base_url: str,
        api_prefix: str = "/api",
        verify: bool | str = True,
        token: str | None = None,
        username: str | None = None,
        password: str | None = None,
        token_cache_path: Path | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Default prefix is /api if blank/omitted
        self.api_prefix = "/" + api_prefix.strip("/") if api_prefix.strip() else "/api"
        self.verify = verify
        self.timeout = timeout
        self.s = requests.Session()
        self.token = token
        self.username = (username or "").strip()
        self.password = password or ""
        self.token_cache_path = token_cache_path

        debug(f"ApiClient base={self.base_url} prefix={self.api_prefix} verify={self.verify} "
              f"auth={'present' if bool(token) else 'none'}")

    def _save_token(self, token: str) -> None:
        self.token = token
        if not self.token_cache_path:
            return
        try:
            self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_cache_path.write_text(token + "\n", encoding="utf-8")
        except Exception as e:
            debug(f"failed to persist auth token: {e}")

    def login(self) -> bool:
        if not self.username or not self.password:
            return False
        try:
            resp = self.s.post(
                self._url("/auth/login"),
                json={"username": self.username, "password": self.password},
                verify=self.verify,
                timeout=self.timeout,
                headers={"User-Agent": "vtx-client-agent/1.0"},
            )
            if resp.status_code != 200:
                debug(f"auth/login failed: {resp.status_code} {resp.text[:200]}")
                return False
            data = resp.json() if resp.content else {}
            token = str(data.get("access_token", "")).strip()
            if not token:
                debug("auth/login response missing access_token")
                return False
            self._save_token(token)
            debug("auth/login succeeded")
            return True
        except Exception as e:
            debug(f"auth/login exception: {e}")
            return False

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"User-Agent": "vtx-client-agent/1.0"}
        if self.token:
            # Server accepts Bearer; keep X-API-Key for backward compat
            h["Authorization"] = f"Bearer {self.token}"
            h["X-API-Key"] = self.token
        if extra:
            for k, v in extra.items():
                if v is not None:
                    h[k] = v
        return h

    def _url(self, path: str) -> str:
        # Health (and openapi) are not under /api prefix on this server
        if path.startswith("/health") or path.startswith("/openapi"):
            return f"{self.base_url}{path}"
        # Normal API endpoints
        path = "/" + path.lstrip("/")
        return f"{self.base_url}{self.api_prefix}{path}"

    def request(self, method: str, path: str, **kwargs) -> requests.Response:
        extra_headers = kwargs.pop("headers", None)
        allow_retry = kwargs.pop("_allow_auth_retry", True)
        headers = self._headers(extra_headers)
        url = self._url(path)
        debug(f"{method} {url} params={kwargs.get('params')} headers={{'Authorization': '<redacted>' if 'Authorization' in headers else 'none', 'X-API-Key': '<redacted>' if 'X-API-Key' in headers else 'none'}}")
        resp = self.s.request(method, url, headers=headers, verify=self.verify, timeout=self.timeout, **kwargs)
        debug(f"-> status {resp.status_code}")
        if (
            resp.status_code == 401
            and allow_retry
            and path != "/auth/login"
            and self.username
            and self.password
        ):
            debug("received 401; attempting credential re-login and one retry")
            if self.login():
                headers = self._headers(extra_headers)
                resp = self.s.request(method, url, headers=headers, verify=self.verify, timeout=self.timeout, **kwargs)
                debug(f"retry -> status {resp.status_code}")
        return resp

    def health(self) -> bool:
        try:
            r = self.request("GET", "/health")
            return r.status_code == 200
        except Exception as e:
            debug(f"health check failed: {e}")
            return False

# ---------- helpers ----------
def rel_path(root: Path, file_path: Path) -> str:
    rel = file_path.resolve().relative_to(root.resolve())
    return rel.as_posix()

def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


# ---------- time helpers (minimal, for pull-if-newer) ----------
def _parse_any_time_to_epoch(value):
    try:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip()
        if s.replace('.', '', 1).isdigit():
            return float(s)
        # RFC1123
        from email.utils import parsedate_to_datetime
        from datetime import timezone
        dt = parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except Exception:
        pass
    try:
        # ISO-8601
        from datetime import datetime, timezone
        s = str(value).strip()
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None

def _get_local_mtime_epoch(path: Path):
    import os
    try:
        return os.path.getmtime(path)
    except FileNotFoundError:
        return None

# ---------- pulls ----------
def list_manifest(api: ApiClient, pull_dirs: list[str], pull_files: list[str]) -> list:
    params: dict[str, str] = {}
    if pull_dirs:
        params["dirs"] = ",".join(pull_dirs)
    if pull_files:
        params["files"] = ",".join(pull_files)

    paths: list[str] = []
    try:
        r = api.request("GET", "/sync/manifest", params=params)
        if r.status_code != 200:
            debug(f"manifest HTTP {r.status_code}, falling back to configured pull_files")
            return pull_files[:]  # fallback

        # Try to parse the response
        try:
            data = r.json()
            items = []
            if isinstance(data, list):
                for it in data:
                    if isinstance(it, dict):
                        rel = it.get("rel") or it.get("path") or it.get("name")
                        mtime = _parse_any_time_to_epoch(it.get("mtime") or it.get("last_modified") or it.get("modified"))
                        if rel:
                            items.append({"rel": rel, "mtime": mtime})
                    elif isinstance(it, str):
                        items.append(it)
            elif isinstance(data, dict):
                # Wrapper keys or mapping path->mtime
                inner = None
                for k in ("paths","files","items","data"):
                    if k in data:
                        inner = data[k]
                        break
                if inner is not None and isinstance(inner, list):
                    for it in inner:
                        if isinstance(it, dict):
                            rel = it.get("rel") or it.get("path") or it.get("name")
                            mtime = _parse_any_time_to_epoch(it.get("mtime") or it.get("last_modified") or it.get("modified"))
                            if rel:
                                items.append({"rel": rel, "mtime": mtime})
                        elif isinstance(it, str):
                            items.append(it)
                else:
                    for k, v in data.items():
                        if isinstance(k, str):
                            items.append({"rel": k, "mtime": _parse_any_time_to_epoch(v)})
            if items:
                paths = items
                debug(f"manifest returned {len(paths)} items (mtime-aware)")
                return paths
        except Exception:
            pass
        try:
            data = r.json()
        except Exception as e:
            debug(f"manifest JSON parse failed: {e}; falling back")
            return pull_files[:]

        items: list[t.Any] = []
        if isinstance(data, dict):
            # Accept common shapes: {"files":[...]}, {"paths":[...]}, {"items":[...]}
            for k in ("files", "paths", "items", "results"):
                v = data.get(k)
                if isinstance(v, list):
                    items = v
                    break
            if not items and isinstance(data.get("data"), list):
                items = data["data"]
        elif isinstance(data, list):
            items = data
        else:
            debug("manifest: unexpected JSON type; falling back")
            return pull_files[:]

        # Normalize to a list of relative path strings
        for it in items:
            if isinstance(it, str):
                paths.append(it)
            elif isinstance(it, dict):
                rel = (
                    it.get("rel")
                    or it.get("path")
                    or it.get("relative_path")
                    or it.get("file")
                    or it.get("name")
                )
                if isinstance(rel, str) and rel.strip():
                    paths.append(rel.strip())

        if not paths:
            debug("manifest parsed 0 paths; falling back to configured pull_files")
            return pull_files[:]

        debug(f"manifest returned {len(paths)} paths (e.g. {paths[:3]})")
        return paths

    except Exception as e:
        debug(f"manifest request failed: {e}; falling back")
        return pull_files[:]

def download_one(api: ApiClient, root: Path, rel: str) -> bool:
    """Download a single file if the server copy is newer than local.

    We first rely on any manifest-provided mtime filtering in do_pulls(),
    but as a fallback we also look at HTTP headers (Last-Modified or
    X-File-Mtime) so that even a manifest without mtimes won't cause
    constant re-downloads of unchanged files.
    """
    rel_enc = urllib.parse.quote(rel, safe="/")
    r = api.request("GET", f"/sync/file/{rel_enc}", stream=True)
    dest = root / rel
    if r.status_code == 200:
        # Fallback mtime check using response headers
        local_epoch = _get_local_mtime_epoch(dest)
        hdr_mtime = r.headers.get("X-File-Mtime") or r.headers.get("Last-Modified")
        server_epoch = _parse_any_time_to_epoch(hdr_mtime) if hdr_mtime else None
        if (server_epoch is not None) and (local_epoch is not None) and (server_epoch <= local_epoch + 1e-6):
            debug(f"SKIP  {rel} (server not newer; via headers)")
            r.close()
            return True

        ensure_parent(dest)
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if chunk:
                    f.write(chunk)
        # Preserve server mtime locally if available
        if server_epoch is not None:
            try:
                os.utime(dest, (server_epoch, server_epoch))
            except Exception:
                pass
        debug(f"Pulled: {rel} -> {dest}")
        return True
    if r.status_code == 404:
        debug(f"Pull missing on server: {rel}")
        return False
    debug(f"Pull failed {rel}: {r.status_code} {r.text[:300]}")
    return False

def do_pulls(api: ApiClient, root: Path, cfg: configparser.ConfigParser) -> None:
    pull_dirs  = [s.strip() for s in cfg.get("sync", "pull_dirs" , fallback="").split(",") if s.strip()]
    pull_files = [s.strip() for s in cfg.get("sync", "pull_files", fallback="").split(",") if s.strip()]
    if not pull_dirs and not pull_files:
        debug("no pull_dirs/pull_files configured; skipping pulls")
        return
    manifest_paths = list_manifest(api, pull_dirs, pull_files)
    for item in manifest_paths:
        rel = item
        server_epoch = None
        if isinstance(item, dict):
            rel = item.get("rel") or item.get("path") or item.get("name")
            server_epoch = item.get("mtime")
        if rel:
            dest = root / rel
            local_epoch = _get_local_mtime_epoch(dest)
            if (server_epoch is not None) and (local_epoch is not None) and (server_epoch <= local_epoch + 1e-6):
                debug(f"SKIP  {rel} (server not newer)")
                continue
            download_one(api, root, rel)

# ---------- pushes ----------
def upload_file(api: ApiClient, root: Path, file_path: Path) -> bool:
    """Upload a file only if the local copy is newer than the server copy.

    We try multiple server upload variants and stop at first success (2xx).
    Before uploading, we *optionally* issue a HEAD request to see if the
    server already has a copy that is at least as new; if so, we skip.
    """
    rel = rel_path(root, file_path)
    local_epoch = _get_local_mtime_epoch(file_path)
    rel_enc = urllib.parse.quote(rel, safe="/")

    # Optional remote mtime check using HEAD /sync/file/{rel}
    try:
        r_head = api.request("HEAD", f"/sync/file/{rel_enc}")
        if r_head.status_code == 200:
            hdr_mtime = r_head.headers.get("X-File-Mtime") or r_head.headers.get("Last-Modified")
            remote_epoch = _parse_any_time_to_epoch(hdr_mtime) if hdr_mtime else None
            if (remote_epoch is not None) and (local_epoch is not None) and (remote_epoch >= local_epoch - 1e-6):
                debug(f"SKIP  {rel} (server not older; via HEAD headers)")
                return True
    except Exception:
        # If HEAD isn't supported or fails, fall back to normal upload behavior
        pass

    attempts = [
        {"method": "POST", "path": "/ingest",               "kind": "multipart", "params": None,           "form": {"path": rel}, "field": "file"},
        {"method": "POST", "path": "/ingest",               "kind": "octet",     "params": {"path": rel}},
        {"method": "PUT",  "path": f"/sync/file/{rel_enc}", "kind": "octet",     "params": None},
        {"method": "POST", "path": f"/sync/file/{rel_enc}", "kind": "octet",     "params": None},
        {"method": "POST", "path": "/sync/upload",          "kind": "multipart", "params": None,           "form": {"path": rel}, "field": "file"},
        {"method": "POST", "path": "/sync/upload",          "kind": "octet",     "params": {"path": rel}},
    ]

    for att in attempts:
        method = att["method"]; path = att["path"]; kind = att["kind"]; params = att.get("params")
        try:
            if kind == "multipart":
                form = dict(att.get("form") or {})
                field_name = att.get("field", "file")
                with file_path.open("rb") as f:
                    files = {field_name: (file_path.name, f, "application/octet-stream")}
                    debug(f"Trying {method} {path} (multipart) params={params} form_keys={list(form.keys())}")
                    resp = api.request(method, path, params=params, data=form, files=files)
            else:
                headers = {"Content-Type": "application/octet-stream"}
                with file_path.open("rb") as f:
                    data = f.read()
                    debug(f"Trying {method} {path} (octet) params={params} bytes={len(data)}")
                    resp = api.request(method, path, params=params, data=data, headers=headers)

            if 200 <= resp.status_code < 300:
                debug(f"Uploaded {rel} via {method} {path} -> {resp.status_code}")
                return True

            if resp.status_code in (404, 405, 415, 422):
                debug(f"Upload variant {method} {path} not supported ({resp.status_code}); trying next.")
                continue

            debug(f"Upload failed {rel} via {method} {path}: {resp.status_code} {resp.text[:300]}")
            return False

        except Exception as e:
            debug(f"Upload exception via {method} {path}: {e}")
            continue

    debug(f"Upload failed {rel}: all attempts exhausted.")
    return False

def do_pushes(api: ApiClient, root: Path, cfg: configparser.ConfigParser) -> None:
    push_delete = as_bool(cfg.get("client", "push_delete_after_upload", fallback="false"), False)
    src_dir = root / "var" / "customerdata"
    if not src_dir.exists():
        return
    for fp in sorted(src_dir.rglob("*")):
        if fp.is_file():
            ok = upload_file(api, root, fp)
            if ok and push_delete:
                try:
                    fp.unlink()
                    debug(f"Deleted after upload: {rel_path(root, fp)}")
                except Exception as e:
                    debug(f"Failed to delete {fp}: {e}")

# ---------- main ----------
def build_api_from_cfg(cfg: configparser.ConfigParser, root: Path) -> ApiClient:
    base_url = cfg.get("server", "base_url", fallback="").strip()
    if not base_url:
        raise RuntimeError("server.base_url is required in client.ini")

    # TLS verify: true/false or path to CA bundle
    verify_val = cfg.get("server", "verify_tls", fallback="true").strip()
    if verify_val.lower() in ("true", "1", "yes", "on"):
        verify: bool | str = True
    elif verify_val.lower() in ("false", "0", "no", "off"):
        verify = False
    else:
        # If a file path was provided, allow it
        verify = verify_val

    # Legacy token via api_key or api_key_file
    token = cfg.get("server", "api_key", fallback="").strip()
    if not token:
        key_path = cfg.get("server", "api_key_file", fallback=str(DEFAULT_ROOT / "usr" / "config" / "default" / "apikey.txt")).strip()
        key_p = Path(key_path)
        if not key_p.is_absolute():
            key_p = root / key_p
        try:
            token = key_p.read_text(encoding="utf-8").strip()
        except Exception as e:
            debug(f"Could not read api_key_file {key_p}: {e}")
            token = ""

    # Preferred auth: username/password login to gateway.
    username = cfg.get("auth", "username", fallback="").strip()
    password = cfg.get("auth", "password", fallback="")
    password_file = cfg.get("auth", "password_file", fallback="").strip()
    if not password and password_file:
        pw_p = Path(password_file)
        if not pw_p.is_absolute():
            pw_p = root / pw_p
        try:
            password = pw_p.read_text(encoding="utf-8").strip()
        except Exception as e:
            debug(f"Could not read password_file {pw_p}: {e}")
            password = ""

    token_cache_file = cfg.get("auth", "token_cache_file", fallback=str(DEFAULT_TOKEN_CACHE)).strip()
    token_cache_path = Path(token_cache_file)
    if not token_cache_path.is_absolute():
        token_cache_path = root / token_cache_path

    prefer_up = as_bool(cfg.get("auth", "prefer_username_password", fallback="true"), True)
    if not token and prefer_up:
        try:
            token = token_cache_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    api_prefix = cfg.get("server", "api_prefix", fallback="").strip() or "/api"

    return ApiClient(
        base_url=base_url,
        api_prefix=api_prefix,
        verify=verify,
        token=token,
        username=username,
        password=password,
        token_cache_path=token_cache_path,
    )

def main() -> int:
    try:
        cfg = load_cfg()
    except Exception as e:
        debug(str(e))
        return 1

    root = Path(cfg.get("client", "client_root", fallback=str(DEFAULT_ROOT))).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "var" / "customerdata").mkdir(parents=True, exist_ok=True)

    try:
        api = build_api_from_cfg(cfg, root)
    except Exception as e:
        debug(str(e))
        return 1

    if api.username and api.password and not api.token:
        api.login()

    poll = int(cfg.get("client", "poll_seconds", fallback="15"))
    debug(f"Starting loop: poll={poll}s root={root}")

    while True:
        try:
            api.health()
            do_pulls(api, root, cfg)
            do_pushes(api, root, cfg)
        except Exception as e:
            debug(f"cycle error: {e}")
        time.sleep(poll)

if __name__ == "__main__":
    sys.exit(main())
