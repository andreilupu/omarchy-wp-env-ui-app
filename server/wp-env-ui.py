#!/usr/bin/env python3
"""wp-env control panel.

Discovers every wp-env project (directory containing .wp-env.json) under the
configured roots, shows live status from Docker, and starts/stops/opens sites.
Stdlib only; serves on 127.0.0.1 and is meant to be opened via
omarchy-launch-webapp as an app-mode window (see bin/wp-env-ui).

Filesystem scans run in a background thread and the discovered project list is
persisted to ~/.local/state/wp-env-ui/projects.json, so /api/sites always
answers instantly — even on a cold start.

New sites can be created straight from the UI (POST /api/create): the server
writes <sites dir>/<slug>/.wp-env.json itself, picking a free port pair, and
starts the site.

Env overrides:
  WP_ENV_UI_PORT          listen port (default 8710)
  WP_ENV_UI_ROOTS         colon-separated scan roots (default ~/workspace)
  WP_ENV_SITES_DIR        where UI-created sites live
                          (default ~/workspace/wordpress/omarchy/sites)
  WP_ENV_UI_DOMAIN_BASE   suggestion base for auto-assigned domains (wp.site)
  WP_ENV_UI_CADDY_ADMIN   Caddy admin API (default http://127.0.0.1:2029)
"""

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HOME = Path.home()
PORT = int(os.environ.get("WP_ENV_UI_PORT", "8710"))
ROOTS = [
    Path(p).expanduser()
    for p in os.environ.get("WP_ENV_UI_ROOTS", "~/workspace").split(":")
    if p
]
SITES_DIR = Path(
    os.environ.get("WP_ENV_SITES_DIR", "~/workspace/wordpress/omarchy/sites")
).expanduser()
# UI-created sites must always be discoverable, even when the sites dir sits
# outside the configured scan roots.
if not any(SITES_DIR == r or r in SITES_DIR.parents for r in ROOTS):
    ROOTS.append(SITES_DIR)

SCAN_TTL = 300  # seconds between automatic filesystem rescans
JOB_TIMEOUT = 1800  # seconds before a wp-env start/stop is killed
LOG_LIMIT = 20000  # bytes of job log kept per project
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")

# Local real-TLD domains (see bin/wp-env-domains-setup): sites mapped to a
# domain are reverse-proxied by Caddy at https://<domain>, with routes synced
# through Caddy's admin API.
DOMAIN_BASE = os.environ.get("WP_ENV_UI_DOMAIN_BASE", "wp.site")
CADDY_ADMIN = os.environ.get("WP_ENV_UI_CADDY_ADMIN", "http://127.0.0.1:2029")
DOMAIN_RE = re.compile(
    r"(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}"
)

STATE_DIR = HOME / ".local/state/wp-env-ui"
FAV_FILE = STATE_DIR / "favorites.json"
HIDDEN_FILE = STATE_DIR / "hidden.json"
DOMAINS_FILE = STATE_DIR / "domains.json"
PROJECTS_FILE = STATE_DIR / "projects.json"

# Only these hosts may address the server (Host header) or script against it
# (Origin header). Blocks CSRF from random websites and DNS-rebinding.
ALLOWED_HOSTS = ("localhost", "127.0.0.1")

# .desktop launches don't source the shell profile; mise provides node/npx.
os.environ["PATH"] = os.pathsep.join(
    [
        str(HOME / ".local/share/mise/shims"),
        str(HOME / ".local/bin"),
        os.environ.get("PATH", ""),
    ]
)


def md5hex(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def scan_projects() -> list[Path]:
    dirs: set[Path] = set()
    for root in ROOTS:
        if not root.is_dir():
            continue
        find_cmd = [
            "find", str(root),
            "-type", "d",
            "(", "-name", "node_modules", "-o", "-name", ".git",
            "-o", "-name", ".wp-env", ")", "-prune",
            "-o", "-type", "f", "-name", ".wp-env.json", "-print",
        ]
        fd_cmd = [
            "fd", "--hidden", "--no-ignore", "--type", "f",
            "--exclude", "node_modules", "--exclude", ".git",
            "--exclude", ".wp-env",
            "--glob", ".wp-env.json", str(root),
        ]
        out = ""
        for cmd in (fd_cmd, find_cmd):
            try:
                out = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=60
                ).stdout
                break
            except FileNotFoundError:
                continue  # fd not installed — fall back to find
            except subprocess.TimeoutExpired:
                out = ""
                break  # the other tool won't be faster on the same tree
        for line in out.splitlines():
            dirs.add(Path(line).resolve().parent)
    return sorted(dirs)


def compose_project_names(project_dir: Path) -> list[str]:
    """Candidate docker-compose project names for a wp-env project.

    Current wp-env: wp-env-<dirname>-<md5(config path)[:8]>, compose-normalized
    (lowercase, invalid chars dropped). Legacy wp-env: full md5 of the config
    file path.
    """
    config_hash = md5hex(str(project_dir / ".wp-env.json"))
    descriptive = f"wp-env-{project_dir.name}-{config_hash[:8]}"
    descriptive = re.sub(r"[^a-z0-9_-]", "", descriptive.lower())
    return [descriptive, config_hash]


def parse_mapped_port(ports: str) -> int | None:
    """Host port mapped to the container's :80, from a `docker ps` Ports column."""
    match = re.search(r":(\d+)->80/tcp", ports)
    return int(match.group(1)) if match else None


def docker_ports_by_name() -> dict[str, str]:
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    result = {}
    for line in out.splitlines():
        name, _, ports = line.partition("\t")
        result[name] = ports
    return result


# CSI/OSC escape sequences plus carriage-return redraws (progress bars) —
# wp-env/npx output is full of them and they render as garbage in the UI.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)")


def clean_log_line(line: str) -> str:
    return ANSI_RE.sub("", line).replace("\r\n", "\n").replace("\r", "\n")


def project_info(project_dir: Path) -> dict:
    """Raw wp-env config files for the detail panel (None when absent)."""
    def read(name):
        try:
            data = json.loads((project_dir / name).read_text())
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None
    return {
        "config": read(".wp-env.json"),
        "override": read(".wp-env.override.json"),
    }


def config_port(project_dir: Path) -> int:
    port = 8888
    for name in (".wp-env.json", ".wp-env.override.json"):
        try:
            data = json.loads((project_dir / name).read_text())
            if isinstance(data.get("port"), int):
                port = data["port"]
        except (OSError, ValueError):
            pass
    return port


# ---------------------------------------------------------------------------
# State

CACHE = {"dirs": [], "scanned_at": 0.0, "scanning": False}
JOBS: dict[str, dict] = {}  # project path -> {action, state, log, rc, t}
LOCK = threading.Lock()


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def load_path_set(file: Path) -> set[str]:
    try:
        data = json.loads(file.read_text())
        return {str(p) for p in data} if isinstance(data, list) else set()
    except (OSError, ValueError):
        return set()


def set_membership(file: Path, path: str, member: bool) -> None:
    with LOCK:  # load-modify-save must not interleave between clients
        paths = load_path_set(file)
        if member:
            paths.add(path)
        else:
            paths.discard(path)
        atomic_write(file, json.dumps(sorted(paths)))


def load_favorites() -> set[str]:
    return load_path_set(FAV_FILE)


def set_favorite(path: str, favorite: bool) -> None:
    set_membership(FAV_FILE, path, favorite)


def load_hidden() -> set[str]:
    return load_path_set(HIDDEN_FILE)


def set_hidden(path: str, hidden: bool) -> None:
    set_membership(HIDDEN_FILE, path, hidden)


def load_domains() -> dict[str, str]:
    try:
        data = json.loads(DOMAINS_FILE.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def save_domain(path: str, domain: str | None) -> None:
    with LOCK:
        domains = load_domains()
        if domain:
            domains[path] = domain
        else:
            domains.pop(path, None)
        atomic_write(
            DOMAINS_FILE, json.dumps(domains, indent=2, sort_keys=True)
        )


# ---------------------------------------------------------------------------
# WordPress URL alignment: a proxied site must believe its URL is the domain,
# or it redirects visitors back to localhost:<port>. wp-env merges
# .wp-env.override.json over .wp-env.json, so we keep our WP_HOME/WP_SITEURL
# and an X-Forwarded-Proto mu-plugin there (applied on the next wp-env start).

MU_PLUGIN_NAME = ".wp-env-ui-proxy.php"
MU_PLUGIN_MAPPING = "wp-content/mu-plugins/wp-env-ui-proxy.php"
MU_PLUGIN_PHP = """<?php
/**
 * Managed by wp-env-ui: make WordPress work behind the local Caddy proxy
 * at https://<domain>.
 *
 * 1. Trust X-Forwarded-Proto so is_ssl() and canonical redirects see https.
 * 2. wp-env appends its internal port to any configured WP_HOME/WP_SITEURL
 *    (https://<domain>:8890); the proxy serves plain :443, so strip the
 *    port back off at read time. These filters run after wp-env's
 *    _config_wp_home/_config_wp_siteurl constant hooks.
 */
if ( isset( $_SERVER['HTTP_X_FORWARDED_PROTO'] ) && 'https' === $_SERVER['HTTP_X_FORWARDED_PROTO'] ) {
\t$_SERVER['HTTPS'] = 'on';
}

$wp_env_ui_strip_port = static function ( $url ) {
\tif ( is_string( $url ) ) {
\t\treturn preg_replace( '#^(https://[^/:]+):\\d+#', '$1', $url );
\t}
\treturn $url;
};
add_filter( 'option_home', $wp_env_ui_strip_port, PHP_INT_MAX );
add_filter( 'option_siteurl', $wp_env_ui_strip_port, PHP_INT_MAX );
"""


def apply_domain_override(project: Path, domain: str | None) -> None:
    """Add or remove our entries in the project's .wp-env.override.json,
    preserving anything else the user keeps there."""
    override = project / ".wp-env.override.json"
    try:
        data = json.loads(override.read_text())
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    config = data.setdefault("config", {})
    mappings = data.setdefault("mappings", {})
    if domain:
        url = f"https://{domain}"
        config["WP_HOME"] = url
        config["WP_SITEURL"] = url
        mappings[MU_PLUGIN_MAPPING] = f"./{MU_PLUGIN_NAME}"
        (project / MU_PLUGIN_NAME).write_text(MU_PLUGIN_PHP)
    else:
        for key in ("WP_HOME", "WP_SITEURL"):
            config.pop(key, None)
        mappings.pop(MU_PLUGIN_MAPPING, None)
        try:
            (project / MU_PLUGIN_NAME).unlink()
        except OSError:
            pass
    for key in ("config", "mappings"):
        if not data[key]:
            del data[key]
    if data:
        atomic_write(override, json.dumps(data, indent=2) + "\n")
    else:
        try:
            override.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Caddy reverse-proxy sync (admin API)

CADDY = {"last": None, "down_until": 0.0}


def caddy_routes(sites: list[dict]) -> list[dict]:
    routes = []
    for site in sites:
        if site.get("domain") and site["state"] == "running":
            routes.append(
                {
                    "match": [{"host": [site["domain"]]}],
                    "handle": [
                        {
                            "handler": "reverse_proxy",
                            "upstreams": [
                                {"dial": f"127.0.0.1:{site['port']}"}
                            ],
                            # Apache in the wp-env container issues its own
                            # redirects (e.g. /wp-admin -> /wp-admin/) with
                            # an http:// scheme; keep the client on https.
                            "headers": {
                                "response": {
                                    "replace": {
                                        "Location": [
                                            {
                                                "search_regexp": "^http://",
                                                "replace": "https://",
                                            }
                                        ]
                                    }
                                }
                            },
                        }
                    ],
                    "terminal": True,
                }
            )
    return routes


def caddy_available(timeout: float = 0.4) -> bool:
    try:
        with urllib.request.urlopen(CADDY_ADMIN + "/config/", timeout=timeout):
            return True
    except OSError:
        return False


HOSTS_HELPER = "/usr/local/lib/wp-env-ui/hosts-helper"


def hosts_helper(*args: str) -> bool:
    """Run the privileged /etc/hosts helper (sudoers rule installed by
    wp-env-domains-setup makes it passwordless). False when the setup
    hasn't been run or the call fails."""
    try:
        return subprocess.run(
            ["sudo", "-n", HOSTS_HELPER, *args],
            capture_output=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_READY_CACHE = {"value": False, "t": 0.0}


def domains_ready() -> bool:
    """The domains stack is usable: Caddy answers on its admin API and the
    passwordless /etc/hosts helper is installed. Cached briefly — the UI
    polls this, and each failed `sudo -n` probe would otherwise write a
    denial to the auth log."""
    now = time.time()
    with LOCK:
        if now - _READY_CACHE["t"] < 5:
            return _READY_CACHE["value"]
    value = caddy_available() and hosts_helper("check")
    with LOCK:
        _READY_CACHE.update(value=value, t=now)
    return value


def sync_caddy(sites: list[dict]) -> None:
    """Replace the wpenv server's routes when the desired set changed.
    Cheap no-op otherwise; backs off while Caddy is unreachable."""
    payload = json.dumps(caddy_routes(sites), sort_keys=True)
    now = time.time()
    with LOCK:
        if payload == CADDY["last"] or now < CADDY["down_until"]:
            return

    def work():
        req = urllib.request.Request(
            CADDY_ADMIN + "/config/apps/http/servers/wpenv/routes",
            data=payload.encode(),
            method="PATCH",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=2):
                pass
            with LOCK:
                CADDY["last"] = payload
                CADDY["down_until"] = 0.0
        except OSError:
            with LOCK:
                CADDY["down_until"] = time.time() + 30

    threading.Thread(target=work, daemon=True).start()


def load_cached_projects() -> list[Path]:
    try:
        data = json.loads(PROJECTS_FILE.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [Path(p) for p in data if (Path(p) / ".wp-env.json").is_file()]


def rescan_async() -> None:
    with LOCK:
        if CACHE["scanning"]:
            return
        CACHE["scanning"] = True

    def work():
        try:
            dirs = scan_projects()
        except Exception:
            dirs = None
        with LOCK:
            CACHE["scanning"] = False
            if dirs is not None:
                CACHE["dirs"] = dirs
                CACHE["scanned_at"] = time.time()
        if dirs is not None:
            try:
                atomic_write(PROJECTS_FILE, json.dumps([str(d) for d in dirs]))
            except OSError:
                pass

    threading.Thread(target=work, daemon=True).start()


def known_dirs(rescan: bool = False) -> list[Path]:
    """Current project list; never blocks on a filesystem scan."""
    with LOCK:
        stale = time.time() - CACHE["scanned_at"] > SCAN_TTL
        dirs = list(CACHE["dirs"])
    if rescan or stale:
        rescan_async()
    return dirs


def job_view(path: str) -> dict | None:
    with LOCK:
        job = JOBS.get(path)
        return dict(job) if job else None


def start_job(path: str, action: str) -> bool:
    with LOCK:
        job = JOBS.get(path)
        if job and job["state"] == "running":
            return False
        JOBS[path] = {
            "action": action, "state": "running", "log": "", "rc": None,
            "t": time.time(),
        }

    def work():
        try:
            proc = subprocess.Popen(
                ["npx", "@wordpress/env", action],
                cwd=path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True,
            )
        except OSError as exc:  # missing npx, bad cwd, …
            with LOCK:
                JOBS[path].update(
                    state="error", rc=1, log=f"{type(exc).__name__}: {exc}"
                )
            return
        killer = threading.Timer(JOB_TIMEOUT, proc.kill)
        killer.start()
        # Stream output so /api/log shows progress while the job runs.
        for line in proc.stdout:
            line = clean_log_line(line)
            with LOCK:
                JOBS[path]["log"] = (JOBS[path]["log"] + line)[-LOG_LIMIT:]
        rc = proc.wait()
        killer.cancel()
        with LOCK:
            if rc == -9:
                JOBS[path]["log"] += f"\n[killed after {JOB_TIMEOUT}s timeout]"
            JOBS[path].update(state="done" if rc == 0 else "error", rc=rc)

    threading.Thread(target=work, daemon=True).start()
    return True


def port_in_use(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def claimed_ports() -> set[int]:
    """Ports (and tests ports) named in every known project's configs."""
    ports = set()
    for d in known_dirs():
        for name in (".wp-env.json", ".wp-env.override.json"):
            try:
                data = json.loads((d / name).read_text())
            except (OSError, ValueError):
                continue
            for key in ("port", "testsPort"):
                if isinstance(data.get(key), int):
                    ports.add(data[key])
    return ports


def pick_free_port() -> int:
    """Lowest even port from 8888 up whose pair is neither claimed by a
    project config nor actually listening on the machine."""
    used = claimed_ports()
    port = 8888
    while (
        port in used or port + 1 in used
        or port_in_use(port) or port_in_use(port + 1)
    ):
        port += 2
    return port


def create_site(slug, port, start: bool = True) -> tuple[int, dict]:
    """Create <SITES_DIR>/<slug>/.wp-env.json and (optionally) start it.

    Returns (http status, response body).
    """
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        return 400, {
            "error": "slug must be lowercase letters/digits/hyphens "
                     "(starting with a letter or digit, max 41 chars)"
        }
    site_dir = SITES_DIR / slug
    if (site_dir / ".wp-env.json").is_file():
        return 409, {"error": f"site '{slug}' already exists"}
    if port is None:
        port = pick_free_port()
    elif not isinstance(port, int) or not 1024 <= port <= 65534:
        return 400, {"error": "port must be an integer between 1024 and 65534"}
    config = {"core": None, "port": port, "testsPort": port + 1}
    try:
        site_dir.mkdir(parents=True, exist_ok=True)
        atomic_write(
            site_dir / ".wp-env.json", json.dumps(config, indent=2) + "\n"
        )
    except OSError as exc:
        return 500, {"error": f"could not create site: {exc}"}
    with LOCK:  # visible in the UI immediately, without waiting for a rescan
        if site_dir not in CACHE["dirs"]:
            CACHE["dirs"] = sorted(CACHE["dirs"] + [site_dir])
    # When the local-domains proxy is set up, give new sites a domain from
    # the start so the first provision already uses https://<slug>.<base>.
    domain = None
    if domains_ready():
        candidate = f"{slug}.{DOMAIN_BASE}"
        if candidate not in load_domains().values():
            domain = candidate
            save_domain(str(site_dir), domain)
            apply_domain_override(site_dir, domain)
            hosts_helper("add", domain)
    if start:
        start_job(str(site_dir), "start")
    return 200, {
        "ok": True, "path": str(site_dir), "port": port, "domain": domain,
    }


def open_webapp(url: str) -> None:
    subprocess.Popen(
        ["omarchy-launch-webapp", url],
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def site_list(force_scan: bool = False, include_hidden: bool = False) -> list[dict]:
    ports_by_name = docker_ports_by_name()
    favorites = load_favorites()
    hidden = load_hidden()
    domains = load_domains()
    sites = []
    for d in known_dirs(force_scan):
        path = str(d)
        is_hidden = path in hidden
        if is_hidden and not include_hidden:
            continue
        running_port = None
        for project in compose_project_names(d):
            ports = ports_by_name.get(f"{project}-wordpress-1")
            if ports is not None:
                running_port = parse_mapped_port(ports) or config_port(d)
                break
        job = job_view(path)
        if job and job["state"] == "running":
            state = "starting" if job["action"] == "start" else "stopping"
        elif running_port:
            state = "running"
        else:
            state = "stopped"
        port = running_port or config_port(d)
        domain = domains.get(path)
        sites.append(
            {
                "path": path,
                "name": d.name,
                "port": port,
                "state": state,
                "favorite": path in favorites,
                "hidden": is_hidden,
                "domain": domain,
                "url": f"https://{domain}" if domain
                       else f"http://localhost:{port}",
                "job": None if not job else {
                    "action": job["action"], "state": job["state"],
                    "rc": job["rc"], "has_log": bool(job["log"]),
                },
            }
        )
    sites.sort(key=lambda s: (not s["favorite"], s["name"].lower()))
    sync_caddy(sites)
    return sites


# ---------------------------------------------------------------------------
# One-click domains setup: runs bin/wp-env-domains-setup as root. When a
# graphical polkit agent is running, pkexec pops the system password dialog;
# otherwise (Omarchy ships no polkit agent) a terminal window opens with a
# sudo prompt — either way the user never types a command.

SETUP_STATE = {"state": "idle", "log": ""}  # idle | running | done | error
SETUP_LOG_FILE = STATE_DIR / "domains-setup.log"


def domains_setup_script() -> Path | None:
    local = Path(__file__).resolve().parent.parent / "bin/wp-env-domains-setup"
    if local.is_file():
        return local
    packaged = Path("/usr/bin/wp-env-domains-setup")
    return packaged if packaged.is_file() else None


def polkit_agent_running() -> bool:
    try:
        return subprocess.run(
            ["pgrep", "-f",
             "polkit-gnome|polkit-kde|hyprpolkitagent|lxpolkit|"
             "polkit-mate|xfce-polkit"],
            capture_output=True, timeout=5,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_terminal() -> list[str] | None:
    preferred = os.environ.get("TERMINAL")
    for term in ([preferred] if preferred else []) + [
        "alacritty", "ghostty", "kitty", "foot", "wezterm",
    ]:
        if term and shutil.which(term):
            if os.path.basename(term) == "wezterm":
                return [term, "start", "--"]
            return [term, "-e"]
    return None


def start_domains_setup() -> tuple[int, dict]:
    if domains_ready():
        return 200, {"ok": True, "already_ready": True}
    with LOCK:
        if SETUP_STATE["state"] == "running":
            return 409, {"error": "setup is already running"}
        SETUP_STATE.update(state="running", log="")
    script = domains_setup_script()
    if script is None:
        with LOCK:
            SETUP_STATE.update(state="error", log="setup script not found")
        return 500, {"error": "wp-env-domains-setup not found"}

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        SETUP_LOG_FILE.unlink()
    except OSError:
        pass

    # The EXIT marker in the log file is how the watcher learns the outcome
    # of the detached run.
    def launch() -> tuple[list[str], str]:
        if polkit_agent_running():
            shell = (
                f"pkexec '{script}' >'{SETUP_LOG_FILE}' 2>&1; "
                f"echo \"EXIT:$?\" >>'{SETUP_LOG_FILE}'"
            )
            return ["sh", "-c", shell], "dialog"
        terminal = find_terminal()
        if terminal is None:
            raise OSError("no terminal emulator found")
        shell = (
            f"echo 'wp-env: setting up local domains (needs your "
            f"password)'; echo; "
            f"sudo '{script}' 2>&1 | tee '{SETUP_LOG_FILE}'; "
            f"rc=${{PIPESTATUS[0]}}; "
            f"echo \"EXIT:$rc\" >>'{SETUP_LOG_FILE}'; "
            f"if [ \"$rc\" -ne 0 ]; then echo; "
            f"read -rp 'Setup failed — press enter to close'; fi"
        )
        return terminal + ["bash", "-c", shell], "terminal"

    try:
        cmd, mode = launch()
        subprocess.Popen(
            cmd, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except OSError as exc:
        with LOCK:
            SETUP_STATE.update(state="error", log=str(exc))
        return 500, {"error": f"could not launch setup: {exc}"}

    def watch():
        deadline = time.time() + 600  # generous: includes typing a password
        while time.time() < deadline:
            time.sleep(2)
            try:
                log = SETUP_LOG_FILE.read_text()[-LOG_LIMIT:]
            except OSError:
                log = ""
            match = re.search(r"^EXIT:(\d+)$", log, re.MULTILINE)
            with LOCK:
                SETUP_STATE["log"] = log
                if match:
                    rc = int(match.group(1))
                    if rc == 0:
                        SETUP_STATE["state"] = "done"
                    else:
                        SETUP_STATE["state"] = "error"
                        if rc in (126, 127):  # dismissed / not authorized
                            SETUP_STATE["log"] += "\n(authentication was " \
                                "cancelled or refused)"
                    return
        with LOCK:
            if SETUP_STATE["state"] == "running":
                SETUP_STATE.update(state="error")
                SETUP_STATE["log"] += "\n(timed out)"

    threading.Thread(target=watch, daemon=True).start()
    return 200, {"ok": True, "mode": mode}


def domains_status() -> dict:
    with LOCK:
        state, log = SETUP_STATE["state"], SETUP_STATE["log"][-4000:]
    return {"proxy": domains_ready(), "setup": state, "log": log}


# ---------------------------------------------------------------------------
# Theming: the UI follows the active Omarchy theme by reading its palette
# and injecting CSS variables into the page; falls back to the page's own
# palette when no theme is present.

THEME_FILE = HOME / ".local/state/omarchy/current/theme/colors.toml"


def theme_css() -> str:
    try:
        text = THEME_FILE.read_text()
    except OSError:
        return ""
    colors = {}
    for line in text.splitlines():
        m = re.match(r'\s*([a-z_]+)\s*=\s*"([^"]+)"', line)
        if m:
            colors[m.group(1)] = m.group(2)
    fg, bg = colors.get("foreground"), colors.get("background")
    if not fg or not bg:
        return ""
    tokens = {
        "--bg": bg,
        "--panel": colors.get("dark_background", bg),
        "--fg": fg,
        "--muted": colors.get("muted", "#808080"),
        "--accent": colors.get("accent", fg),
        "--urgent": colors.get("red", "#a55555"),
        "--ok": colors.get("green", "#7aa77a"),
        "--warn": colors.get("yellow", "#c9a545"),
    }
    return (":root{" + ";".join(f"{k}:{v}" for k, v in tokens.items())
            + ";color-scheme:"
            + ("light" if colors.get("mode") == "light" else "dark") + "}")


# ---------------------------------------------------------------------------
# HTTP

PAGE_FILE = Path(__file__).resolve().parent / "page.html"


def render_page() -> str:
    try:
        page = PAGE_FILE.read_text()
    except OSError:
        return "<!doctype html><title>wp-env</title><p>page.html is missing"
    return (page
            .replace("__DOMAIN_BASE__", DOMAIN_BASE)
            .replace("/*__THEME__*/", theme_css()))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quiet
        pass

    def send(self, code, body, ctype="application/json", headers=()):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            for key, value in headers:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away mid-response

    def origin_ok(self) -> bool:
        """Reject cross-site requests (CSRF) and DNS-rebinding.

        Browsers send JSON POSTs with a text/plain content type without a
        CORS preflight, so any website could otherwise drive this API.
        """
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).hostname not in ALLOWED_HOSTS:
            return False
        return True

    def do_GET(self):
        if not self.origin_ok():
            self.send(403, {"error": "forbidden origin"})
            return
        path, _, query = self.path.partition("?")
        params = parse_qs(query)
        if path == "/":
            self.send(200, render_page().encode(), "text/html; charset=utf-8")
        elif path == "/favicon.ico":
            icon = Path(__file__).resolve().parent.parent / "icons/wordpress.png"
            try:
                self.send(200, icon.read_bytes(), "image/png")
            except OSError:
                self.send(404, {"error": "no icon"})
        elif path == "/api/ping":
            self.send(200, {"ok": True})
        elif path == "/api/sites":
            sites = site_list(
                force_scan="1" in params.get("refresh", []),
                include_hidden="1" in params.get("all", []),
            )
            with LOCK:
                scanning = CACHE["scanning"]
            self.send(
                200, sites, headers=[("X-Scanning", "1" if scanning else "0")]
            )
        elif path == "/api/domains-status":
            self.send(200, domains_status())
        elif path == "/api/info":
            target = params.get("path", [""])[0]
            if target not in {str(d) for d in known_dirs()}:
                self.send(403, {"error": "unknown project path"})
            else:
                self.send(200, project_info(Path(target)))
        elif path == "/api/log":
            target = params.get("path", [""])[0]
            job = job_view(target)
            if not job:  # no job yet this server run — empty, not an error
                self.send(200, {"action": None, "log": ""})
            else:
                self.send(200, {"action": job["action"], "log": job["log"]})
        else:
            self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.origin_ok():
            self.send(403, {"error": "forbidden origin"})
            return
        if self.path == "/api/domains-setup":
            code, body = start_domains_setup()
            self.send(code, body)
            return
        if self.path not in (
            "/api/action", "/api/favorite", "/api/hidden", "/api/domain",
            "/api/create",
        ):
            self.send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(length))
            if not isinstance(req, dict):
                raise ValueError("body must be a JSON object")
        except ValueError:
            self.send(400, {"error": "bad request"})
            return

        if self.path == "/api/create":
            code, body = create_site(
                req.get("slug"), req.get("port"),
                start=bool(req.get("start", True)),
            )
            self.send(code, body)
            return

        target = req.get("path")
        if not isinstance(target, str):
            self.send(400, {"error": "bad request"})
            return

        if target not in {str(d) for d in known_dirs()}:
            self.send(403, {"error": "unknown project path"})
            return

        if self.path == "/api/favorite":
            set_favorite(target, bool(req.get("favorite")))
            self.send(200, {"ok": True})
            return

        if self.path == "/api/hidden":
            set_hidden(target, bool(req.get("hidden")))
            self.send(200, {"ok": True})
            return

        if self.path == "/api/domain":
            domain = req.get("domain") or None
            if domain is not None:
                if not isinstance(domain, str) or not DOMAIN_RE.fullmatch(
                    domain := domain.strip().lower()
                ):
                    self.send(400, {"error": "invalid domain name"})
                    return
                taken = {
                    p: d for p, d in load_domains().items()
                    if d == domain and p != target
                }
                if taken:
                    self.send(
                        409,
                        {"error": f"domain already used by "
                                  f"{Path(next(iter(taken))).name}"},
                    )
                    return
            old_domain = load_domains().get(target)
            save_domain(target, domain)
            apply_domain_override(Path(target), domain)
            dns_ok = True
            if old_domain and old_domain != domain:
                hosts_helper("remove", old_domain)
            if domain:
                dns_ok = hosts_helper("add", domain)
            # A running site must be re-provisioned for the new WP_HOME /
            # WP_SITEURL to reach wp-config.php; wp-env start is idempotent.
            restarting = False
            for site in site_list(include_hidden=True):
                if site["path"] == target and site["state"] == "running":
                    restarting = start_job(target, "start")
            body = {"ok": True, "restarting": restarting}
            if not dns_ok:
                body["warning"] = (
                    "domain saved, but it won't resolve yet — run "
                    "\"Set up local domains\" first"
                )
            self.send(200, body)
            return

        action = req.get("action", "")

        if action in ("start", "stop"):
            if not start_job(target, action):
                self.send(409, {"error": "a job is already running"})
                return
        elif action in ("open", "open-admin"):
            url = None
            for site in site_list(include_hidden=True):
                if site["path"] == target and site["state"] == "running":
                    url = site["url"]
            if not url:
                self.send(409, {"error": "site is not running"})
                return
            suffix = "/wp-admin/" if action == "open-admin" else ""
            open_webapp(url + suffix)
        else:
            self.send(400, {"error": "unknown action"})
            return
        self.send(200, {"ok": True})


def main():
    # Serve the last known project list immediately; refresh in the background.
    with LOCK:
        CACHE["dirs"] = load_cached_projects()
    rescan_async()
    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        # Most likely another instance already owns the port (e.g. one started
        # by the launcher before the systemd service). Exit cleanly so systemd
        # doesn't restart-loop against it.
        print(f"cannot bind 127.0.0.1:{PORT}: {exc} — already running?")
        raise SystemExit(0)
    print(f"wp-env-ui listening on http://127.0.0.1:{PORT}")
    print(f"scan roots: {', '.join(str(r) for r in ROOTS)}")
    server.serve_forever()


if __name__ == "__main__":
    main()
