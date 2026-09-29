import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class LightweightReleaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.release_script = (ROOT / "scripts" / "build_release.ps1").read_text(
            encoding="utf-8-sig"
        )
        cls.installer_script = (ROOT / "installer" / "LingJing.iss").read_text(
            encoding="utf-8-sig"
        )
        cls.readme = (ROOT / "README.md").read_text(encoding="utf-8-sig")
        cls.readme_en = (ROOT / "README_EN.md").read_text(encoding="utf-8-sig")
        cls.license_path = ROOT / "LICENSE"
        cls.open_source_notice_path = ROOT / "开源许可说明.txt"
        cls.version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        cls.runtime_script = (ROOT / "scripts" / "build_runtime_release.ps1").read_text(
            encoding="utf-8-sig"
        )
        cls.runtime_release = json.loads(
            (ROOT / "app" / "runtime_release.json").read_text(encoding="utf-8-sig")
        )
        cls.guide_path = ROOT / "docs" / "灵境造片厂使用教学.pdf"
        cls.example_path = ROOT / "examples" / "灵境造片厂示例页.html"

    def test_release_does_not_copy_the_heavy_ai_environment(self):
        forbidden_copy_commands = (
            "Copy-AllowlistedTree -Source (Join-Path $SourceRoot 'runtime\\ComfyUI')",
            "Copy-AllowlistedTree -Source (Join-Path $SourceRoot '.venv\\Lib')",
            "Copy-AllowlistedTree -Source (Join-Path $SourceRoot '.venv\\share')",
            "Copy-RequiredFile -Source (Join-Path $SourceRoot 'bin\\cloudflared.exe')",
        )
        for command in forbidden_copy_commands:
            with self.subTest(command=command):
                self.assertNotIn(command, self.release_script)

        self.assertIn("runtime/comfyui", self.release_script.lower())
        self.assertIn("cloudflared", self.release_script.lower())
        self.assertIn("site-packages/(?:torch", self.release_script.lower())

    def test_release_contains_a_self_bootstrapping_gui_and_extractor(self):
        for marker in (
            "BootstrapPythonRoot",
            "BootstrapSitePackagesRoot",
            "SevenZipRoot",
            "$BootstrapPackageEntries",
            "7z.exe",
            "7z.dll",
            "7-Zip-License.txt",
            "bootstrap-python-separate-ai-runtime",
            "environment_included = $false",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.release_script)

        self.assertIn('MyAppExe "runtime\\python\\pythonw.exe"', self.installer_script)
        self.assertIn("轻量客户端", self.installer_script)

    def test_release_bootstrap_includes_http_client_and_smoke_tests_real_entrypoint(self):
        for package_entry in (
            "certifi",
            "charset_normalizer",
            "idna",
            "requests",
            "urllib3",
        ):
            with self.subTest(package_entry=package_entry):
                self.assertIn(f"'{package_entry}'", self.release_script)
                self.assertIn(f"'{package_entry}-*.dist-info'", self.release_script)

        self.assertIn("'*__mypyc*.pyd'", self.release_script)

        for marker in (
            "Assert-StagedClientImportable",
            "import charset_normalizer",
            "runpy.run_path",
            "__release_startup_smoke__",
            "LINGJING_STARTUP_IMPORT_OK",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.release_script)

    def test_release_requires_and_stages_the_comfyui_updater(self):
        updater_files = (
            "app\\core\\comfyui_update.py",
            "app\\core\\comfyui_update_worker.py",
            "app\\comfyui_release.json",
        )
        for relative_path in updater_files:
            with self.subTest(relative_path=relative_path):
                self.assertGreaterEqual(
                    self.release_script.count(f"'{relative_path}'"),
                    2,
                    "Updater files must be required inputs and required staged files",
                )

    def test_release_requires_and_stages_runtime_update_handoff(self):
        for relative_path in (
            "app\\core\\runtime_update.py",
            "app\\core\\runtime_update_helper.ps1",
        ):
            with self.subTest(relative_path=relative_path):
                self.assertGreaterEqual(
                    self.release_script.count(f"'{relative_path}'"),
                    2,
                    "Runtime handoff files must be required inputs and staged files",
                )

    def test_release_policy_excludes_git_comfyui_core_and_torch(self):
        for marker in (
            "(^|/)git\\.exe$",
            "^runtime/comfyui(/|$)",
            "^\\.venv/lib/site-packages/(?:torch|torchaudio|torchvision|triton|nvidia)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.release_script.lower())

    def test_release_stages_pip_only_for_the_isolated_updater_runtime(self):
        for marker in (
            "Join-Path $BootstrapPythonRoot 'Lib\\site-packages'",
            "Join-Path $UpdaterPipSitePackagesRoot 'pip'",
            "app\\updater_runtime\\pip",
            "app\\updater_runtime\\pip-LICENSE.txt",
            "app\\updater_runtime\\pip\\__main__.py",
            "^app/updater_runtime/(?!pip(?:/|$)|pip-license\\.txt$)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker.lower(), self.release_script.lower())

        self.assertNotIn(
            "Destination (Join-Path $StageRoot 'runtime\\python\\Lib\\site-packages\\pip')",
            self.release_script,
        )
        self.assertNotIn(
            "Copy-AllowlistedTree -Source (Join-Path $BootstrapPythonRoot "
            "'Lib\\site-packages\\setuptools')",
            self.release_script,
        )

    def test_release_secret_scan_includes_fine_grained_github_tokens(self):
        self.assertIn("github_pat_", self.release_script)

    def test_release_rechecks_policy_after_startup_smoke_test(self):
        smoke_index = self.release_script.index(
            "Assert-StagedClientImportable -StageRoot $StageRoot"
        )
        rescan_index = self.release_script.index(
            "Assert-StagingPolicy -StageRoot $StageRoot",
            smoke_index,
        )
        self.assertGreater(rescan_index, smoke_index)

    def test_readme_documents_separate_program_environment_and_models(self):
        for marker in ("轻量客户端", "独立运行环境包", "不包含模型"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.readme)

    def test_readme_omits_download_links_and_matches_current_version(self):
        self.assertNotIn("releases/download/", self.readme)
        self.assertIn(f"当前版本：`{self.version}`", self.readme)

    def test_project_declares_apache_2_and_stages_the_license(self):
        self.assertTrue(self.license_path.is_file())
        license_text = self.license_path.read_text(encoding="utf-8")
        self.assertIn("Apache License", license_text)
        self.assertIn("Version 2.0, January 2004", license_text)
        self.assertIn("END OF TERMS AND CONDITIONS", license_text)
        for notice_name in (
            "LICENSE",
            "THIRD_PARTY_NOTICES.md",
            "开源许可说明.txt",
        ):
            with self.subTest(notice_name=notice_name):
                self.assertGreaterEqual(
                    self.release_script.count(f"'{notice_name}'"),
                    3,
                    "License notices must be required inputs, copied, and verified",
                )

        for readme in (self.readme, self.readme_en):
            with self.subTest(readme=readme[:20]):
                self.assertIn("Apache License 2.0", readme)

        self.assertNotIn("仅可用于学习、测试与评估", self.readme)
        self.assertTrue(self.open_source_notice_path.is_file())
        notice = self.open_source_notice_path.read_text(encoding="utf-8-sig")
        self.assertIn("由项目作者拥有版权并有权授权", notice)
        self.assertIn("第三方组件、外部工作流、模型和素材", notice)
        self.assertFalse((ROOT / "公开非商业版本说明.txt").exists())

    def test_runtime_builder_supports_external_source_and_emits_release_manifest(self):
        for marker in (
            "$RuntimeSourceRoot",
            "runtime-nvidia-rtx20plus-cu130",
            "$ReleaseManifestPath",
            "schema_version = 1",
            "size_bytes =",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.runtime_script)

    def test_runtime_builder_uses_and_enforces_project_version(self):
        self.assertIn('$ProjectVersionPath = Join-Path $ProjectDir "VERSION"', self.runtime_script)
        self.assertIn("does not match project VERSION", self.runtime_script)

    def test_runtime_release_manifest_pins_one_exact_asset(self):
        package_name = self.runtime_release["package_name"]
        self.assertTrue(package_name.startswith("runtime-nvidia-"))
        self.assertTrue(package_name.endswith(".7z"))
        self.assertGreater(self.runtime_release["size_bytes"], 0)
        self.assertEqual(len(self.runtime_release["sha256"]), 64)
        self.assertTrue(self.runtime_release["download_url"].endswith(f"/{package_name}"))

    def test_installer_creates_a_branded_root_launcher(self):
        self.assertIn('#define MyAppName "灵境 · LingJingAPI"', self.installer_script)
        self.assertIn('Name: "{app}\\{#MyAppName}"', self.installer_script)
        self.assertIn(
            'IconFilename: "{app}\\app\\gui\\assets\\app.ico"',
            self.installer_script,
        )

    def test_release_stages_the_tutorial_pdf_at_the_install_root(self):
        self.assertTrue(self.guide_path.is_file())
        self.assertGreater(self.guide_path.stat().st_size, 10_000)
        self.assertEqual(self.guide_path.read_bytes()[:5], b"%PDF-")
        self.assertIn("docs\\灵境造片厂使用教学.pdf", self.release_script)
        self.assertIn("灵境造片厂使用教学.pdf", self.release_script)

    def test_readme_describes_manual_download_and_installed_tutorial(self):
        for marker in ("https://github.com/Yaro-lu/LingJingAPI", "一键修复", "拉取失败", "使用教学 PDF"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.readme)

    def test_release_stages_local_example_page_at_install_root(self):
        self.assertTrue(self.example_path.is_file())
        self.assertIn("examples\\灵境造片厂示例页.html", self.release_script)
        self.assertIn("灵境造片厂示例页.html", self.release_script)

    def test_installer_preserves_the_restored_14b_visual_workflow_on_upgrade(self):
        old_visual_workflow_delete = (
            'Type: files; Name: "{app}\\workflows\\wan_flf2v_v1\\'
            'wan2.1_flf2v_720_f16.json"'
        )

        self.assertNotIn(old_visual_workflow_delete, self.installer_script)

    def test_installer_creates_same_logo_desktop_example_shortcut(self):
        self.assertIn('Name: "{autodesktop}\\灵境 · LingJingAPI 示例页"', self.installer_script)
        self.assertIn('Filename: "{app}\\灵境造片厂示例页.html"', self.installer_script)
        self.assertIn(
            'IconFilename: "{app}\\app\\gui\\assets\\app.ico"',
            self.installer_script,
        )
        desktop_task = next(
            line
            for line in self.installer_script.splitlines()
            if line.startswith('Name: "desktopicon";')
        )
        self.assertNotIn("unchecked", desktop_task)

    def test_uninstaller_removes_program_files_but_preserves_mutable_data(self):
        removable_directories = (
            ".venv",
            "app",
            "bin",
        )
        for directory in removable_directories:
            with self.subTest(directory=directory):
                self.assertIn(
                    f'Type: filesandordirs; Name: "{{app}}\\{directory}"',
                    self.installer_script,
                )

        preserved_directories = (
            "cache", "models", "runtime", "workflows", "outputs",
            ".runtime-install-staging-*", ".runtime-install-backup-*",
            ".comfyui-update-staging-*", ".comfyui-update-overlay-*",
            ".comfyui-update-backup-*",
        )
        for directory in preserved_directories:
            with self.subTest(directory=directory):
                self.assertNotIn(
                    f'Type: filesandordirs; Name: "{{app}}\\{directory}"',
                    self.installer_script,
                )

        self.assertIn("PreservedModelsPath := ConfiguredDataPath('models')", self.installer_script)
        self.assertIn("PreservedOutputsPath := ConfiguredDataPath('outputs')", self.installer_script)
        self.assertIn("CurUninstallStep = usPostUninstall", self.installer_script)
        self.assertIn("模型和生成结果没有自动删除", self.installer_script)
        self.assertIn("UninstallLogMode=overwrite", self.installer_script)

    def test_uninstaller_preserves_root_outputs_and_never_recursively_deletes_app_root(self):
        uninstall_section = self.installer_script.split("[UninstallDelete]", 1)[1]
        uninstall_section = uninstall_section.split("[Tasks]", 1)[0]

        self.assertNotIn('Name: "{app}\\outputs"', uninstall_section)
        self.assertNotIn('Name: "{app}\\outputs\\*"', uninstall_section)
        self.assertNotIn('Name: "{app}"', uninstall_section)
        self.assertNotIn('Name: "{app}\\*"', uninstall_section)


if __name__ == "__main__":
    unittest.main()
