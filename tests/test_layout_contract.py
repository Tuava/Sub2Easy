"""Static UI contracts only: no server, browser, application import or user data."""

from collections import Counter
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
import re
import unittest


STATIC = Path(__file__).resolve().parents[1] / "sub2easy" / "static"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}

# IDs present before the layout change. Existing JS integrations must survive.
LEGACY_IDS = set("""
nav-count job-count lock-btn runtime-version breadcrumb-current connection-pill
compact-lock page-pool sync-btn pool-mode-description pool-mode-tag stat-total
stat-ready stat-attention stat-cloud cloud-time pool-count search status-filter
local-binding local-monitor local-group local-proxy local-profile local-sort
local-reset select-all selected-count deploy-selected batch-bind-selected
monitor-selected authorize-selected account-rows empty-pool no-matches
local-page-info local-prev local-next refresh-time page-bindings bind-all
bind-retry binding-report-summary binding-result-filter binding-report-rows
binding-empty page-cloud cloud-reload cloud-query cloud-platform cloud-status
cloud-group cloud-proxy cloud-binding cloud-since cloud-until cloud-sort
cloud-apply cloud-reset cloud-list-status cloud-list-rows cloud-empty
cloud-page-info cloud-prev cloud-next page-import sample-btn import-format
import-format-help materials-label materials material-help material-file-label
material-file sub2-update-wrap sub2-update preview-btn import-btn import-result
import-profile-summary page-jobs cancel-queued deploy-report job-rows empty-jobs
page-monitor monitor-check monitor-stop monitor-state monitor-runtime
monitor-scope-count monitor-form monitor-interval monitor-grace monitor-model
monitor-budget monitor-resume monitor-resume-paused monitor-save monitor-rows
monitor-empty page-settings connection-form cookie-indicator nvt-cookie
oauth-client-id sub2-url admin-indicator admin-key clear-cookie clear-admin
profile-form choices-status reload-choices profile-name staging-group
target-groups-label target-groups target-groups-summary target-group-options
proxy-id concurrency priority rate fingerprint account-expiry auto-pause
selection-warning save-profile monitor-footer lock-screen unlock-form
unlock-title unlock-description master-password confirm-label confirm-password
unlock-submit unlock-error action-dialog dialog-title dialog-message
dialog-input-wrap dialog-input-label dialog-input dialog-check-wrap dialog-check
dialog-check-label dialog-confirm binding-dialog binding-dialog-title
binding-close binding-dialog-description candidate-query candidate-eligible
candidate-sort candidate-list deployment-dialog deployment-form deployment-close
deployment-summary deployment-model deployment-confirm deployment-isolation-wrap
deployment-isolation deployment-submit deployment-error toast
""".split())


@dataclass
class Element:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: "Element | None" = field(default=None, repr=False)

    @property
    def classes(self):
        return set(self.attrs.get("class", "").split())

    @property
    def text(self):
        raw = "".join(item if isinstance(item, str) else item.text
                      for item in self.children)
        return " ".join(raw.split())

    def elements(self):
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.elements()

    def ancestor(self, *, tag=None, css_class=None):
        node = self.parent
        while node:
            if (tag is None or node.tag == tag) and (
                    css_class is None or css_class in node.classes):
                return node
            node = node.parent
        return None


class Document(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]
        self.errors = []
        self.feed(source)
        self.close()
        if len(self.stack) != 1:
            self.errors.append("Unclosed tags: " + str([n.tag for n in self.stack[1:]]))

    def handle_starttag(self, tag, attrs):
        if len(dict(attrs)) != len(attrs):
            self.errors.append(f"Duplicate attribute on {tag}")
        node = Element(tag, dict(attrs), parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        if len(self.stack) > 1 and self.stack[-1].tag == tag:
            self.stack.pop()
        else:
            self.errors.append(f"Unexpected closing tag {tag}")

    def handle_data(self, data):
        self.stack[-1].children.append(data)


class LayoutContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.css = (STATIC / "style.css").read_text(encoding="utf-8")
        cls.document = Document(cls.html)
        cls.nodes = list(cls.document.root.elements())
        cls.ids = {node.attrs["id"]: node for node in cls.nodes if "id" in node.attrs}

    def test_markup_balanced_and_ids_unique(self):
        self.assertEqual(self.document.errors, [])
        counts = Counter(n.attrs["id"] for n in self.nodes if "id" in n.attrs)
        self.assertEqual([key for key, count in counts.items() if count != 1], [])

    def test_all_legacy_ids_and_script_order_preserved(self):
        self.assertEqual(LEGACY_IDS - self.ids.keys(), set())
        scripts = [n.attrs.get("src") for n in self.nodes if n.tag == "script"]
        self.assertEqual(scripts, ["/app.js", "/binding-ui.js"])
        for filename in ("app.js", "binding-ui.js"):
            source = (STATIC / filename).read_text(encoding="utf-8")
            references = set(re.findall(r"\$\(\s*['\"]([^'\"]+)['\"]\s*\)", source))
            self.assertEqual(references - self.ids.keys(), set(), filename)

    def test_local_usage_column_after_binding_before_template(self):
        self.check_headers("account-rows", [
            "选择", "账号 / 本地身份", "状态", "云端绑定", "用量 / 重置", "导入模板", "操作",
        ])

    def test_cloud_usage_column_before_dates(self):
        self.check_headers("cloud-list-rows", [
            "ID / 名称", "邮箱 / workspace", "平台 / 状态", "分组 / 代理",
            "用量 / 重置", "创建 / 最近使用", "本地绑定",
        ])

    def check_headers(self, body_id, expected):
        body = self.ids[body_id]
        self.assertEqual(body.tag, "tbody")
        self.assertEqual(body.text, "")
        self.assertEqual(list(body.elements()), [], "Rows must be supplied by JS, not sample usage")
        table = body.ancestor(tag="table")
        headers = [n for n in table.elements() if n.tag == "th"]
        self.assertEqual([n.text for n in headers], expected)
        self.assertTrue(all(n.attrs.get("scope") == "col" for n in headers))
        self.assertIn("usage-heading", headers[4].classes)
        region = table.ancestor(css_class="account-table-scroll")
        self.assertIsNotNone(region)
        self.assertEqual(region.attrs.get("role"), "region")
        self.assertEqual(region.attrs.get("tabindex"), "0")
        self.assertTrue(region.attrs.get("aria-label"))

    def test_refresh_buttons_in_correct_toolbars(self):
        for button_id, body_id, toolbar in (
            ("refresh-usage", "account-rows", "local-toolbar"),
            ("cloud-refresh-usage", "cloud-list-rows", "cloud-toolbar"),
        ):
            with self.subTest(button=button_id):
                node = self.ids[button_id]
                self.assertEqual(node.tag, "button")
                self.assertEqual(node.attrs.get("type"), "button")
                self.assertEqual(node.attrs.get("aria-controls"), body_id)
                self.assertIn("刷新用量", node.text)
                self.assertIn("secondary", node.classes)
                self.assertIsNotNone(node.ancestor(css_class=toolbar))
                self.assertNotIn("hidden", node.attrs)

    def test_deploy_is_only_primary_action_in_local_page(self):
        buttons = [n for n in self.ids["page-pool"].elements()
                   if n.tag == "button" and "primary" in n.classes]
        self.assertEqual([n.attrs.get("id") for n in buttons], ["deploy-selected"])
        self.assertEqual(buttons[0].text, "导入服务器并上线")
        self.assertIn("disabled", buttons[0].attrs, "Start disabled until JS has a selection")

    def test_secondary_batch_actions_remain_in_native_disclosure(self):
        disclosure = self.ids["local-bulk-more"]
        self.assertEqual(disclosure.tag, "details")
        self.assertNotIn("open", disclosure.attrs)
        summary = next(n for n in disclosure.children if isinstance(n, Element))
        self.assertEqual(summary.tag, "summary")
        self.assertIn("更多批量操作", summary.text)
        for button_id in ("batch-bind-selected", "monitor-selected", "authorize-selected"):
            node = self.ids[button_id]
            self.assertIs(node.ancestor(tag="details"), disclosure)
            self.assertIn("secondary", node.classes)
            self.assertIn("disabled", node.attrs)
            while node is not self.ids["page-pool"]:
                self.assertNotIn("hidden", node.attrs)
                self.assertNotIn("inert", node.attrs)
                self.assertNotEqual(node.attrs.get("aria-hidden"), "true")
                node = node.parent

    def test_three_step_help_distinguishes_local_save_and_verified_online(self):
        workflow = self.ids["pool-workflow"]
        self.assertEqual(workflow.tag, "ol")
        steps = [n for n in workflow.children if isinstance(n, Element)]
        self.assertEqual([n.tag for n in steps], ["li", "li", "li"])
        self.assertIn("保存到本地", steps[0].text)
        self.assertIn("导入服务器并上线", steps[1].text)
        self.assertIn("最终云端读回", steps[2].text)
        self.assertIn("本地入库 ≠ 服务器上线", self.ids["page-pool"].text)
        self.assertIn("本地", self.ids["import-btn"].text)

    def test_accessible_references_and_compact_navigation_names(self):
        for node in self.nodes:
            for attr in ("aria-controls", "aria-labelledby", "aria-describedby", "for"):
                for ref in node.attrs.get(attr, "").split():
                    self.assertIn(ref, self.ids, f"{attr} on {node.tag}")
            if "data-page" in node.attrs:
                self.assertTrue(node.attrs.get("aria-label"))
                self.assertTrue(node.attrs.get("title"))

    def test_usage_css_contract_and_no_generated_metrics(self):
        css = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        for name in ("usage-cell", "usage-window", "usage-detail", "usage-stale",
                     "usage-unavailable", "usage-loading", "usage-heading"):
            self.assertRegex(css, rf"\.{name}(?![\w-])[^{{]*\{{")
        self.assertRegex(css, r"\.usage-cell[^{}]*\{[^}]*font-variant-numeric:\s*tabular-nums")
        self.assertNotRegex(css, r"\.usage-[^{}]*::(?:before|after)[^{}]*\{[^}]*content:")
        depth = 0
        # Strip quoted strings before checking nested media-query braces.
        plain = re.sub(r'''("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')''', "", css)
        for char in plain:
            depth += (char == "{") - (char == "}")
            self.assertGreaterEqual(depth, 0)
        self.assertEqual(depth, 0)

    def test_focus_disabled_responsive_and_reduced_motion_contracts(self):
        self.assertIn("summary:focus-visible", self.css)
        self.assertIn(".account-table-scroll:focus-visible", self.css)
        self.assertRegex(self.css, r"button:disabled,\s*\.button:disabled,\s*\.button:disabled:hover\s*\{[^}]*cursor:\s*not-allowed")
        self.assertGreater(self.css.rfind(".button:disabled,"), self.css.rfind(".button.primary{"))
        self.assertRegex(self.css, r"\.panel-heading \.table-controls\s*\{\s*display:\s*flex")
        self.assertIn("@media (max-width: 600px)", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertRegex(self.css, r"\.sidebar nav\s*\{[^}]*overflow-x:\s*auto")

    def test_usage_text_colors_meet_normal_text_contrast(self):
        def luminance(color):
            values = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4
                      for v in values]
            return sum(v * weight for v, weight in zip(linear, (.2126, .7152, .0722)))

        for token, background in (("--usage-muted", "#ffffff"),
                                  ("--usage-warning", "#fff5df"),
                                  ("--muted", "#f6f8f1")):
            foreground = re.findall(rf"{token}:\s*(#[0-9a-f]{{6}})", self.css)[-1]
            light, dark = sorted((luminance(foreground), luminance(background)), reverse=True)
            self.assertGreaterEqual((light + .05) / (dark + .05), 4.5, token)


if __name__ == "__main__":
    unittest.main()
