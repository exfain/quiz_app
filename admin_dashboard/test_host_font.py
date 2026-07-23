from pathlib import Path

from django.test import SimpleTestCase


REPO_ROOT = Path(__file__).resolve().parents[1]
HOST_CSS_PATH = REPO_ROOT / "static" / "host.css"
HOST_BASE_PATH = REPO_ROOT / "templates" / "admin_dashboard" / "base.html"
HOST_FONT_PATH = REPO_ROOT / "static" / "fonts" / "Coolvetica Rg Cond.otf"


class HostFontScopeTests(SimpleTestCase):
    def test_local_coolvetica_font_is_declared_for_host_scope(self):
        css = HOST_CSS_PATH.read_text(encoding="utf-8")

        self.assertTrue(HOST_FONT_PATH.is_file())
        self.assertEqual(HOST_FONT_PATH.read_bytes()[:4], b"OTTO")
        self.assertIn('font-family: "QA Host Coolvetica";', css)
        self.assertIn(
            'url("/static/fonts/Coolvetica%20Rg%20Cond.otf") format("opentype")',
            css,
        )
        self.assertIn("font-weight: 400;", css)
        self.assertIn("font-display: swap;", css)

    def test_host_base_is_the_only_root_that_loads_host_font_styles(self):
        base = HOST_BASE_PATH.read_text(encoding="utf-8")

        self.assertIn("{% static 'host.css' %}", base)
        self.assertIn('<body class="qa-host-root">', base)
        self.assertNotIn("fonts.googleapis.com", base)

        for template_path in (REPO_ROOT / "templates").rglob("*.html"):
            if template_path == HOST_BASE_PATH:
                continue
            with self.subTest(template=template_path.relative_to(REPO_ROOT)):
                content = template_path.read_text(encoding="utf-8")
                self.assertNotIn("{% static 'host.css' %}", content)
                self.assertNotIn('class="qa-host-root"', content)

    def test_host_variables_cover_text_titles_scores_and_form_controls(self):
        css = HOST_CSS_PATH.read_text(encoding="utf-8")

        self.assertIn('--host-font-family: "QA Host Coolvetica", sans-serif;', css)
        self.assertIn("--font-family-sans: var(--host-font-family);", css)
        self.assertIn("--font-family-game-title: var(--host-font-family);", css)
        self.assertIn("--font-family-mono: var(--host-font-family);", css)
        self.assertIn("--bs-body-font-family: var(--host-font-family);", css)
        for selector in ["button", "input", "textarea", "select", "option", "optgroup"]:
            self.assertIn(f".qa-host-root {selector}", css)

    def test_host_font_rules_do_not_use_global_or_icon_font_overrides(self):
        css = HOST_CSS_PATH.read_text(encoding="utf-8")

        self.assertNotIn("body {", css)
        self.assertNotIn("* {", css)
        self.assertNotIn("!important", css)
        self.assertNotIn("fontawesome", css.lower())
        self.assertNotIn("bootstrap-icons", css.lower())

    def test_normal_host_views_share_the_scoped_base(self):
        host_templates = list(
            (REPO_ROOT / "templates" / "admin_dashboard").glob("*.html")
        )
        host_templates += [
            REPO_ROOT / "templates" / "hub" / "create_session.html",
            REPO_ROOT / "templates" / "hub" / "monitor.html",
        ]

        excluded_standalone_templates = {HOST_BASE_PATH.name, "login.html"}
        for template_path in host_templates:
            if template_path.name in excluded_standalone_templates:
                continue
            with self.subTest(template=template_path.relative_to(REPO_ROOT)):
                content = template_path.read_text(encoding="utf-8")
                self.assertIn("admin_dashboard/base.html", content)

    def test_participant_and_public_roots_do_not_opt_into_host_font(self):
        public_templates = [
            "templates/quiz/play.html",
            "templates/estimation/play.html",
            "templates/assign/play.html",
            "templates/hub/lobby.html",
            "templates/hub/join_session.html",
            "templates/hub/spectate.html",
            "templates/admin_dashboard/login.html",
        ]

        for relative_path in public_templates:
            with self.subTest(template=relative_path):
                content = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertNotIn("host.css", content)
                self.assertNotIn("qa-host-root", content)
                self.assertNotIn("QA Host Coolvetica", content)
