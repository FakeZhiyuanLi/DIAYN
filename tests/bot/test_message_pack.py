"""
Packing text into Discord-sized messages.

    python3 -m unittest discover -s tests      # no install needed

`message_pack.pack` is the greedy packer the finder uses for long replies,
kept apart so that the finder's pure modules can use it without importing the
Discord client. These are the cases it is trusted with, plus the one promise
every caller leans on: no chunk is ever longer than the limit, because
Discord refuses a message over 2,000 characters outright and a refused message
is a reply the user never sees.
"""

import ast
import random
import sys
import unittest
from pathlib import Path

import message_pack
from message_pack import MAX_CHUNK, pack


class Packing(unittest.TestCase):
    def test_items_that_fit_share_one_chunk(self):
        # Arrange
        items = ["alpha", "beta", "gamma"]

        # Act
        chunks = pack(items, limit=100, sep="\n")

        # Assert
        self.assertEqual(chunks, ["alpha\nbeta\ngamma"])

    def test_a_chunk_is_closed_before_it_would_pass_the_limit(self):
        # Each item costs its length plus one separator, so three 4-char items
        # need 15 characters of budget and a limit of 10 takes two of them.
        chunks = pack(["aaaa", "bbbb", "cccc"], limit=10, sep="\n")

        self.assertEqual(chunks, ["aaaa\nbbbb", "cccc"])

    def test_an_item_longer_than_the_limit_is_hard_sliced(self):
        chunks = pack(["x" * 25], limit=10, sep="\n")

        self.assertEqual(chunks, ["x" * 10, "x" * 10, "x" * 5])

    def test_a_long_item_flushes_what_came_before_it(self):
        # The slices never share a chunk with their neighbours, so the short
        # item before the long one is not glued onto its first slice.
        chunks = pack(["head", "y" * 12, "tail"], limit=10, sep="\n")

        self.assertEqual(chunks, ["head", "y" * 10, "yy", "tail"])

    def test_empty_input_gives_no_chunks(self):
        self.assertEqual(pack([], limit=10, sep="\n"), [])

    def test_the_separator_is_counted(self):
        # "ab" + "--" + "cd" is six characters: it fits a limit of eight but
        # not of five, where the second item has to start a chunk of its own.
        self.assertEqual(pack(["ab", "cd"], limit=8, sep="--"), ["ab--cd"])
        self.assertEqual(pack(["ab", "cd"], limit=5, sep="--"), ["ab", "cd"])

    def test_defaults_are_the_bot_s_chunk_size_and_a_newline(self):
        self.assertEqual(MAX_CHUNK, 1850)
        line = "z" * 1000

        chunks = pack([line, line])

        self.assertEqual(chunks, [line, line])

    def test_the_input_list_is_left_alone(self):
        items = ["one", "two" * 10, "three"]
        before = list(items)

        pack(items, limit=8, sep=" ")

        self.assertEqual(items, before)

    def test_no_chunk_ever_exceeds_the_limit(self):
        # A property check over many random shapes, seeded so a failure repeats.
        rng = random.Random(20260928)
        for _ in range(500):
            limit = rng.randint(1, 60)
            sep = rng.choice(["\n", "\n\n", " | ", ""])
            items = ["q" * rng.randint(0, 90) for _ in range(rng.randint(0, 12))]
            with self.subTest(limit=limit, sep=sep, lengths=[len(i) for i in items]):
                chunks = pack(items, limit=limit, sep=sep)
                self.assertTrue(all(len(c) <= limit for c in chunks))
                # Nothing is lost: every character of every item is somewhere.
                self.assertEqual(sum(c.count("q") for c in chunks), sum(map(len, items)))

    def test_the_module_imports_nothing_but_the_standard_library(self):
        # Every finder module may import this, and the pure ones must load
        # under bare python3: no third-party import may creep in.
        tree = ast.parse(Path(message_pack.__file__).read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module}
        self.assertLessEqual(imported, set(sys.stdlib_module_names))


if __name__ == "__main__":
    unittest.main()
