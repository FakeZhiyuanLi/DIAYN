"""
`hints`: the commands DIAYN's messages tell someone to type, spelled with the
Python that is actually running.

    python3 -m unittest discover -s tests      # no install needed

The quick start installs into .venv and never activates it, and a stock macOS
or Ubuntu has no `python` at all, so a hint that says bare `python` cannot be
copied. What is pinned here:

- an interpreter inside the checkout is shown relative to it, as typed there:
  `.venv/bin/python diayn.py setup`;
- one outside it is shown absolute, and quoted when the shell needs it;
- a venv's python, a symlink to the Python it was made from, stays the venv's;
- a checkout reached through a symlinked directory is still the checkout;
- the requirements go in with the running Python's own pip inside a venv, and
  into a new .venv, as the quick start makes one, outside any.

Nothing here runs an interpreter or touches the checkout: every path is built
in a temporary directory, or never looked at.
"""

import os
import sys
import tempfile
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TESTS)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import hints  # noqa: E402

CHECKOUT = "/home/someone/DIAYN"


class TheInterpreter(unittest.TestCase):
    def test_a_venv_in_the_checkout_is_shown_relative_to_it(self):
        self.assertEqual(hints.command("setup", executable=f"{CHECKOUT}/.venv/bin/python",
                                       checkout=CHECKOUT),
                         ".venv/bin/python diayn.py setup")

    def test_one_outside_the_checkout_is_shown_absolute(self):
        self.assertEqual(hints.command("run", executable="/usr/bin/python3", checkout=CHECKOUT),
                         "/usr/bin/python3 diayn.py run")

    def test_a_directory_that_only_starts_like_the_checkout_is_outside_it(self):
        self.assertEqual(hints.interpreter("/home/someone/DIAYN-old/.venv/bin/python",
                                           checkout=CHECKOUT),
                         "/home/someone/DIAYN-old/.venv/bin/python")

    def test_a_path_the_shell_would_split_is_quoted(self):
        self.assertEqual(hints.interpreter("/opt/my pythons/bin/python3", checkout=CHECKOUT),
                         "'/opt/my pythons/bin/python3'")
        self.assertEqual(hints.command("doctor", executable="/My Code/DIAYN/.venv/bin/python",
                                       checkout="/My Code/DIAYN"),
                         ".venv/bin/python diayn.py doctor")

    def test_an_interpreter_at_the_top_of_the_checkout_is_typed_with_a_slash(self):
        # Bare `python` would be looked up on PATH, which is the very mistake.
        self.assertEqual(hints.interpreter(f"{CHECKOUT}/python", checkout=CHECKOUT), "./python")

    def test_no_interpreter_known_falls_back_to_python3(self):
        self.assertEqual(hints.command("setup", executable="", checkout=CHECKOUT),
                         "python3 diayn.py setup")

    def test_every_argument_is_part_of_the_command(self):
        self.assertEqual(hints.command("sweep", "--init", executable=f"{CHECKOUT}/.venv/bin/python",
                                       checkout=CHECKOUT),
                         ".venv/bin/python diayn.py sweep --init")

    def test_by_default_it_is_the_running_python_and_this_checkout(self):
        shown = hints.interpreter()
        inside = os.path.abspath(sys.executable).startswith(os.path.join(ROOT, ""))
        if inside:
            self.assertEqual(shown, os.path.relpath(sys.executable, ROOT))
        else:
            self.assertEqual(shown, os.path.abspath(sys.executable))
        self.assertEqual(hints.CHECKOUT, ROOT)


class OnDisk(unittest.TestCase):
    """Symlinks, as a venv and a macOS temporary directory have them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = os.path.realpath(tmp.name)
        self.checkout = os.path.join(self.base, "DIAYN")
        self.bin = os.path.join(self.checkout, ".venv", "bin")
        os.makedirs(self.bin)
        self.elsewhere = os.path.join(self.base, "framework", "python3.12")
        os.makedirs(os.path.dirname(self.elsewhere))
        with open(self.elsewhere, "w", encoding="ascii"):
            pass

    def test_a_venv_python_that_links_elsewhere_stays_the_venvs(self):
        python = os.path.join(self.bin, "python")
        os.symlink(self.elsewhere, python)
        self.assertEqual(hints.interpreter(python, checkout=self.checkout), ".venv/bin/python")

    def test_a_checkout_named_through_a_symlinked_directory_is_still_the_checkout(self):
        link = os.path.join(self.base, "link-to-checkout")
        os.symlink(self.checkout, link)
        python = os.path.join(self.bin, "python")
        os.symlink(self.elsewhere, python)
        self.assertEqual(hints.interpreter(python, checkout=link), ".venv/bin/python")
        self.assertEqual(hints.interpreter(os.path.join(link, ".venv", "bin", "python"),
                                           checkout=self.checkout), ".venv/bin/python")


class Installing(unittest.TestCase):
    def test_inside_a_venv_the_running_pythons_own_pip_installs_them(self):
        said = hints.install_hint(executable=f"{CHECKOUT}/.venv/bin/python", checkout=CHECKOUT,
                                  in_venv=True)
        self.assertIn(".venv/bin/python -m pip install -r requirements.txt", said)

    def test_outside_a_venv_it_says_to_make_the_quick_starts(self):
        # A system Python's pip refuses to install (PEP 668) on Debian, Ubuntu and
        # Homebrew, so pointing it there would be a hint that cannot work.
        said = hints.install_hint(executable="/usr/bin/python3", checkout=CHECKOUT,
                                  in_venv=False)
        self.assertIn("python3 -m venv .venv && .venv/bin/pip install -r requirements.txt", said)
        self.assertIn(".venv/bin/python diayn.py", said)
        self.assertNotIn("/usr/bin/python3 -m pip", said)

    def test_by_default_it_asks_whether_this_python_is_a_venv(self):
        in_venv = sys.prefix != sys.base_prefix
        self.assertEqual(hints.install_hint(), hints.install_hint(in_venv=in_venv))


class ThePortal(unittest.TestCase):
    def test_names_the_toggle_and_where_it_is(self):
        for named in ("developer portal", "Bot", "Privileged Gateway Intents",
                      "Server Members Intent"):
            with self.subTest(named=named):
                self.assertIn(named, hints.INTENT_HOW)


if __name__ == "__main__":
    unittest.main()
