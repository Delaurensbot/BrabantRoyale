import hashlib
import unittest

from api.snapshot_history import token_matches


class SnapshotHistoryApiTests(unittest.TestCase):
    def test_token_hash_comparison(self):
        token = "test-scheduled-token"
        expected_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        self.assertTrue(token_matches(token, expected_hash))
        self.assertFalse(token_matches("wrong-token", expected_hash))
        self.assertFalse(token_matches("", expected_hash))


if __name__ == "__main__":
    unittest.main()
