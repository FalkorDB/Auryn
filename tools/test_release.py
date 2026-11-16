"""Release checks run without the editable workspace or its test dependencies."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from check_release import check_release


class ReleaseCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dist = Path(self.directory.name)
        for folder, name in (("umbrella", "falkordb-auryn"), ("members", "auryn-python")):
            target = self.dist / folder
            target.mkdir()
            normalized = name.replace("-", "_")
            with zipfile.ZipFile(target / f"{normalized}-0.1.0-py3-none-any.whl", "w") as wheel:
                wheel.writestr(
                    f"{normalized}-0.1.0.dist-info/METADATA",
                    f"Name: {name}\nVersion: 0.1.0\n",
                )

    @patch("check_release.subprocess.run")
    def test_missing_published_member_stops_before_install(self, run):
        def fail_download(command, **kwargs):
            if "download" in command:
                raise subprocess.CalledProcessError(1, command)

        run.side_effect = fail_download
        with self.assertRaises(subprocess.CalledProcessError):
            check_release(self.dist, "https://test.pypi.org/simple/")
        self.assertFalse(any("install" in call.args[0] for call in run.call_args_list))

    @patch("check_release.subprocess.run")
    def test_published_check_downloads_exact_versions_without_local_fallback(self, run):
        def inspect_download(command, **kwargs):
            if "download" in command:
                requirements = Path(command[command.index("-r") + 1])
                self.assertEqual(requirements.read_text(), "auryn-python==0.1.0\n")

        run.side_effect = inspect_download
        check_release(self.dist, "https://test.pypi.org/simple/")
        commands = [call.args[0] for call in run.call_args_list]
        download = next(command for command in commands if "download" in command)
        self.assertIn("https://test.pypi.org/simple/", download)
        self.assertIn("--no-cache-dir", download)
        self.assertNotIn("--extra-index-url", download)
        self.assertNotIn("--find-links", download)
        install = next(command for command in commands if "install" in command)
        self.assertNotIn(str(self.dist / "members"), install)
        self.assertTrue(any("check" in command for command in commands))


if __name__ == "__main__":
    unittest.main()
