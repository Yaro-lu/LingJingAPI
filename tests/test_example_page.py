import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE_PATH = ROOT / "examples" / "灵境造片厂示例页.html"


class LocalExamplePageContractTests(unittest.TestCase):
    def page(self) -> str:
        self.assertTrue(PAGE_PATH.is_file(), "本地示例页尚未创建")
        return PAGE_PATH.read_text(encoding="utf-8-sig")

    def test_page_is_branded_and_has_three_creation_categories(self):
        page = self.page()
        for marker in (
            "灵境造片厂",
            "本地 API 示例页",
            'data-category="text"',
            'data-category="image"',
            'data-category="video"',
            "文字生成",
            "图片生成",
            "视频生成",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, page)

    def test_page_discovers_names_then_loads_selected_schema_and_uses_existing_tasks(self):
        page = self.page()
        for marker in (
            "/v1/workflows?summary=true&available_only=true",
            "/schema",
            "/v1/workflows/run/",
            "/v1/tasks/",
            "Authorization",
            "Bearer",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, page)

    def test_page_has_no_external_frontend_assets_or_fixed_private_server(self):
        page = self.page()
        external_assets = re.findall(
            r"(?:src|href)\s*=\s*['\"]https?://",
            page,
            flags=re.IGNORECASE,
        )
        self.assertEqual(external_assets, [])
        self.assertNotRegex(
            page,
            r"\b100\.(?:6[4-9]|[78]\d|9\d|1[01]\d|12[0-7])(?:\.\d{1,3}){2}\b",
        )
        self.assertNotIn("fonts.googleapis.com", page)
        self.assertNotIn("cdn.jsdelivr.net", page)

    def test_page_remembers_connection_as_requested(self):
        page = self.page()
        self.assertIn('localStorage.getItem("lingjing_connection")', page)
        self.assertIn('JSON.stringify({url:state.baseUrl, key:state.apiKey})', page)

    def test_video_flow_requires_and_submits_first_and_last_frame_files(self):
        page = self.page()
        for marker in (
            'id="videoStartFrame"',
            'id="videoEndFrame"',
            'accept="image/png,image/jpeg,image/webp"',
            "fileToDataUrl",
            "body.start_image = startImage",
            "body.end_image = endImage",
            'modelRequiresInput(model, "start_image")',
            'modelRequiresInput(model, "end_image")',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, page)

    def test_image_flow_exposes_optional_reference_image_for_capable_models(self):
        page = self.page()
        for marker in (
            'id="imageReference"',
            'id="imageReferenceField"',
            'modelAcceptsInput(model, "image")',
            "body.image = referenceImage",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, page)

    def test_authenticated_follow_up_urls_are_pinned_to_client_origin(self):
        page = self.page()
        self.assertIn("if (target.origin === base.origin) return target.href", page)
        self.assertIn(
            "return new URL(`${target.pathname}${target.search}${target.hash}`.replace(/^[\\\\/]+/, \"\"), `${state.baseUrl}/`).href",
            page,
        )
        self.assertGreaterEqual(page.count('redirect: "error"'), 2)
        self.assertRegex(
            page,
            r"const statusPath = data\.status_path \|\| response\.headers\.get\(\"Location\"\) \|\| data\.status_url",
        )
        find_media = re.search(
            r"function findMediaUrl\(data\) \{(?P<body>.*?)\n    \}",
            page,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(find_media)
        body = find_media.group("body")
        self.assertLess(body.index("download_path"), body.index("download_url"))

    def test_plain_http_is_limited_to_loopback_hosts(self):
        page = self.page()
        self.assertIn(
            'new Set(["localhost", "127.0.0.1", "[::1]"])',
            page,
        )
        self.assertIn(
            'parsed.protocol === "http:" && !loopbackHosts.has(parsed.hostname.toLowerCase())',
            page,
        )
        self.assertIn("远程客户端必须使用 https://", page)


if __name__ == "__main__":
    unittest.main()
