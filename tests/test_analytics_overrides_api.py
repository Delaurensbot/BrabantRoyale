import unittest
from unittest.mock import Mock, patch

from api.analytics_overrides import update_override, validate_payload


class AnalyticsOverridesApiTests(unittest.TestCase):
    def test_validates_and_normalizes_override(self):
        payload = validate_payload(
            {
                "clan_tag": "#9yp8uy",
                "player_tag": "#qp0c9qg9",
                "race_created_at": "2026-07-20T09:42:06Z",
                "excluded": True,
                "reason": "Afwezig",
            }
        )

        self.assertEqual(payload["clan_tag"], "9YP8UY")
        self.assertEqual(payload["player_tag"], "QP0C9QG9")
        self.assertEqual(payload["race_created_at"], "2026-07-20T09:42:06Z")

    @patch("api.analytics_overrides.requests.post")
    def test_exclusion_write_forwards_admin_key_only_as_header(self, mocked_post):
        mocked_post.return_value = Mock(status_code=201)
        payload = {
            "clan_tag": "9YP8UY",
            "player_tag": "QP0C9QG9",
            "race_created_at": "2026-07-20T09:42:06Z",
            "excluded": True,
            "reason": "Afwezig",
        }

        result = update_override(payload, "beheer-key")

        self.assertEqual(result["action"], "excluded")
        headers = mocked_post.call_args.kwargs["headers"]
        self.assertEqual(headers["X-Analytics-Admin-Key"], "beheer-key")
        self.assertNotIn("beheer-key", mocked_post.call_args.args[0])

    @patch("api.analytics_overrides.requests.delete")
    def test_include_deletes_only_the_exact_override(self, mocked_delete):
        mocked_delete.return_value = Mock(status_code=204)
        payload = {
            "clan_tag": "9YP8UY",
            "player_tag": "QP0C9QG9",
            "race_created_at": "2026-07-20T09:42:06Z",
            "excluded": False,
            "reason": "",
        }

        result = update_override(payload, "beheer-key")

        self.assertEqual(result["action"], "included")
        params = mocked_delete.call_args.kwargs["params"]
        self.assertEqual(params["clan_tag"], "eq.9YP8UY")
        self.assertEqual(params["player_tag"], "eq.QP0C9QG9")


if __name__ == "__main__":
    unittest.main()

