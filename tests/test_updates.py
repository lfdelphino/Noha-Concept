import io
import json
import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from scripts import updater
from scripts.build_release import build


class ReleaseTests(unittest.TestCase):
    def test_missing_release_leaves_installation_untouched(self):
        import urllib.error
        with patch.object(updater, "request", side_effect=urllib.error.HTTPError("url", 404, "none", {}, None)):
            self.assertIsNone(updater.latest())

    def test_release_requires_digest_and_expected_repository(self):
        data = {"tag_name": "v1.2.0", "id": 4, "assets": [{"name": "noha-pi.zip", "id": 8,
                "browser_download_url": "https://github.com/lfdelphino/Noha-Concept/releases/download/v1.2.0/noha-pi.zip"}]}
        with patch.object(updater, "request", return_value=json.dumps(data).encode()):
            with self.assertRaises(ValueError):
                updater.latest()
            data["assets"][0]["digest"] = "sha256:" + "a" * 64
        with patch.object(updater, "request", return_value=json.dumps(data).encode()):
            self.assertEqual(updater.latest()["tag"], "v1.2.0")

    def test_checksum_failure_never_builds_or_switches(self):
        with TemporaryDirectory() as folder, patch.object(updater, "ROOT", Path(folder)), \
                patch.object(updater, "request", return_value=b"corrupt"), patch.object(updater, "command") as command:
            with self.assertRaises(ValueError):
                updater.stage({"tag": "v1.2.0", "asset_id": 4, "digest": "a" * 64, "url": "unused"})
            command.assert_not_called()
            self.assertFalse((Path(folder) / "current").exists())

    def test_archive_cannot_escape_or_replace_arbitrary_paths(self):
        for name in ("noha-pi/../../etc/passwd", "/etc/passwd", "other/project.py", "noha-pi/..\\escape"):
            payload = io.BytesIO()
            with ZipFile(payload, "w") as bundle:
                bundle.writestr(name, "bad")
            payload.seek(0)
            with TemporaryDirectory() as folder, self.assertRaises(ValueError):
                updater.extract(payload, Path(folder))

    def test_build_version_and_runtime_files(self):
        with TemporaryDirectory() as folder:
            current = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
            asset = build(Path.cwd(), Path(folder) / "noha-pi.zip", "v" + current)
            with ZipFile(asset) as bundle:
                manifest = json.loads(bundle.read("noha-pi/release.json"))
                self.assertEqual(manifest["version"], current)
                self.assertIn("noha-pi/scripts/updater.py", bundle.namelist())
                self.assertNotIn("noha-pi/secrets.env", bundle.namelist())
            with self.assertRaises(ValueError):
                build(Path.cwd(), asset, "v9999.0.0")


class ActivationTests(unittest.TestCase):
    def test_failed_health_check_restores_previous_code_and_units(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            previous = root / "releases/old"
            candidate = root / "releases/new"
            previous.mkdir(parents=True)
            candidate.mkdir()
            # A plain directory current is sufficient here because switch is the transaction boundary mock.
            (root / "current").mkdir()
            status = root / "runtime/update-pending"
            status.parent.mkdir()
            units = {}
            for name in ("noha.service", "noha-network.service", "noha-portal.service", "noha-admin.service", "noha-ngrok.service"):
                old = root / "units" / name
                old.parent.mkdir(exist_ok=True)
                old.write_text("old " + name)
                units[name] = old
                new = candidate / "source/systemd" / name
                new.parent.mkdir(parents=True, exist_ok=True)
                new.write_text("new " + name)
            release = {"tag": "v1.2.0", "id": 42}
            with patch.object(updater, "ROOT", root), patch.object(updater, "STATE", root / "state"), \
                    patch.object(updater, "account", return_value=SimpleNamespace()), \
                    patch.object(updater, "maintenance", return_value=status), \
                    patch.object(updater, "paths", return_value=units), \
                    patch.object(updater, "health", return_value=False), \
                    patch.object(updater, "switch") as switch, \
                    patch.object(updater, "command"), patch.object(updater, "user_command"):
                with self.assertRaises(RuntimeError):
                    updater.apply(candidate, release, {})
                self.assertEqual(switch.call_args_list[0].args, (candidate,))
                self.assertEqual(switch.call_args_list[-1].args, (root / "current",))
                self.assertTrue(all(path.read_text() == "old " + name for name, path in units.items()))
                self.assertEqual(updater.read_json(root / "state/updates.json")["rejected"], release)
                self.assertFalse((root / "state/update-transaction.json").exists())
                self.assertFalse(status.exists())

    def test_applies_immediately_during_conversation_and_updates_itself(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / "releases/new"
            (candidate / "source/scripts").mkdir(parents=True)
            (candidate / "source/scripts/updater.py").write_text("new updater")
            (root / "updater.py").write_text("old updater")
            (root / "current").mkdir()
            marker = root / "runtime/update-pending"
            marker.parent.mkdir()
            units = {}
            for name in ("noha.service", "noha-network.service"):
                units[name] = root / "units" / name
                units[name].parent.mkdir(exist_ok=True)
                units[name].write_text("old")
                (candidate / "source/systemd").mkdir(parents=True, exist_ok=True)
                (candidate / "source/systemd" / name).write_text("new")
            # A busy, not-ready robot (conversation running, sensor unwired) must not block the update.
            (root / "status.json").write_text('{"busy": true, "ready": false}')
            with patch.object(updater, "ROOT", root), patch.object(updater, "STATE", root / "state"), \
                    patch.object(updater, "account", return_value=SimpleNamespace()), \
                    patch.object(updater, "maintenance", return_value=marker), \
                    patch.object(updater, "paths", return_value=units), \
                    patch.object(updater, "health", return_value=True), \
                    patch.object(updater, "switch"), patch.object(updater, "command"), \
                    patch.object(updater, "user_command") as user_command:
                self.assertTrue(updater.apply(candidate, {"tag": "v1.3.2"}, {}))
            user_command.assert_any_call(SimpleNamespace(), "stop", "noha.service")
            self.assertEqual((root / "updater.py").read_text(), "new updater")
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
