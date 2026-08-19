"""Tests for arch-controller. Run with: python -m unittest discover -s tests"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from archctl import actions, apps, auth, config as config_module, files, system  # noqa: E402
from archctl.server import Server  # noqa: E402


def subprocess_result(code: int, stdout: str, stderr: str):
    """Stand-in for a CompletedProcess, so sudo paths can be tested without root."""
    return unittest.mock.Mock(returncode=code, stdout=stdout, stderr=stderr)


class ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        os.environ.pop("ARCHCTL_TOKEN", None)

    def write_config(self, text: str) -> Path:
        path = self.dir / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_defaults_when_file_missing(self) -> None:
        cfg = config_module.load(self.dir / "absent.toml")
        self.assertEqual(cfg.port, config_module.DEFAULT_PORT)
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertFalse(cfg.allow_shell)
        self.assertIsNone(cfg.path)

    def test_full_config(self) -> None:
        path = self.write_config(
            f"""
            [server]
            host = "100.64.0.2"
            port = 9001

            [auth]
            token = "hunter2"
            token_file = "{self.dir / 'tok'}"

            [actions]
            allow_shell = true

            [[commands]]
            id = "update"
            label = "Update system"
            argv = ["pacman", "-Syu"]
            confirm = true
            """
        )
        cfg = config_module.load(path)
        self.assertEqual(cfg.host, "100.64.0.2")
        self.assertEqual(cfg.port, 9001)
        self.assertEqual(cfg.token, "hunter2")
        self.assertTrue(cfg.allow_shell)
        self.assertEqual(len(cfg.commands), 1)
        self.assertEqual(cfg.commands[0].argv, ["pacman", "-Syu"])
        self.assertTrue(cfg.commands[0].confirm)
        self.assertEqual(cfg.base_url, "http://100.64.0.2:9001")
        self.assertEqual(cfg.login_url(), "http://100.64.0.2:9001/#t=hunter2")

    def test_env_token_wins(self) -> None:
        path = self.write_config('[auth]\ntoken = "from-file"\n')
        os.environ["ARCHCTL_TOKEN"] = "from-env"
        self.addCleanup(os.environ.pop, "ARCHCTL_TOKEN", None)
        self.assertEqual(config_module.load(path).token, "from-env")

    def test_command_without_argv_is_rejected(self) -> None:
        path = self.write_config('[[commands]]\nid = "bad"\nargv = []\n')
        with self.assertRaises(ValueError):
            config_module.load(path)

    def test_token_file_is_created_private(self) -> None:
        target = self.dir / "nested" / "token"
        token = config_module.read_or_create_token(target)
        self.assertTrue(target.exists())
        self.assertGreater(len(token), 20)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(config_module.read_or_create_token(target), token)

    def test_loose_permissions_are_tightened(self) -> None:
        target = self.dir / "token"
        target.write_text("plaintext\n", encoding="utf-8")
        target.chmod(0o644)
        self.assertEqual(config_module.read_or_create_token(target), "plaintext")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_wildcard_bind_reports_loopback_url(self) -> None:
        cfg = config_module.Config(host="0.0.0.0", port=8787)
        self.assertEqual(cfg.base_url, "http://127.0.0.1:8787")

    def test_public_url_overrides(self) -> None:
        cfg = config_module.Config(public_url="https://arch.tail1234.ts.net/")
        self.assertEqual(cfg.base_url, "https://arch.tail1234.ts.net")


class AuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = auth.Auth(token="secret", session_ttl=100)

    def test_token_comparison(self) -> None:
        self.assertTrue(self.auth.check_token("secret"))
        self.assertFalse(self.auth.check_token("Secret"))
        self.assertFalse(self.auth.check_token(""))
        self.assertFalse(auth.Auth(token="").check_token(""))

    def test_session_lifecycle(self) -> None:
        sid = self.auth.create_session(now=1000)
        self.assertTrue(self.auth.valid_session(sid, now=1050))
        self.assertFalse(self.auth.valid_session(sid, now=1101))
        self.assertFalse(self.auth.valid_session("nope"))

    def test_drop_session(self) -> None:
        sid = self.auth.create_session()
        self.auth.drop_session(sid)
        self.assertFalse(self.auth.valid_session(sid))

    def test_lockout_after_repeated_failures(self) -> None:
        for _ in range(auth.MAX_FAILURES - 1):
            self.auth.record_failure("1.2.3.4", now=0)
        self.assertEqual(self.auth.retry_after("1.2.3.4", now=0), 0.0)
        self.auth.record_failure("1.2.3.4", now=0)
        self.assertGreater(self.auth.retry_after("1.2.3.4", now=0), 0)
        self.assertEqual(self.auth.retry_after("1.2.3.4", now=10_000), 0.0)

    def test_lockout_is_per_client_and_cleared_on_success(self) -> None:
        for _ in range(auth.MAX_FAILURES):
            self.auth.record_failure("1.2.3.4", now=0)
        self.assertEqual(self.auth.retry_after("5.6.7.8", now=0), 0.0)
        self.auth.record_success("1.2.3.4")
        self.assertEqual(self.auth.retry_after("1.2.3.4", now=0), 0.0)


class SystemParserTests(unittest.TestCase):
    def test_cpu_line(self) -> None:
        idle, total = system.parse_cpu_line("cpu  100 0 50 800 20 0 0 0 0 0")
        self.assertEqual(idle, 820)
        self.assertEqual(total, 970)
        self.assertEqual(system.parse_cpu_line("cpu 1 2"), (0, 0))

    def test_meminfo(self) -> None:
        parsed = system.parse_meminfo("MemTotal:  16384 kB\nMemAvailable: 8192 kB\nBogus: x\n")
        self.assertEqual(parsed["MemTotal"], 16384 * 1024)
        self.assertNotIn("Bogus", parsed)

    def test_duration_formatting(self) -> None:
        self.assertEqual(system.format_duration(90), "1m")
        self.assertEqual(system.format_duration(3700), "1h 1m")
        self.assertEqual(system.format_duration(90061), "1d 1h 1m")

    def test_net_dev_skips_loopback(self) -> None:
        text = (
            "Inter-|   Receive                          |  Transmit\n"
            " face |bytes packets errs drop fifo frame compressed multicast|bytes packets\n"
            "    lo: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n"
            "  eth0: 500 5 0 0 0 0 0 0 700 7 0 0 0 0 0 0\n"
        )
        parsed = system.parse_proc_net_dev(text)
        self.assertNotIn("lo", parsed)
        self.assertEqual(parsed["eth0"], (500, 700))

    def test_snapshot_shape(self) -> None:
        snap = system.snapshot()
        for key in ("hostname", "uptime", "load", "memory", "disk", "cpu_percent"):
            self.assertIn(key, snap)
        self.assertGreater(snap["memory"]["total"], 0)


class ActionParserTests(unittest.TestCase):
    def test_wpctl_volume(self) -> None:
        self.assertEqual(actions.parse_wpctl_volume("Volume: 0.45"), {"percent": 45, "muted": False})
        self.assertEqual(
            actions.parse_wpctl_volume("Volume: 1.00 [MUTED]"), {"percent": 100, "muted": True}
        )

    def test_pactl_volume(self) -> None:
        line = "Volume: front-left: 39321 /  60% / -13.32 dB,   front-right: 39321 /  60%"
        self.assertEqual(actions.parse_pactl_volume(line), 60)
        self.assertEqual(actions.parse_pactl_volume("nothing here"), 0)

    def test_brightnessctl(self) -> None:
        self.assertEqual(actions.parse_brightnessctl("intel_backlight,backlight,3980,45%,7500"), 45)

    def test_url_scheme_is_restricted(self) -> None:
        for bad in ("file:///etc/passwd", "javascript:alert(1)", "", "http://x\nevil"):
            with self.assertRaises(actions.ActionError):
                actions.open_url(bad)

    def test_unknown_actions_rejected(self) -> None:
        with self.assertRaises(actions.ActionError):
            actions.media("format-disk")
        with self.assertRaises(actions.ActionError):
            actions.power("nuke")

    def test_kill_guards(self) -> None:
        with self.assertRaises(actions.ActionError):
            actions.kill_process(1)
        with self.assertRaises(actions.ActionError):
            actions.kill_process(os.getpid())
        with self.assertRaises(actions.ActionError):
            actions.kill_process(999999, "STOP")

    def test_missing_binary_raises_not_implemented(self) -> None:
        with self.assertRaises(actions.ActionError) as ctx:
            actions.run(["definitely-not-a-real-binary-xyz"])
        self.assertEqual(ctx.exception.status, 501)

    def test_processes_reports_this_one(self) -> None:
        found = actions.processes(limit=100)
        self.assertTrue(all(p["rss"] >= 0 for p in found))
        self.assertLessEqual(len(found), 100)

    def test_capabilities_keys(self) -> None:
        caps = actions.capabilities()
        self.assertIn("media", caps)
        self.assertTrue(all(isinstance(v, bool) for v in caps.values()))


class DesktopEntryTests(unittest.TestCase):
    def test_parses_the_fields_we_show(self) -> None:
        entry = apps.parse_desktop_entry(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Firefox\n"
            "Name[de]=Feuerfuchs\n"
            "Comment=Browse the web\n"
            "Exec=firefox %u\n"
            "Categories=Network;WebBrowser;\n"
            "\n[Desktop Action new-window]\n"
            "Name=New Window\n"
        )
        self.assertEqual(entry["name"], "Firefox")
        self.assertEqual(entry["comment"], "Browse the web")
        self.assertEqual(entry["categories"], ["Network", "WebBrowser"])
        self.assertFalse(entry["terminal"])

    def test_hidden_and_non_application_entries_are_skipped(self) -> None:
        base = "[Desktop Entry]\nType=Application\nName=X\nExec=x\n"
        self.assertIsNone(apps.parse_desktop_entry(base + "NoDisplay=true\n"))
        self.assertIsNone(apps.parse_desktop_entry(base + "Hidden=true\n"))
        self.assertIsNone(apps.parse_desktop_entry("[Desktop Entry]\nType=Link\nName=X\nExec=x\n"))
        self.assertIsNone(apps.parse_desktop_entry("[Desktop Entry]\nType=Application\nName=X\n"))

    def test_tryexec_missing_hides_the_entry(self) -> None:
        entry = "[Desktop Entry]\nType=Application\nName=X\nExec=x\nTryExec=/no/such/binary\n"
        self.assertIsNone(apps.parse_desktop_entry(entry))

    def test_field_codes_are_stripped_from_exec(self) -> None:
        self.assertEqual(apps.clean_exec("firefox %u"), ["firefox"])
        self.assertEqual(apps.clean_exec("code --unity-launch %F"), ["code", "--unity-launch"])
        self.assertEqual(apps.clean_exec('"/opt/my app/run" %f'), ["/opt/my app/run"])
        self.assertEqual(apps.clean_exec("thing --file=%f"), ["thing", "--file="])

    def test_terminal_flag(self) -> None:
        entry = apps.parse_desktop_entry(
            "[Desktop Entry]\nType=Application\nName=htop\nExec=htop\nTerminal=true\n"
        )
        self.assertTrue(entry["terminal"])

    def test_list_apps_is_cached_and_shaped(self) -> None:
        found = apps.list_apps(force=True)
        self.assertIsInstance(found, list)
        for app in found:
            self.assertTrue(app["id"].endswith(".desktop"))
            self.assertTrue(app["name"])
        self.assertIs(apps.list_apps(), apps.list_apps())

    def test_launching_an_unknown_app_404s(self) -> None:
        with self.assertRaises(actions.ActionError) as ctx:
            apps.find_app("not-installed.desktop")
        self.assertEqual(ctx.exception.status, 404)


class FileBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        (self.root / "sub").mkdir()
        (self.root / "notes.txt").write_text("hello", encoding="utf-8")
        (self.root / ".secret").write_text("shh", encoding="utf-8")

    def test_listing_sorts_directories_first(self) -> None:
        result = files.listdir(str(self.root))
        names = [e["name"] for e in result["entries"]]
        self.assertEqual(names, ["sub", "notes.txt"])
        self.assertTrue(result["entries"][0]["is_dir"])
        self.assertEqual(result["entries"][1]["size"], 5)

    def test_hidden_files_are_opt_in(self) -> None:
        visible = [e["name"] for e in files.listdir(str(self.root))["entries"]]
        self.assertNotIn(".secret", visible)
        shown = [e["name"] for e in files.listdir(str(self.root), show_hidden=True)["entries"]]
        self.assertIn(".secret", shown)

    def test_root_confines_navigation(self) -> None:
        with self.assertRaises(actions.ActionError) as ctx:
            files.listdir("/etc", root=self.root)
        self.assertEqual(ctx.exception.status, 403)
        # Traversal dressed up as a relative path is resolved before the check.
        with self.assertRaises(actions.ActionError):
            files.listdir(f"{self.root}/sub/../../..", root=self.root)
        # And the root itself reports no parent to climb to.
        self.assertIsNone(files.listdir(str(self.root), root=self.root)["parent"])

    def test_missing_and_non_directory_paths(self) -> None:
        with self.assertRaises(actions.ActionError) as ctx:
            files.listdir(str(self.root / "nope"))
        self.assertEqual(ctx.exception.status, 404)
        with self.assertRaises(actions.ActionError):
            files.listdir(str(self.root / "notes.txt"))

    def test_download_target_checks(self) -> None:
        self.assertEqual(files.download_target(str(self.root / "notes.txt")).name, "notes.txt")
        with self.assertRaises(actions.ActionError):
            files.download_target(str(self.root / "sub"))

    def test_broken_symlink_is_listed_not_fatal(self) -> None:
        (self.root / "dangling").symlink_to(self.root / "gone")
        names = [e["name"] for e in files.listdir(str(self.root))["entries"]]
        self.assertIn("dangling", names)

    def test_tilde_expands_to_home(self) -> None:
        self.assertEqual(files.resolve("~"), Path.home().resolve())


class SudoTests(unittest.TestCase):
    def test_empty_command_rejected(self) -> None:
        with self.assertRaises(actions.ActionError):
            actions.run_sudo("   ")

    def test_password_prompt_is_detected(self) -> None:
        """A sudo that asks for a password surfaces as SudoPasswordRequired."""
        fake = subprocess_result(1, "", "sudo: a password is required")
        with unittest.mock.patch("subprocess.run", return_value=fake):
            with self.assertRaises(actions.SudoPasswordRequired):
                actions.run_sudo("whoami")

    def test_wrong_password_is_reported(self) -> None:
        fake = subprocess_result(1, "", "Sorry, try again.")
        with unittest.mock.patch("subprocess.run", return_value=fake):
            with self.assertRaises(actions.ActionError) as ctx:
                actions.run_sudo("whoami", password="wrong")
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("rejected", str(ctx.exception))

    def test_password_is_sent_on_stdin_only(self) -> None:
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            captured["input"] = kwargs.get("input")
            return subprocess_result(0, "root", "")

        with unittest.mock.patch("subprocess.run", side_effect=fake_run):
            result = actions.run_sudo("id -un", password="hunter2")

        self.assertTrue(result["ok"])
        self.assertEqual(captured["input"], "hunter2\n")
        self.assertNotIn("hunter2", " ".join(captured["argv"]))
        self.assertIn("-S", captured["argv"])

    def test_no_password_uses_non_interactive_mode(self) -> None:
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = argv
            return subprocess_result(0, "root", "")

        with unittest.mock.patch("subprocess.run", side_effect=fake_run):
            actions.run_sudo("id -un")
        self.assertIn("-n", captured["argv"])

    def test_sudo_noise_is_stripped_from_stderr(self) -> None:
        fake = subprocess_result(0, "done", "sudo: chatter here\nreal warning")
        with unittest.mock.patch("subprocess.run", return_value=fake):
            result = actions.run_sudo("thing", password="x")
        self.assertEqual(result["stderr"], "real warning")


class PasswordCacheTests(unittest.TestCase):
    def test_stores_until_expiry(self) -> None:
        cache = auth.PasswordCache(ttl=100)
        cache.store("pw", now=0)
        self.assertEqual(cache.get(now=99), "pw")
        self.assertIsNone(cache.get(now=101))

    def test_clear_and_zero_ttl(self) -> None:
        cache = auth.PasswordCache(ttl=100)
        cache.store("pw", now=0)
        cache.clear()
        self.assertIsNone(cache.get(now=1))

        disabled = auth.PasswordCache(ttl=0)
        disabled.store("pw", now=0)
        self.assertIsNone(disabled.get(now=0))


class ServerTests(unittest.TestCase):
    """End-to-end HTTP tests against a real server on a loopback port."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cfg = config_module.Config(
            host="127.0.0.1",
            port=0,
            token="test-token",
            token_file=Path(cls.tmp.name) / "token",
        )
        cfg.commands = [
            config_module.Command(id="echo", label="Echo", argv=["echo", "hello"]),
        ]
        cls.server = Server(cfg)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, path, method="GET", body=None, headers=None, opener=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url(path), data=data, method=method)
        req.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        fetch = opener.open if opener else urllib.request.urlopen
        try:
            with fetch(req, timeout=10) as response:
                raw = response.read()
                payload = json.loads(raw) if raw and response.headers.get(
                    "Content-Type", ""
                ).startswith("application/json") else raw
                return response.status, payload
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw)
            except json.JSONDecodeError:
                return err.code, raw

    def bearer(self, path, method="GET", body=None):
        return self.request(path, method, body, {"Authorization": "Bearer test-token"})

    # -- unauthenticated surface ------------------------------------
    def test_ping_is_public(self) -> None:
        status, payload = self.request("/api/ping")
        self.assertEqual(status, 200)
        self.assertEqual(payload["service"], "arch-controller")

    def test_status_requires_auth(self) -> None:
        status, payload = self.request("/api/status")
        self.assertEqual(status, 401)
        self.assertIn("error", payload)

    def test_login_rejects_wrong_token(self) -> None:
        status, _ = self.request("/api/login", "POST", {"token": "wrong"})
        self.assertEqual(status, 401)

    def test_unknown_endpoint_404(self) -> None:
        self.assertEqual(self.bearer("/api/nope")[0], 404)

    def test_method_mismatch_405(self) -> None:
        self.assertEqual(self.bearer("/api/status", "POST", {})[0], 405)

    def test_static_index_served(self) -> None:
        with urllib.request.urlopen(self.url("/"), timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("arch-controller", response.read().decode())

    def test_unknown_static_path_404(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(self.url("/../etc/passwd"), timeout=10)
        self.assertEqual(ctx.exception.code, 404)

    # -- token auth --------------------------------------------------
    def test_bearer_token_grants_access(self) -> None:
        status, payload = self.bearer("/api/status")
        self.assertEqual(status, 200)
        self.assertIn("system", payload)
        self.assertIn("capabilities", payload)
        self.assertEqual([c["id"] for c in payload["commands"]], ["echo"])

    def test_bad_bearer_token_rejected(self) -> None:
        status, _ = self.request("/api/status", headers={"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)

    def test_configured_command_runs(self) -> None:
        status, payload = self.bearer("/api/command", "POST", {"id": "echo"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stdout"], "hello")

    def test_unknown_command_404(self) -> None:
        self.assertEqual(self.bearer("/api/command", "POST", {"id": "rm-rf"})[0], 404)

    def test_shell_disabled_by_default(self) -> None:
        status, payload = self.bearer("/api/shell", "POST", {"cmd": "id"})
        self.assertEqual(status, 403)
        self.assertIn("disabled", payload["error"])

    def test_destructive_power_needs_confirmation(self) -> None:
        status, payload = self.bearer("/api/power", "POST", {"action": "poweroff"})
        self.assertEqual(status, 400)
        self.assertIn("confirm", payload["error"])

    def test_malformed_json_rejected(self) -> None:
        req = urllib.request.Request(
            self.url("/api/media"), data=b"{not json", method="POST",
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 400)

    def test_processes_endpoint(self) -> None:
        status, payload = self.bearer("/api/processes?limit=3")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(payload["processes"]), 3)

    # -- cookie sessions ---------------------------------------------
    def test_cookie_session_flow_and_csrf(self) -> None:
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        status, _ = self.request("/api/login", "POST", {"token": "test-token"}, opener=opener)
        self.assertEqual(status, 200)

        # GET works on the cookie alone.
        status, payload = self.request("/api/status", opener=opener)
        self.assertEqual(status, 200)
        self.assertIn("system", payload)

        # A mutation without the custom header is refused (cross-site defence).
        status, payload = self.request("/api/media", "POST", {"action": "next"}, opener=opener)
        self.assertEqual(status, 403)
        self.assertIn("X-Archctl", payload["error"])

        # With the header it is accepted (501 here only because playerctl is absent).
        status, _ = self.request(
            "/api/command", "POST", {"id": "echo"}, {"X-Archctl": "1"}, opener=opener
        )
        self.assertEqual(status, 200)

        # Logging out invalidates the session.
        status, _ = self.request("/api/logout", "POST", {}, {"X-Archctl": "1"}, opener=opener)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("/api/status", opener=opener)[0], 401)

    def test_session_cookie_is_httponly_and_samesite(self) -> None:
        req = urllib.request.Request(
            self.url("/api/login"),
            data=json.dumps({"token": "test-token"}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            cookie = response.headers.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)

    def test_apps_endpoint_lists_and_filters(self) -> None:
        status, payload = self.bearer("/api/apps")
        self.assertEqual(status, 200)
        self.assertIsInstance(payload["apps"], list)
        status, filtered = self.bearer("/api/apps?q=zzz-no-such-app")
        self.assertEqual(status, 200)
        self.assertEqual(filtered["apps"], [])

    def test_launching_unknown_app_404s(self) -> None:
        status, _ = self.bearer("/api/apps/launch", "POST", {"id": "nope.desktop"})
        self.assertEqual(status, 404)

    def test_files_endpoint_browses(self) -> None:
        status, payload = self.bearer("/api/files?path=/etc")
        self.assertEqual(status, 200)
        self.assertEqual(payload["path"], "/etc")
        self.assertEqual(payload["parent"], "/")
        self.assertTrue(payload["entries"])
        self.assertTrue(any(p["label"] == "Home" for p in payload["places"]))

    def test_files_endpoint_defaults_to_home(self) -> None:
        status, payload = self.bearer("/api/files")
        self.assertEqual(status, 200)
        self.assertEqual(payload["path"], str(Path.home().resolve()))

    def test_file_download_streams_content(self) -> None:
        target = Path(self.tmp.name) / "hello.txt"
        target.write_text("streamed", encoding="utf-8")
        req = urllib.request.Request(
            self.url(f"/api/files/download?path={urllib.parse.quote(str(target))}"),
            headers={"Authorization": "Bearer test-token"},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            self.assertEqual(response.read(), b"streamed")
            self.assertIn("attachment", response.headers.get("Content-Disposition", ""))

    def test_downloading_a_directory_404s(self) -> None:
        status, _ = self.bearer(f"/api/files/download?path={urllib.parse.quote(self.tmp.name)}")
        self.assertEqual(status, 404)

    def test_sudo_disabled_by_default(self) -> None:
        status, payload = self.bearer("/api/sudo", "POST", {"cmd": "id"})
        self.assertEqual(status, 403)
        self.assertIn("disabled", payload["error"])

    def test_status_reports_new_panels(self) -> None:
        _, payload = self.bearer("/api/status")
        self.assertFalse(payload["sudo"]["enabled"])
        self.assertTrue(payload["apps_enabled"])
        self.assertTrue(payload["files_enabled"])

    def test_repeated_bad_logins_are_throttled(self) -> None:
        # A dedicated Auth instance keeps this from locking out the shared server.
        throttled = auth.Auth(token="x")
        for _ in range(auth.MAX_FAILURES):
            throttled.record_failure("127.0.0.1", now=time.time())
        self.assertGreater(throttled.retry_after("127.0.0.1"), 0)


class RestrictedServerTests(unittest.TestCase):
    """A server with sudo on and the file browser confined to one directory."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name).resolve()
        (cls.root / "inside.txt").write_text("ok", encoding="utf-8")
        cfg = config_module.Config(
            host="127.0.0.1", port=0, token="test-token", token_file=cls.root / "token"
        )
        cfg.allow_sudo = True
        cfg.sudo_cache_minutes = 5
        cfg.files_root = cls.root
        cfg.apps_enabled = False
        cls.server = Server(cfg)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def call(self, path, method="GET", body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        req.add_header("Authorization", "Bearer test-token")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read())

    def test_file_root_is_enforced_over_http(self) -> None:
        status, payload = self.call("/api/files")
        self.assertEqual(status, 200)
        self.assertEqual(payload["path"], str(self.root))
        self.assertIsNone(payload["parent"])

        status, payload = self.call("/api/files?path=/etc")
        self.assertEqual(status, 403)
        self.assertIn("outside", payload["error"])

    def test_download_outside_root_is_refused(self) -> None:
        status, _ = self.call("/api/files/download?path=/etc/hostname")
        self.assertEqual(status, 403)

    def test_apps_can_be_switched_off(self) -> None:
        self.assertEqual(self.call("/api/apps")[0], 403)

    def test_status_reports_sudo_enabled(self) -> None:
        _, payload = self.call("/api/status")
        self.assertTrue(payload["sudo"]["enabled"])
        self.assertIn("passwordless", payload["sudo"])
        self.assertFalse(payload["apps_enabled"])

    def test_sudo_runs_a_command(self) -> None:
        """Skipped unless this machine can sudo without a password."""
        if not actions.sudo_passwordless():
            self.skipTest("no passwordless sudo available here")
        status, payload = self.call("/api/sudo", "POST", {"cmd": "id -un"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["stdout"].strip(), "root")

    def test_sudo_password_prompt_surfaces_as_401(self) -> None:
        fake = subprocess_result(1, "", "sudo: a password is required")
        with unittest.mock.patch("subprocess.run", return_value=fake):
            status, payload = self.call("/api/sudo", "POST", {"cmd": "id"})
        self.assertEqual(status, 401)
        self.assertTrue(payload["needs_password"])

    def test_password_is_cached_then_forgotten(self) -> None:
        with unittest.mock.patch("subprocess.run", return_value=subprocess_result(0, "root", "")):
            self.call("/api/sudo", "POST", {"cmd": "id", "password": "hunter2"})
        self.assertEqual(self.server.sudo_cache.get(), "hunter2")

        self.assertEqual(self.call("/api/sudo/forget", "POST", {})[0], 200)
        self.assertIsNone(self.server.sudo_cache.get())


if __name__ == "__main__":
    unittest.main()
