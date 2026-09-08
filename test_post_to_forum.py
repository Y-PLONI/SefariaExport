"""Tests for the forum publishing path.

The bug these pin down: the two changelog posts were sent in a tight loop, the
forum refused the second one ("ניתן לפרסם פוסט רק פעם ב-1 שניות"), and the
script printed "✅ Posted" for that refusal — so every "new books" post was
lost silently for eleven consecutive releases.
"""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import post_to_forum
from otzaria_forum import ForumPostError

RATE_LIMIT_MESSAGE = "ניתן לפרסם פוסט רק פעם ב-1 שניות - אנא המתינו לפני פרסום נוסף"


def ok(pid=1, url="https://otzaria.org/forum/post/1"):
    return {"status": {"code": "ok", "message": "OK"}, "response": {"pid": pid, "url": url}}


class FakeClient:
    """Replays a scripted sequence of outcomes per send_post call."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def send_post(self, content, topic_id, to_pid=None):
        self.calls.append((topic_id, content))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class SendPostsTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(post_to_forum.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)
        # send_posts narrates every refusal with ❌ and every retry with ⏳.
        # Left loose, those 19 lines land in the release job's "Validate release
        # tooling" step, so anyone grepping the weekly log for ❌ hits a false
        # positive on a passing test suite (audit of run 33987734987, §4).
        # Captured, not silenced: the assertions below read the buffer.
        self.stdout = io.StringIO()
        redirect = contextlib.redirect_stdout(self.stdout)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def output(self):
        return self.stdout.getvalue()

    def test_every_post_lands_when_the_forum_accepts(self):
        client = FakeClient([ok(1), ok(2)])
        failed = post_to_forum.send_posts(
            client, [("a", 1617, "x"), ("b", 1994, "y")], delay=1, attempts=3)
        self.assertEqual([], failed)
        self.assertEqual([1617, 1994], [topic for topic, _ in client.calls])

    def test_posts_are_spaced_so_the_second_is_not_refused(self):
        client = FakeClient([ok(1), ok(2)])
        post_to_forum.send_posts(
            client, [("a", 1617, "x"), ("b", 1994, "y")], delay=7, attempts=3)
        # Exactly one gap, before the second post and never before the first.
        self.assertEqual([mock.call(7)], self.sleep.call_args_list)

    def test_rate_limited_post_is_retried_until_it_lands(self):
        client = FakeClient([
            ok(1),
            ForumPostError("bad-request", RATE_LIMIT_MESSAGE),
            ok(2),
        ])
        failed = post_to_forum.send_posts(
            client, [("a", 1617, "x"), ("b", 1994, "y")], delay=1, attempts=3)
        self.assertEqual([], failed)
        self.assertEqual(3, len(client.calls))
        self.assertEqual(1994, client.calls[-1][0])
        self.assertIn("⏳", self.output())
        self.assertNotIn("❌", self.output())

    def test_backoff_grows_between_retries(self):
        client = FakeClient([
            ForumPostError("bad-request", RATE_LIMIT_MESSAGE),
            ForumPostError("bad-request", RATE_LIMIT_MESSAGE),
            ok(1),
        ])
        post_to_forum.send_posts(client, [("a", 1994, "x")], delay=2, attempts=4)
        self.assertEqual([mock.call(2), mock.call(4)], self.sleep.call_args_list)

    def test_persistent_refusal_is_reported_as_a_failure(self):
        client = FakeClient([ForumPostError("bad-request", RATE_LIMIT_MESSAGE)] * 3)
        failed = post_to_forum.send_posts(
            client, [("ספרים חדשים", 1994, "x")], delay=1, attempts=3)
        self.assertEqual(["ספרים חדשים"], failed)
        self.assertEqual(3, len(client.calls))
        self.assertIn("❌ Forum post NOT created for ספרים חדשים", self.output())

    def test_non_rate_limit_refusal_is_not_retried(self):
        client = FakeClient([ForumPostError("forbidden", "no-privileges")])
        failed = post_to_forum.send_posts(client, [("a", 1994, "x")], delay=1, attempts=4)
        self.assertEqual(["a"], failed)
        self.assertEqual(1, len(client.calls), "a permanent refusal must not be retried")

    def test_transport_error_is_never_retried(self):
        """The post may already exist; a retry would duplicate it in the thread."""
        client = FakeClient([OSError("connection reset")])
        failed = post_to_forum.send_posts(client, [("a", 1994, "x")], delay=1, attempts=4)
        self.assertEqual(["a"], failed)
        self.assertEqual(1, len(client.calls))
        self.assertIn("undetermined outcome", self.output())

    def test_every_line_this_class_prints_stays_out_of_the_ci_log(self):
        """The capture above is the fix; this is what proves it still holds."""
        self.assertIs(sys.stdout, self.stdout, "setUp must own stdout for every test here")
        client = FakeClient([ok(1)])
        post_to_forum.send_posts(client, [("a", 1617, "x")], delay=1, attempts=1)
        self.assertIn("✅ Posted to forum topic 1617", self.output())

    def test_one_failure_does_not_stop_the_remaining_posts(self):
        client = FakeClient([ForumPostError("forbidden", "no"), ok(2)])
        failed = post_to_forum.send_posts(
            client, [("a", 1617, "x"), ("b", 1994, "y")], delay=1, attempts=1)
        self.assertEqual(["a"], failed)
        self.assertEqual([1617, 1994], [topic for topic, _ in client.calls])


class ForumPostErrorTest(unittest.TestCase):
    def test_the_hebrew_rate_limit_message_is_retryable(self):
        self.assertTrue(ForumPostError("bad-request", RATE_LIMIT_MESSAGE).retryable)

    def test_nodebb_error_keys_are_retryable_across_locales(self):
        self.assertTrue(ForumPostError("bad-request", "[[error:too-many-posts, 10]]").retryable)
        self.assertTrue(ForumPostError("bad-request", "[[error:still-posting]]").retryable)

    def test_other_refusals_are_not_retryable(self):
        self.assertFalse(ForumPostError("forbidden", "[[error:no-privileges]]").retryable)
        self.assertFalse(ForumPostError("bad-request", "").retryable)


class SendPostStatusTest(unittest.TestCase):
    """send_post must decide on `status.code`, not on the HTTP transport."""

    def _client(self, response):
        from otzaria_forum import OtzariaForumClient
        client = OtzariaForumClient("u", "p")
        client.csrf_token = "t"
        client.session = mock.Mock()
        client.session.post.return_value = response
        return client

    def test_ok_status_returns_the_payload(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = ok(7)
        self.assertEqual(ok(7), self._client(response).send_post("x", 1994))

    def test_refusal_raises_even_though_a_body_came_back(self):
        response = mock.Mock(status_code=400)
        response.json.return_value = {
            "status": {"code": "bad-request", "message": RATE_LIMIT_MESSAGE},
            "response": {},
        }
        with self.assertRaises(ForumPostError) as caught:
            self._client(response).send_post("x", 1994)
        self.assertEqual("bad-request", caught.exception.code)
        self.assertTrue(caught.exception.retryable)

    def test_non_json_response_raises(self):
        response = mock.Mock(status_code=502, text="<html>bad gateway</html>")
        response.json.side_effect = ValueError("no json")
        with self.assertRaises(ForumPostError) as caught:
            self._client(response).send_post("x", 1994)
        self.assertEqual("http-502", caught.exception.code)
        self.assertFalse(caught.exception.retryable)


class PostSelectionTest(unittest.TestCase):
    """`--only` / `--as-of` exist so a lost post can be re-published faithfully."""

    def _diff(self, **books):
        base = {"added": [], "removed": [], "he_renamed": [], "en_renamed": [],
                "moved": [], "content_changed": []}
        base.update(books)
        return {"new_tag": "2026-08-20_18-42-32387557930-1", "old_tag": "prev",
                "has_baseline": True, "books": base, "versions": {"added": []}}

    def _run(self, diff, argv):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "diff.json"
            path.write_text(json.dumps(diff), encoding="utf-8")
            with mock.patch("sys.argv", ["post_to_forum.py", str(path)] + argv), \
                 mock.patch.dict("os.environ", {"POST_TO_FORUM": "false"}, clear=False), \
                 mock.patch("builtins.print") as printed:
                code = post_to_forum.main()
        return code, "\n".join(str(c.args[0]) for c in printed.call_args_list if c.args)

    def test_only_new_books_writes_just_that_thread(self):
        diff = self._diff(added=[{"en": "The Koren Shalem Siddur; Ashkenaz",
                                 "he": "סידור קורן השלם; אשכנז"}],
                          content_changed=[{"en": "Genesis", "he": "בראשית"}])
        code, out = self._run(diff, ["--only", "new-books"])
        self.assertEqual(0, code)
        self.assertIn("topic 1994", out)
        self.assertNotIn("topic 1617", out)
        self.assertIn("סידור קורן השלם; אשכנז", out)

    def test_only_new_books_stays_silent_when_there_is_nothing_new(self):
        diff = self._diff(content_changed=[{"en": "Genesis", "he": "בראשית"}])
        code, out = self._run(diff, ["--only", "new-books"])
        self.assertEqual(0, code)
        self.assertIn("Nothing new in this diff", out)

    def test_as_of_dates_the_post_by_the_original_release(self):
        diff = self._diff(added=[{"en": "X", "he": "ספר"}])
        code, out = self._run(diff, ["--only", "new-books", "--as-of", "2026-08-20"])
        self.assertEqual(0, code)
        self.assertIn(post_to_forum.heb_date(date(2026, 8, 20)), out)

    def test_default_still_writes_both_threads(self):
        diff = self._diff(added=[{"en": "X", "he": "ספר"}],
                          content_changed=[{"en": "Genesis", "he": "בראשית"}])
        code, out = self._run(diff, [])
        self.assertEqual(0, code)
        self.assertIn("topic 1617", out)
        self.assertIn("topic 1994", out)


class HebrewDateTest(unittest.TestCase):
    def test_an_explicit_day_is_honoured(self):
        self.assertEqual(post_to_forum.heb_date(date(2026, 8, 20)),
                         post_to_forum.heb_date(date(2026, 8, 20)))
        self.assertNotEqual(post_to_forum.heb_date(date(2026, 8, 20)),
                            post_to_forum.heb_date(date(2026, 9, 2)))


if __name__ == "__main__":
    unittest.main()
