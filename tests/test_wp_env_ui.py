"""Tests for server/wp-env-ui.py.

Stdlib-only (unittest). Run from the repo root with:

    python3 -m unittest discover -s tests -v

Covers the pure helpers (compose project names, port parsing, config
reading), state persistence (favorites, cached project list), and the HTTP
API end-to-end against a real server on an ephemeral port — including the
CSRF/DNS-rebinding origin checks.
"""

import importlib.util
import json
import subprocess
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


def load_module():
    spec = importlib.util.spec_from_file_location(
        "wp_env_ui", REPO / "server" / "wp-env-ui.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = load_module()


class ComposeProjectNamesTest(unittest.TestCase):
    def test_descriptive_and_legacy_names(self):
        d = Path("/home/user/workspace/my-site")
        legacy = mod.md5hex("/home/user/workspace/my-site/.wp-env.json")
        names = mod.compose_project_names(d)
        self.assertEqual(names, [f"wp-env-my-site-{legacy[:8]}", legacy])

    def test_invalid_chars_dropped_and_lowercased(self):
        d = Path("/tmp/Weird Näme.Dir")
        descriptive = mod.compose_project_names(d)[0]
        self.assertRegex(descriptive, r"^wp-env-[a-z0-9_-]*$")
        self.assertNotIn(" ", descriptive)
        self.assertNotIn(".", descriptive)


class ParseMappedPortTest(unittest.TestCase):
    def test_typical_docker_ps_output(self):
        self.assertEqual(
            mod.parse_mapped_port("0.0.0.0:8890->80/tcp, :::8890->80/tcp"),
            8890,
        )

    def test_no_web_mapping(self):
        self.assertIsNone(mod.parse_mapped_port("3306/tcp"))
        self.assertIsNone(mod.parse_mapped_port(""))
        # a mapping to a different container port must not match
        self.assertIsNone(mod.parse_mapped_port("0.0.0.0:8891->3306/tcp"))


class ConfigPortTest(unittest.TestCase):
    def test_default_base_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertEqual(mod.config_port(d), 8888)
            (d / ".wp-env.json").write_text('{"port": 9000}')
            self.assertEqual(mod.config_port(d), 9000)
            (d / ".wp-env.override.json").write_text('{"port": 9100}')
            self.assertEqual(mod.config_port(d), 9100)

    def test_garbage_config_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / ".wp-env.json").write_text("not json")
            self.assertEqual(mod.config_port(d), 8888)
            (d / ".wp-env.json").write_text('{"port": "8890"}')  # wrong type
            self.assertEqual(mod.config_port(d), 8888)


class FavoritesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = mod.FAV_FILE
        mod.FAV_FILE = Path(self.tmp.name) / "state" / "favorites.json"

    def tearDown(self):
        mod.FAV_FILE = self.orig
        self.tmp.cleanup()

    def test_round_trip(self):
        self.assertEqual(mod.load_favorites(), set())
        mod.set_favorite("/a", True)
        mod.set_favorite("/b", True)
        self.assertEqual(mod.load_favorites(), {"/a", "/b"})
        mod.set_favorite("/a", False)
        self.assertEqual(mod.load_favorites(), {"/b"})
        # file on disk is sorted JSON, no leftover temp file
        self.assertEqual(json.loads(mod.FAV_FILE.read_text()), ["/b"])
        self.assertEqual(
            [p.name for p in mod.FAV_FILE.parent.iterdir()],
            ["favorites.json"],
        )

    def test_corrupt_file_is_tolerated(self):
        mod.FAV_FILE.parent.mkdir(parents=True)
        mod.FAV_FILE.write_text("{broken")
        self.assertEqual(mod.load_favorites(), set())
        mod.FAV_FILE.write_text('{"not": "a list"}')
        self.assertEqual(mod.load_favorites(), set())


class CachedProjectsTest(unittest.TestCase):
    def test_only_existing_projects_survive(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            alive = t / "alive"
            alive.mkdir()
            (alive / ".wp-env.json").write_text("{}")
            no_config = t / "no-config"
            no_config.mkdir()
            orig = mod.PROJECTS_FILE
            mod.PROJECTS_FILE = t / "projects.json"
            try:
                mod.PROJECTS_FILE.write_text(
                    json.dumps([str(alive), str(no_config), str(t / "gone")])
                )
                self.assertEqual(mod.load_cached_projects(), [alive])
                mod.PROJECTS_FILE.write_text("nonsense")
                self.assertEqual(mod.load_cached_projects(), [])
            finally:
                mod.PROJECTS_FILE = orig


class ScanProjectsTest(unittest.TestCase):
    def test_finds_projects_and_honors_excludes(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "site-a").mkdir()
            (t / "site-a/.wp-env.json").write_text("{}")
            nested = t / "plugins/site-b"
            nested.mkdir(parents=True)
            (nested / ".wp-env.json").write_text("{}")
            for excluded in ("node_modules/pkg", ".git/x", ".wp-env/y"):
                d = t / "site-a" / excluded
                d.mkdir(parents=True)
                (d / ".wp-env.json").write_text("{}")
            orig = mod.ROOTS
            mod.ROOTS = [t]
            try:
                names = sorted(d.name for d in mod.scan_projects())
            finally:
                mod.ROOTS = orig
            self.assertEqual(names, ["site-a", "site-b"])


class JobGuardTest(unittest.TestCase):
    def test_running_job_blocks_second_job(self):
        path = "/nonexistent/job-guard-test"
        with mod.LOCK:
            mod.JOBS[path] = {
                "action": "start", "state": "running", "log": "",
                "rc": None, "t": time.time(),
            }
        try:
            self.assertFalse(mod.start_job(path, "start"))
            self.assertFalse(mod.start_job(path, "stop"))
        finally:
            with mod.LOCK:
                del mod.JOBS[path]

    def test_job_view_returns_a_copy(self):
        path = "/nonexistent/job-view-test"
        with mod.LOCK:
            mod.JOBS[path] = {
                "action": "start", "state": "running", "log": "x",
                "rc": None, "t": 0,
            }
        try:
            view = mod.job_view(path)
            view["state"] = "mutated"
            self.assertEqual(mod.JOBS[path]["state"], "running")
            self.assertIsNone(mod.job_view("/never-seen"))
        finally:
            with mod.LOCK:
                del mod.JOBS[path]


class PickFreePortTest(unittest.TestCase):
    def setUp(self):
        self.saved = (mod.known_dirs, mod.port_in_use)

    def tearDown(self):
        mod.known_dirs, mod.port_in_use = self.saved

    def test_skips_claimed_and_listening_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / ".wp-env.json").write_text(
                '{"port": 8888, "testsPort": 8889}'
            )
            mod.known_dirs = lambda rescan=False: [d]
            mod.port_in_use = lambda p: p in (8890, 8891)
            self.assertEqual(mod.pick_free_port(), 8892)

    def test_default_when_nothing_used(self):
        mod.known_dirs = lambda rescan=False: []
        mod.port_in_use = lambda p: False
        self.assertEqual(mod.pick_free_port(), 8888)

    def test_odd_test_port_blocks_the_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / ".wp-env.json").write_text('{"testsPort": 8891}')
            mod.known_dirs = lambda rescan=False: [d]
            mod.port_in_use = lambda p: False
            self.assertEqual(mod.pick_free_port(), 8888)
            (d / ".wp-env.json").write_text('{"testsPort": 8889}')
            self.assertEqual(mod.pick_free_port(), 8890)


class CreateSiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (
            mod.SITES_DIR, mod.port_in_use, mod.start_job,
            mod.domains_ready, mod.DOMAINS_FILE, dict(mod.CACHE),
        )
        mod.SITES_DIR = Path(self.tmp.name) / "sites"
        mod.DOMAINS_FILE = Path(self.tmp.name) / "domains.json"
        mod.port_in_use = lambda p: False
        mod.domains_ready = lambda: False
        self.saved_hosts_helper = mod.hosts_helper
        mod.hosts_helper = lambda *a: True
        self.started = []
        mod.start_job = lambda path, action: self.started.append(
            (path, action)
        )
        with mod.LOCK:
            mod.CACHE["dirs"] = []
            mod.CACHE["scanned_at"] = time.time()

    def tearDown(self):
        (mod.SITES_DIR, mod.port_in_use, mod.start_job,
         mod.domains_ready, mod.DOMAINS_FILE, cache) = self.saved
        mod.hosts_helper = self.saved_hosts_helper
        with mod.LOCK:
            mod.CACHE.update(cache)
        self.tmp.cleanup()

    def test_creates_config_and_starts(self):
        code, body = mod.create_site("my-site", None)
        self.assertEqual(code, 200)
        self.assertEqual(body["port"], 8888)
        config = json.loads(
            (mod.SITES_DIR / "my-site" / ".wp-env.json").read_text()
        )
        self.assertEqual(
            config, {"core": None, "port": 8888, "testsPort": 8889}
        )
        self.assertEqual(
            self.started, [(str(mod.SITES_DIR / "my-site"), "start")]
        )
        # immediately visible without waiting for a rescan
        self.assertIn(mod.SITES_DIR / "my-site", mod.known_dirs())

    def test_start_false_skips_job(self):
        code, _ = mod.create_site("quiet-site", None, start=False)
        self.assertEqual(code, 200)
        self.assertEqual(self.started, [])

    def test_explicit_port_is_used(self):
        code, body = mod.create_site("ported", 9200)
        self.assertEqual(code, 200)
        self.assertEqual(body["port"], 9200)
        config = json.loads(
            (mod.SITES_DIR / "ported" / ".wp-env.json").read_text()
        )
        self.assertEqual(config["testsPort"], 9201)

    def test_invalid_slugs_rejected(self):
        for slug in ("", "My-Site", "-dash", "a b", "a/b", "..", None,
                     ["x"], "x" * 42):
            code, body = mod.create_site(slug, None)
            self.assertEqual(code, 400, f"slug {slug!r} should be rejected")
            self.assertIn("slug", body["error"])
        self.assertEqual(list(mod.SITES_DIR.glob("*")) if
                         mod.SITES_DIR.exists() else [], [])

    def test_invalid_port_rejected(self):
        for port in (80, 70000, "8888", 8888.5):
            code, _ = mod.create_site("some-site", port)
            self.assertEqual(code, 400, f"port {port!r} should be rejected")

    def test_duplicate_rejected(self):
        self.assertEqual(mod.create_site("twice", None)[0], 200)
        code, body = mod.create_site("twice", None)
        self.assertEqual(code, 409)
        self.assertIn("already exists", body["error"])

    def test_second_site_gets_next_port_pair(self):
        mod.create_site("first", None)
        code, body = mod.create_site("second", None)
        self.assertEqual((code, body["port"]), (200, 8890))

    def test_domain_auto_assigned_when_proxy_available(self):
        mod.domains_ready = lambda: True
        code, body = mod.create_site("proxied", None)
        self.assertEqual(code, 200)
        self.assertEqual(body["domain"], f"proxied.{mod.DOMAIN_BASE}")
        self.assertEqual(
            mod.load_domains(),
            {str(mod.SITES_DIR / "proxied"): f"proxied.{mod.DOMAIN_BASE}"},
        )
        override = json.loads(
            (mod.SITES_DIR / "proxied" / ".wp-env.override.json").read_text()
        )
        self.assertEqual(
            override["config"]["WP_HOME"],
            f"https://proxied.{mod.DOMAIN_BASE}",
        )

    def test_no_domain_without_proxy(self):
        code, body = mod.create_site("plain", None)
        self.assertEqual(code, 200)
        self.assertIsNone(body["domain"])
        self.assertEqual(mod.load_domains(), {})


class DockerStateTest(unittest.TestCase):
    """docker_ports_by_name records why Docker is unusable."""

    def tearDown(self):
        mod.DOCKER_STATE["status"] = "ok"

    def test_missing_binary(self):
        with mock.patch.object(
            mod.subprocess, "run", side_effect=FileNotFoundError("docker")
        ):
            self.assertEqual(mod.docker_ports_by_name(), {})
        self.assertEqual(mod.DOCKER_STATE["status"], "missing")

    def test_daemon_down(self):
        failed = subprocess.CompletedProcess(
            ["docker"], returncode=1, stdout="",
            stderr="Cannot connect to the Docker daemon",
        )
        with mock.patch.object(mod.subprocess, "run", return_value=failed):
            self.assertEqual(mod.docker_ports_by_name(), {})
        self.assertEqual(mod.DOCKER_STATE["status"], "down")

    def test_timeout_counts_as_down(self):
        with mock.patch.object(
            mod.subprocess, "run",
            side_effect=subprocess.TimeoutExpired(["docker"], 10),
        ):
            self.assertEqual(mod.docker_ports_by_name(), {})
        self.assertEqual(mod.DOCKER_STATE["status"], "down")

    def test_ok_resets_state(self):
        mod.DOCKER_STATE["status"] = "down"
        good = subprocess.CompletedProcess(
            ["docker"], returncode=0, stdout="c1-wordpress-1\t:80\n", stderr=""
        )
        with mock.patch.object(mod.subprocess, "run", return_value=good):
            self.assertEqual(
                mod.docker_ports_by_name(), {"c1-wordpress-1": ":80"}
            )
        self.assertEqual(mod.DOCKER_STATE["status"], "ok")


class CleanLogLineTest(unittest.TestCase):
    def test_strips_ansi_and_carriage_returns(self):
        self.assertEqual(
            mod.clean_log_line("\x1b[32mok\x1b[0m done\r\n"), "ok done\n"
        )
        self.assertEqual(
            mod.clean_log_line("progress 1\rprogress 2\r"),
            "progress 1\nprogress 2\n",
        )
        self.assertEqual(
            mod.clean_log_line("\x1b]0;title\x07plain"), "plain"
        )
        self.assertEqual(mod.clean_log_line("no escapes"), "no escapes")


class ProjectInfoTest(unittest.TestCase):
    def test_reads_config_and_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / ".wp-env.json").write_text('{"port": 9000, "core": null}')
            info = mod.project_info(d)
            self.assertEqual(info["config"]["port"], 9000)
            self.assertIsNone(info["override"])
            (d / ".wp-env.override.json").write_text('{"phpVersion": "8.3"}')
            info = mod.project_info(d)
            self.assertEqual(info["override"]["phpVersion"], "8.3")

    def test_tolerates_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / ".wp-env.json").write_text("not json")
            self.assertEqual(
                mod.project_info(d), {"config": None, "override": None}
            )


class DomainOverrideTest(unittest.TestCase):
    def test_create_and_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            mod.apply_domain_override(project, "my.wp.site")
            override = json.loads(
                (project / ".wp-env.override.json").read_text()
            )
            self.assertEqual(
                override["config"],
                {"WP_HOME": "https://my.wp.site",
                 "WP_SITEURL": "https://my.wp.site"},
            )
            self.assertEqual(
                override["mappings"][mod.MU_PLUGIN_MAPPING],
                f"./{mod.MU_PLUGIN_NAME}",
            )
            self.assertIn(
                "HTTP_X_FORWARDED_PROTO",
                (project / mod.MU_PLUGIN_NAME).read_text(),
            )
            mod.apply_domain_override(project, None)
            self.assertFalse((project / ".wp-env.override.json").exists())
            self.assertFalse((project / mod.MU_PLUGIN_NAME).exists())

    def test_preserves_user_override_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".wp-env.override.json").write_text(json.dumps({
                "phpVersion": "8.2",
                "config": {"WP_DEBUG": True},
                "mappings": {"wp-content/plugins/x": "./x"},
            }))
            mod.apply_domain_override(project, "my.wp.site")
            override = json.loads(
                (project / ".wp-env.override.json").read_text()
            )
            self.assertEqual(override["phpVersion"], "8.2")
            self.assertTrue(override["config"]["WP_DEBUG"])
            self.assertEqual(override["config"]["WP_HOME"],
                             "https://my.wp.site")
            mod.apply_domain_override(project, None)
            override = json.loads(
                (project / ".wp-env.override.json").read_text()
            )
            self.assertEqual(override, {
                "phpVersion": "8.2",
                "config": {"WP_DEBUG": True},
                "mappings": {"wp-content/plugins/x": "./x"},
            })


class HostsHelperTest(unittest.TestCase):
    """Runs the real libexec/wp-env-hosts-helper against a temp hosts file."""

    HELPER = REPO / "libexec" / "wp-env-hosts-helper"

    def run_helper(self, hosts, *args):
        import subprocess
        return subprocess.run(
            ["bash", str(self.HELPER), *args],
            env={**__import__("os").environ, "WP_ENV_HOSTS_FILE": str(hosts)},
            capture_output=True, text=True, timeout=10,
        )

    def test_add_remove_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            hosts = Path(tmp) / "hosts"
            hosts.write_text("127.0.0.1 localhost\n")
            self.assertEqual(
                self.run_helper(hosts, "add", "my-project.site").returncode, 0
            )
            content = hosts.read_text()
            self.assertIn("127.0.0.2 my-project.site", content)
            self.assertIn("127.0.0.1 localhost", content)
            # idempotent add
            self.run_helper(hosts, "add", "my-project.site")
            self.assertEqual(
                hosts.read_text().count("my-project.site"), 1
            )
            # second domain, then remove the first
            self.run_helper(hosts, "add", "client.dev")
            self.assertEqual(
                self.run_helper(hosts, "remove", "my-project.site").returncode,
                0,
            )
            content = hosts.read_text()
            self.assertNotIn("my-project.site", content)
            self.assertIn("127.0.0.2 client.dev", content)
            self.assertIn("127.0.0.1 localhost", content)

    def test_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            hosts = Path(tmp) / "hosts"
            hosts.write_text("127.0.0.1 localhost\n")
            for bad in ("nodots", "UPPER.site", "a b.site", "evil.site; rm",
                        "$(boom).site", ""):
                res = self.run_helper(hosts, "add", bad)
                self.assertNotEqual(res.returncode, 0, f"{bad!r} accepted")
            self.assertNotEqual(
                self.run_helper(hosts, "frobnicate", "x.site").returncode, 0
            )
            self.assertEqual(hosts.read_text(), "127.0.0.1 localhost\n")

    def test_check_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            hosts = Path(tmp) / "hosts"
            hosts.write_text("")
            self.assertEqual(self.run_helper(hosts, "check").returncode, 0)


class CaddyRoutesTest(unittest.TestCase):
    def test_only_running_sites_with_domains_get_routes(self):
        sites = [
            {"domain": "a.wp.site", "state": "running", "port": 8888},
            {"domain": None, "state": "running", "port": 8890},
            {"domain": "c.wp.site", "state": "stopped", "port": 8892},
        ]
        routes = mod.caddy_routes(sites)
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["match"], [{"host": ["a.wp.site"]}])
        self.assertEqual(
            routes[0]["handle"][0]["upstreams"],
            [{"dial": "127.0.0.1:8888"}],
        )


class HttpApiTest(unittest.TestCase):
    """End-to-end tests against a real server instance on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.project = tmp / "mysite"
        cls.project.mkdir()
        (cls.project / ".wp-env.json").write_text('{"port": 9001}')

        cls.saved = {
            "FAV_FILE": mod.FAV_FILE,
            "HIDDEN_FILE": mod.HIDDEN_FILE,
            "DOMAINS_FILE": mod.DOMAINS_FILE,
            "PROJECTS_FILE": mod.PROJECTS_FILE,
            "scan_projects": mod.scan_projects,
            "docker_ports_by_name": mod.docker_ports_by_name,
            "sync_caddy": mod.sync_caddy,
            "caddy_available": mod.caddy_available,
        }
        mod.FAV_FILE = tmp / "state/favorites.json"
        mod.HIDDEN_FILE = tmp / "state/hidden.json"
        mod.DOMAINS_FILE = tmp / "state/domains.json"
        mod.PROJECTS_FILE = tmp / "state/projects.json"
        cls.saved["domains_ready"] = mod.domains_ready
        cls.saved["hosts_helper"] = mod.hosts_helper
        mod.sync_caddy = lambda sites: None
        mod.caddy_available = lambda timeout=0.4: False
        mod.domains_ready = lambda: False
        mod.hosts_helper = lambda *a: True
        mod.scan_projects = lambda: [cls.project]
        mod.docker_ports_by_name = lambda: {}
        with mod.LOCK:
            mod.CACHE["dirs"] = [cls.project]
            mod.CACHE["scanned_at"] = time.time()
            mod.CACHE["scanning"] = False

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        for key, value in cls.saved.items():
            setattr(mod, key, value)
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, host=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest(method, path, skip_host=host is not None)
            if host is not None:
                conn.putheader("Host", host)
            data = json.dumps(body).encode() if body is not None else None
            if data:
                conn.putheader("Content-Length", str(len(data)))
            for key, value in (headers or {}).items():
                conn.putheader(key, value)
            conn.endheaders(data)
            res = conn.getresponse()
            return res.status, dict(res.getheaders()), res.read()
        finally:
            conn.close()

    def get_json(self, path, **kw):
        status, headers, raw = self.request("GET", path, **kw)
        return status, headers, json.loads(raw)

    def test_ping(self):
        status, _, data = self.get_json("/api/ping")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertIn(data["docker"], ("ok", "down", "missing"))

    def test_docker_header_on_sites(self):
        mod.DOCKER_STATE["status"] = "down"
        try:
            _, headers, _ = self.get_json("/api/sites")
            self.assertEqual(headers["X-Docker"], "down")
        finally:
            mod.DOCKER_STATE["status"] = "ok"
        _, headers, _ = self.get_json("/api/sites")
        self.assertEqual(headers["X-Docker"], "ok")

    def test_index_serves_html(self):
        status, headers, raw = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn(b"<title>wp-env</title>", raw)

    def test_sites_lists_project_stopped(self):
        status, headers, sites = self.get_json("/api/sites")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Scanning"], "0")
        self.assertEqual(len(sites), 1)
        site = sites[0]
        self.assertEqual(site["name"], "mysite")
        self.assertEqual(site["path"], str(self.project))
        self.assertEqual(site["port"], 9001)
        self.assertEqual(site["state"], "stopped")

    def test_sites_reports_running_from_docker(self):
        compose = mod.compose_project_names(self.project)[0]
        mod.docker_ports_by_name = lambda: {
            f"{compose}-wordpress-1": "0.0.0.0:9005->80/tcp"
        }
        try:
            _, _, sites = self.get_json("/api/sites")
            self.assertEqual(sites[0]["state"], "running")
            self.assertEqual(sites[0]["port"], 9005)
        finally:
            mod.docker_ports_by_name = lambda: {}

    def test_sites_reports_running_for_legacy_container_name(self):
        legacy = mod.compose_project_names(self.project)[1]
        mod.docker_ports_by_name = lambda: {
            f"{legacy}-wordpress-1": "0.0.0.0:9001->80/tcp"
        }
        try:
            _, _, sites = self.get_json("/api/sites")
            self.assertEqual(sites[0]["state"], "running")
        finally:
            mod.docker_ports_by_name = lambda: {}

    def test_forbidden_host_header(self):
        status, _, _ = self.request("GET", "/api/sites", host="evil.example")
        self.assertEqual(status, 403)

    def test_forbidden_origin_on_get_and_post(self):
        status, _, _ = self.request(
            "GET", "/api/sites", headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(status, 403)
        status, _, _ = self.request(
            "POST", "/api/action",
            body={"path": str(self.project), "action": "start"},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)

    def test_localhost_origin_allowed(self):
        status, _, _ = self.request(
            "GET", "/api/sites",
            headers={"Origin": f"http://localhost:{self.port}"},
        )
        self.assertEqual(status, 200)

    def test_action_on_unknown_path_rejected(self):
        status, _, _ = self.request(
            "POST", "/api/action", body={"path": "/etc", "action": "start"}
        )
        self.assertEqual(status, 403)

    def test_unknown_action_rejected(self):
        status, _, _ = self.request(
            "POST", "/api/action",
            body={"path": str(self.project), "action": "reboot"},
        )
        self.assertEqual(status, 400)

    def test_malformed_body_rejected(self):
        status, _, _ = self.request("POST", "/api/action", body=["nonsense"])
        self.assertEqual(status, 400)

    def test_open_on_stopped_site_conflicts(self):
        status, _, _ = self.request(
            "POST", "/api/action",
            body={"path": str(self.project), "action": "open"},
        )
        self.assertEqual(status, 409)

    def test_favorite_round_trip_via_api(self):
        status, _, _ = self.request(
            "POST", "/api/favorite",
            body={"path": str(self.project), "favorite": True},
        )
        self.assertEqual(status, 200)
        _, _, sites = self.get_json("/api/sites")
        self.assertTrue(sites[0]["favorite"])
        status, _, _ = self.request(
            "POST", "/api/favorite",
            body={"path": str(self.project), "favorite": False},
        )
        self.assertEqual(status, 200)
        _, _, sites = self.get_json("/api/sites")
        self.assertFalse(sites[0]["favorite"])

    def test_create_via_api(self):
        saved = (mod.SITES_DIR, mod.start_job, mod.port_in_use)
        sites_dir = Path(self.tmp.name) / "created-sites"
        mod.SITES_DIR = sites_dir
        started = []
        mod.start_job = lambda p, a: started.append((p, a))
        mod.port_in_use = lambda p: False
        try:
            status, _, raw = self.request(
                "POST", "/api/create", body={"slug": "api-site"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)["port"], 8888)
            self.assertTrue(
                (sites_dir / "api-site" / ".wp-env.json").is_file()
            )
            self.assertEqual(len(started), 1)
            _, _, sites = self.get_json("/api/sites")
            self.assertIn("api-site", [s["name"] for s in sites])

            status, _, _ = self.request(
                "POST", "/api/create", body={"slug": "Bad Slug"}
            )
            self.assertEqual(status, 400)
            status, _, _ = self.request(
                "POST", "/api/create", body={"slug": "api-site"}
            )
            self.assertEqual(status, 409)
        finally:
            mod.SITES_DIR, mod.start_job, mod.port_in_use = saved
            with mod.LOCK:
                mod.CACHE["dirs"] = [
                    d for d in mod.CACHE["dirs"]
                    if not str(d).startswith(str(sites_dir))
                ]

    def test_hidden_round_trip_via_api(self):
        try:
            status, _, _ = self.request(
                "POST", "/api/hidden",
                body={"path": str(self.project), "hidden": True},
            )
            self.assertEqual(status, 200)
            # excluded from the default listing (what the bar widget sees)
            _, _, sites = self.get_json("/api/sites")
            self.assertEqual(sites, [])
            # still present, flagged, when all=1 (the web UI's request)
            _, _, sites = self.get_json("/api/sites?all=1")
            self.assertEqual(len(sites), 1)
            self.assertTrue(sites[0]["hidden"])
            # hidden sites remain valid action targets
            status, _, _ = self.request(
                "POST", "/api/action",
                body={"path": str(self.project), "action": "open"},
            )
            self.assertEqual(status, 409)  # not running — but not 403
        finally:
            status, _, _ = self.request(
                "POST", "/api/hidden",
                body={"path": str(self.project), "hidden": False},
            )
            self.assertEqual(status, 200)
        _, _, sites = self.get_json("/api/sites")
        self.assertEqual(len(sites), 1)
        self.assertFalse(sites[0]["hidden"])

    def test_domain_round_trip_via_api(self):
        saved_start = mod.start_job
        mod.start_job = lambda p, a: True
        try:
            status, _, _ = self.request(
                "POST", "/api/domain",
                body={"path": str(self.project), "domain": "MySite.wp.site"},
            )
            self.assertEqual(status, 200)
            _, _, sites = self.get_json("/api/sites")
            self.assertEqual(sites[0]["domain"], "mysite.wp.site")
            self.assertEqual(sites[0]["url"], "https://mysite.wp.site")
            override = json.loads(
                (self.project / ".wp-env.override.json").read_text()
            )
            self.assertEqual(override["config"]["WP_HOME"],
                             "https://mysite.wp.site")

            for bad in ("not_a_domain", "nodots", "-x.wp.site", "a.b!c.site"):
                status, _, _ = self.request(
                    "POST", "/api/domain",
                    body={"path": str(self.project), "domain": bad},
                )
                self.assertEqual(status, 400, f"{bad!r} should be rejected")

            status, _, _ = self.request(
                "POST", "/api/domain",
                body={"path": str(self.project), "domain": ""},
            )
            self.assertEqual(status, 200)
            _, _, sites = self.get_json("/api/sites")
            self.assertIsNone(sites[0]["domain"])
            self.assertEqual(sites[0]["url"], "http://localhost:9001")
            self.assertFalse(
                (self.project / ".wp-env.override.json").exists()
            )
        finally:
            mod.start_job = saved_start

    def test_duplicate_domain_rejected(self):
        with mod.LOCK:
            mod.DOMAINS_FILE.parent.mkdir(parents=True, exist_ok=True)
            mod.DOMAINS_FILE.write_text(
                json.dumps({"/somewhere/else": "taken.wp.site"})
            )
        try:
            status, _, raw = self.request(
                "POST", "/api/domain",
                body={"path": str(self.project), "domain": "taken.wp.site"},
            )
            self.assertEqual(status, 409)
            self.assertIn("else", json.loads(raw)["error"])
        finally:
            with mod.LOCK:
                mod.DOMAINS_FILE.unlink()

    def test_domain_on_unknown_path_rejected(self):
        status, _, _ = self.request(
            "POST", "/api/domain",
            body={"path": "/etc", "domain": "x.wp.site"},
        )
        self.assertEqual(status, 403)

    def test_hidden_on_unknown_path_rejected(self):
        status, _, _ = self.request(
            "POST", "/api/hidden", body={"path": "/etc", "hidden": True}
        )
        self.assertEqual(status, 403)

    def test_info_endpoint(self):
        status, _, data = self.get_json(
            "/api/info?path=" + str(self.project)
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["config"]["port"], 9001)
        status, _, _ = self.request("GET", "/api/info?path=/etc")
        self.assertEqual(status, 403)

    def test_log_without_job_is_empty(self):
        status, _, data = self.get_json(
            "/api/log?path=" + str(self.project)
        )
        self.assertEqual(status, 200)
        self.assertEqual(data, {"action": None, "log": ""})

    def test_domains_status(self):
        status, _, data = self.get_json("/api/domains-status")
        self.assertEqual(status, 200)
        self.assertEqual(data["proxy"], False)  # caddy_available stubbed
        self.assertIn(data["setup"], ("idle", "running", "done", "error"))

    def test_unknown_route_is_404(self):
        status, _, _ = self.request("GET", "/api/nope")
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", "/api/nope", body={"path": "x"})
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
