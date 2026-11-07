"""The bridge has no irreversible code path and no network code. Checked by reading the source."""
import ast
from pathlib import Path
import re
import unittest

from boundary import IRREVERSIBLE, BoundaryError, handoff, official_link, require_reversible
from candidates import from_api
from fixtures import ALIASES, OFFICIAL_HOSTS, POLICY, api_row
from policy import ValuePolicy

ROOT = Path(__file__).resolve().parent.parent
MODULES = sorted(p for p in ROOT.glob("*.py"))
FORBIDDEN_NAME = re.compile(r"(^|_)(book|booking|purchase|pay|payment|checkout|transfer|redeem|cancel|hold)(_|$)")
NETWORK_IMPORTS = {"socket", "ssl", "http", "urllib.request", "smtplib", "imaplib", "poplib",
                   "ftplib", "telnetlib", "requests", "httpx", "aiohttp", "subprocess", "webbrowser"}


class BoundaryTests(unittest.TestCase):
    def test_modules_exist(self):
        self.assertGreaterEqual(len(MODULES), 6, [p.name for p in MODULES])

    def test_no_function_or_method_is_an_irreversible_action(self):
        for path in MODULES:
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    self.assertIsNone(FORBIDDEN_NAME.search(node.name), f"{path.name}:{node.name}")
                if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                    self.assertIsNone(FORBIDDEN_NAME.search(node.attr), f"{path.name}:.{node.attr}")

    def test_no_module_can_reach_the_network(self):
        for path in MODULES:
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    for banned in NETWORK_IMPORTS:
                        self.assertFalse(name == banned or name.startswith(banned + "."),
                                         f"{path.name} imports {name}")

    def test_irreversible_actions_are_refused_and_unknown_ones_are_not_guessed(self):
        for action in sorted(IRREVERSIBLE):
            with self.subTest(action=action), self.assertRaises(BoundaryError):
                require_reversible(action)
        with self.assertRaises(BoundaryError):
            require_reversible("buy_now")
        self.assertEqual(require_reversible("rearm_alert"), "rearm_alert")

    def test_handoff_links_must_be_https_on_an_official_host(self):
        for url in ("http://awards.example.com/x", "https://awards.example.com.evil.example.com/x",
                    "https://user:pw@awards.example.com/x", "https://other.example.com/x"):
            with self.subTest(url=url), self.assertRaises(BoundaryError):
                official_link(url, OFFICIAL_HOSTS)

    def test_handoff_states_what_was_not_done(self):
        c = from_api(api_row(), aliases=ALIASES)
        v = ValuePolicy.from_config(POLICY).evaluate(c)
        text = handoff(c, v, "https://awards.example.com/search?id=row-7781", allowed_hosts=OFFICIAL_HOSTS)
        self.assertIn("Verification: CHECK NEEDED. Checkout: NOT REACHED.", text)
        self.assertIn("Nothing was booked, held, paid or transferred.", text)
        self.assertIn("65,000 points + 89.40 USD", text)
        with self.assertRaises(ValueError):
            handoff(c, v, "https://awards.example.com/x", allowed_hosts=OFFICIAL_HOSTS, verification="BOOKED")

    def test_handoff_names_every_unconfirmed_field(self):
        c = from_api(api_row(flights=["NW 401", "NW 88"], seats=0, currency=None), aliases=ALIASES)
        v = ValuePolicy.from_config(POLICY).evaluate(c)
        text = handoff(c, v, "https://awards.example.com/search?id=row-7781", allowed_hosts=OFFICIAL_HOSTS)
        self.assertIn("89.40 (currency not stated)", text)
        self.assertIn("Unconfirmed, check on the official page: fee_currency_unconfirmed, "
                      "seats_unconfirmed, connection_unconfirmed", text)


if __name__ == "__main__":
    unittest.main()
