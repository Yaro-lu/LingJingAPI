import json
from pathlib import Path
import tempfile
import shutil
import subprocess
import unittest
from unittest.mock import patch
from app.core.single_user_assets import gateway_id, private_host, lan_urls, image_preview, save_record, load_record

class SingleUserAssetsTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg needed to create a tiny video fixture")
    def test_video_preview_extracts_first_frame_and_reuses_cache(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "首帧测试.mp4"
            subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                            "-f", "lavfi", "-i", "color=c=red:s=800x480:r=2", "-t", "1",
                            "-c:v", "mpeg4", str(source)], check=True, capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            preview = image_preview(source, root / "cache")
            with Image.open(preview) as image:
                self.assertEqual(image.size, (640, 384))
                red, green, blue = image.convert("RGB").getpixel((30, 30))
                self.assertGreater(red, 200)
                self.assertLess(green, 30)
            stamp = preview.stat().st_mtime_ns
            with patch("app.core.single_user_assets._video_first_frame", side_effect=AssertionError("cache miss")):
                self.assertEqual(image_preview(source, root / "cache").stat().st_mtime_ns, stamp)

    def test_identity_and_record_independent_of_keys(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            self.assertEqual(gateway_id(root),gateway_id(root))
            save_record(root,{"task_id":"one","status":"completed","outputs":[],"api_key":"secret"})
            self.assertEqual(load_record(root,"one")["status"],"completed")
            self.assertNotIn("secret",(root/"task-results/one.json").read_text())
            self.assertEqual(load_record(root,"../one"),{})
    def test_private_address_boundary(self):
        for host in ("127.0.0.1","::1","192.168.1.5","10.1.1.2","172.16.0.2"):
            self.assertTrue(private_host(host))
        for host in ("8.8.8.8","172.32.0.1","100.64.0.1","0.0.0.0"):
            self.assertFalse(private_host(host))
    def test_thumbnail_reuses_cache(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/"image.png"
            Image.new("RGB",(1200,800),"blue").save(source)
            first=image_preview(source,root/"cache"); modified=first.stat().st_mtime_ns
            self.assertEqual(image_preview(source,root/"cache"),first)
            self.assertEqual(first.stat().st_mtime_ns,modified)
