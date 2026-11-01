"""Exercise detector failures and metadata exclusions in temporary fixtures."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "fixture"
        (self.root / "tools").mkdir(parents=True)
        shutil.copyfile(Path(__file__).with_name("sensitivity-scan.sh"),
                        self.root / "tools/sensitivity-scan.sh")
        (self.root / "README.md").write_text("Fictional public example\n")

    def scan(self, env=None):
        return subprocess.run(["bash", "tools/sensitivity-scan.sh"], cwd=self.root,
                              env=env, text=True, capture_output=True, timeout=20)

    def test_clean_fixture_passes(self):
        self.assertEqual(self.scan().returncode, 0)

    def test_worktree_git_metadata_is_not_publishable_source(self):
        private_path = "/" + "Users" + "/fictional-reviewer/repo/.git/worktrees/demo"
        (self.root / ".git").write_text("gitdir: " + private_path + "\n")
        self.assertEqual(self.scan().returncode, 0)

    def test_secret_shaped_fixture_is_detected(self):
        (self.root / "fixture.txt").write_text("gh" + "p_" + "a" * 40)
        self.assertEqual(self.scan().returncode, 1)

    def test_placeholder_does_not_hide_a_secret_on_the_same_line(self):
        (self.root / "fixture.txt").write_text("example.com " + "gh" + "p_" + "a" * 40)
        self.assertEqual(self.scan().returncode, 1)

    def test_broken_detector_fails_instead_of_reporting_clean(self):
        fake_bin = Path(self.temp.name) / "bin"
        fake_bin.mkdir()
        real_grep = shutil.which("grep")
        self.assertIsNotNone(real_grep)
        wrapper = fake_bin / "grep"
        wrapper.write_text('#!/bin/sh\nif [ "$1" = "-rnIE" ]; then\n'
                           'echo "simulated detector error" >&2\nexit 2\nfi\n'
                           'exec ' + shlex.quote(real_grep) + ' "$@"\n')
        wrapper.chmod(0o700)
        env = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ["PATH"])
        result = self.scan(env)
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("simulated detector error", result.stderr)
        self.assertNotIn("no configured pattern matched", result.stdout)


if __name__ == "__main__":
    unittest.main()
