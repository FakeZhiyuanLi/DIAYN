"""
The finder's own rate limit, which caps how often one person may have a
resume read.

    python3 -m unittest discover -s tests      # no install needed

A person is not going to upload six resumes in an hour; a script would, and
every read spends a worker process. These are the rules the limit has always
had: a rolling window, one person's use never counted against another's, a
refused attempt costing nothing, and a refund for an attempt that never ran.
"""

import unittest

from rate_limit import RateLimiter


class Limits(unittest.TestCase):
    def test_a_person_may_take_three_then_waits(self):
        limiter = RateLimiter(limit=3, window=3600)
        self.assertEqual([limiter.take(7, now=0) for _ in range(4)], [True, True, True, False])

    def test_the_window_rolls_rather_than_resetting(self):
        limiter = RateLimiter(limit=2, window=100)
        self.assertTrue(limiter.take(7, now=0))
        self.assertTrue(limiter.take(7, now=50))
        self.assertFalse(limiter.take(7, now=60))
        # The first one ages out; the second has not.
        self.assertTrue(limiter.take(7, now=101))
        self.assertFalse(limiter.take(7, now=102))

    def test_one_persons_limit_is_not_anothers(self):
        limiter = RateLimiter(limit=1, window=100)
        self.assertTrue(limiter.take(1, now=0))
        self.assertFalse(limiter.take(1, now=0))
        self.assertTrue(limiter.take(2, now=0))

    def test_it_says_how_long_the_wait_is(self):
        limiter = RateLimiter(limit=1, window=100)
        self.assertEqual(limiter.opens_in(7, now=0), 0)
        limiter.take(7, now=0)
        self.assertGreater(limiter.opens_in(7, now=40), 0)
        self.assertEqual(limiter.opens_in(7, now=101), 0)

    def test_a_refused_attempt_does_not_spend_a_slot(self):
        # Otherwise being rate limited would extend the rate limit.
        limiter = RateLimiter(limit=1, window=100)
        limiter.take(7, now=0)
        for _ in range(5):
            limiter.take(7, now=10)
        self.assertEqual(limiter.opens_in(7, now=10), 91)

    def test_it_counts_what_is_spent_within_the_window(self):
        limiter = RateLimiter(limit=5, window=100)
        self.assertEqual(limiter.used(7, now=0), 0)
        limiter.take(7, now=0)
        limiter.take(7, now=50)
        self.assertEqual(limiter.used(7, now=60), 2)
        self.assertEqual(limiter.used(7, now=120), 1)
        self.assertEqual(limiter.used(8, now=60), 0)


class Refunds(unittest.TestCase):
    """An attempt that never ran must not cost the person a slot."""

    def test_a_refunded_slot_can_be_used_again(self):
        limiter = RateLimiter(limit=1)
        self.assertTrue(limiter.take(7))
        self.assertFalse(limiter.take(7))
        limiter.refund(7)
        self.assertTrue(limiter.take(7))

    def test_a_refund_gives_back_only_the_last_slot(self):
        limiter = RateLimiter(limit=3)
        for _ in range(3):
            limiter.take(7)
        limiter.refund(7)
        self.assertTrue(limiter.take(7))
        self.assertFalse(limiter.take(7))

    def test_refunding_a_person_who_never_took_one_is_harmless(self):
        limiter = RateLimiter(limit=1)
        limiter.refund(99)
        self.assertTrue(limiter.take(99))


class Defaults(unittest.TestCase):
    def test_by_default_five_an_hour(self):
        limiter = RateLimiter()
        self.assertEqual([limiter.take(7, now=0) for _ in range(6)], [True] * 5 + [False])
        self.assertTrue(limiter.take(7, now=3600))

    def test_a_limit_or_window_below_one_is_refused(self):
        for kwargs in ({"limit": 0}, {"window": 0}, {"limit": -1}):
            with self.subTest(**kwargs):
                with self.assertRaises(ValueError):
                    RateLimiter(**kwargs)


if __name__ == "__main__":
    unittest.main()
