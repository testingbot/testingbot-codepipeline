import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from testingbot_codepipeline import api, junit

from stub import KEY, SECRET, Stub, fixture


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.stub = Stub()
        self.client = api.Client(KEY, SECRET, api_base=self.stub.url)

    def tearDown(self):
        self.stub.close()

    def test_follows_pagination_to_meta_total(self):
        self.stub.builds["big"] = [dict(fixture()[0], id=i) for i in range(5)]
        with mock.patch.object(api, "PAGE_SIZE", 2):
            tests = self.client.build_tests("big")
        self.assertEqual([t["id"] for t in tests], [0, 1, 2, 3, 4])
        self.assertEqual(len(self.stub.requests), 3)
        self.assertIn("skip_fields=logs%2Cthumbs", self.stub.requests[0])

    def test_quotes_identifiers(self):
        self.stub.builds["codebuild-app:1/2"] = []
        self.assertEqual(self.client.build_tests("codebuild-app:1/2"), [])
        self.assertIn("/builds/codebuild-app%3A1%2F2?", self.stub.requests[0])

    def test_missing_build(self):
        with self.assertRaisesRegex(api.BuildNotFound, "No TestingBot build named 'nope'"):
            self.client.build_tests("nope")

    def test_bad_credentials(self):
        with self.assertRaises(api.AuthError):
            api.Client("wrong", "creds", api_base=self.stub.url).build_tests("mixed")

    def test_repr_hides_credentials(self):
        self.assertNotIn(SECRET, repr(self.client))

    def test_summarize_and_labels(self):
        tests = fixture() + [dict(fixture()[0], id=104, state="START")]
        c = api.summarize(tests)
        self.assertEqual((c["total"], c["passed"], c["failed"], c["unknown"], c["running"]), (4, 1, 1, 1, 1))
        self.assertEqual(api.browser_label(fixture()[0]), "Chrome 140 on WIN11")
        self.assertEqual(api.browser_label({"browser": "chrome", "browser_version": 140, "os": "LINUX"}), "chrome 140 on LINUX")


class JunitTest(unittest.TestCase):
    def test_renders_one_suite_per_platform(self):
        root = ET.fromstring(junit.render("mixed", fixture()))
        self.assertEqual((root.get("tests"), root.get("failures"), root.get("skipped"), root.get("time")), ("3", "1", "1", "47"))
        self.assertEqual([s.get("name") for s in root], ["Chrome 140 on WIN11", "Safari 17 on SONOMA", "Firefox 131 on WIN11"])

        failed = root.find(".//testcase[@name='checkout spec']")
        self.assertEqual(failed.find("failure").get("message"), "expected 'Welcome' but got 'Error' <&>")
        self.assertIn("https://testingbot.com/members/tests/102", failed.find("failure").text)

        unknown = root.find(".//testcase[@name='flaky spec']")
        self.assertIsNotNone(unknown.find("skipped"))
        self.assertNotIn("Video", unknown.find("system-out").text)

        passed = root.find(".//testcase[@name='login spec']")
        self.assertEqual(list(c.tag for c in passed), ["system-out"])
        self.assertIn("Video: https://example.test/101.mp4", passed.find("system-out").text)


if __name__ == "__main__":
    unittest.main()
