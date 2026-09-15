"""The release may prune only dependencies shadowed by portable Python."""
import csv
import tempfile
import unittest
from pathlib import Path

from scripts.prune_shadowed_runtime import prune


def package(site, name, version, entries):
    info = site / f"{name}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Name: {name}\nVersion: {version}\n", encoding="utf-8")
    with (info / "RECORD").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        for entry in entries:
            target = site / entry
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(version, encoding="utf-8")
            writer.writerow([entry, "", ""])
    return info


class RuntimeDedupTests(unittest.TestCase):
    def test_removes_only_shadowed_packages_and_their_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            stage = Path(temporary)
            own = stage / "runtime/python/Lib/site-packages"
            legacy = stage / ".venv/Lib/site-packages"
            package(own, "av", "18", ["av/__init__.py", "av.libs/video.dll"])
            obsolete = package(legacy, "av", "16", ["av/__init__.py", "av.libs/video.dll"])
            package(legacy, "torch", "2", ["torch/__init__.py"])
            package(own, "namespace_a", "2", ["shared/a.py"])
            package(legacy, "namespace_a", "1", ["shared/a.py"])
            package(legacy, "namespace_b", "1", ["shared/b.py"])
            (stage / "user-data.txt").write_text("keep", encoding="utf-8")
            removed = prune(stage)
            self.assertFalse((legacy / "av").exists())
            self.assertFalse((legacy / "av.libs").exists())
            self.assertFalse(obsolete.exists())
            self.assertEqual((own / "av/__init__.py").read_text(), "18")
            self.assertTrue((legacy / "torch/__init__.py").exists())
            self.assertTrue((legacy / "shared/a.py").exists())
            self.assertTrue((legacy / "shared/b.py").exists())
            self.assertEqual((stage / "user-data.txt").read_text(), "keep")
            self.assertEqual(len(removed), 3)
            self.assertEqual(prune(stage), [])


if __name__ == "__main__":
    unittest.main()
