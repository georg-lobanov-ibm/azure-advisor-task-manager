"""Checks on the files the browser gets: nothing that could turn text into markup or code."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from advisor_agent.web import app as web_app

STATIC = Path(web_app.__file__).parent / "static"
SOURCE = Path(web_app.__file__).parent
JS = (STATIC / "app.js").read_text(encoding="utf-8")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "app.css").read_text(encoding="utf-8")


def strip_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)(^|[^:'\"\\])//.*$", r"\1", js)


@pytest.mark.parametrize(
    "pattern",
    [
        r"\binnerHTML\b",
        r"\bouterHTML\b",
        r"\binsertAdjacentHTML\b",
        r"\bdocument\s*\.\s*write(ln)?\b",
        r"\beval\s*\(",
        r"\bnew\s+Function\b",
        r"\bFunction\s*\(",
        r"\bsetTimeout\s*\(\s*['\"`]",
        r"\bsetInterval\s*\(\s*['\"`]",
        r"\bsrcdoc\b",
        r"\bcreateContextualFragment\b",
        r"\bDOMParser\b",
        r"\bimportScripts\b",
        r"setAttribute\s*\(\s*['\"]on",
        r"javascript:",
    ],
)
def test_the_script_has_no_way_to_turn_text_into_markup_or_code(pattern):
    assert not re.search(pattern, strip_comments(JS)), pattern


def test_the_only_place_that_sets_a_link_address_is_the_checked_helper():
    code = strip_comments(JS)
    assignments = list(re.finditer(r"\.\s*(href|src|action|formAction)\s*=(?!=)", code))
    via_attribute = re.findall(r"""setAttribute\(\s*['"](href|src|action|formaction|srcset|data)['"]""", code, flags=re.I)

    assert len(assignments) == 1 and not via_attribute
    # ... and it sits in the helper that builds a Jira link, right after the address was checked.
    helper = re.search(r"function jiraKeyNode\(key\) \{(.*?)\n  \}", code, flags=re.S)
    assert helper and "const url = jiraUrl(key);" in helper.group(1)
    assert assignments[0].start() > helper.start() and assignments[0].end() < helper.end()
    assert "if (!url) return" in helper.group(1)
    assert "'noopener noreferrer'" in helper.group(1)


def test_the_jira_address_helper_allows_only_http_and_https():
    code = strip_comments(JS)
    helper = re.search(r"function jiraUrl\(key\) \{(.*?)\n  \}", code, flags=re.S)

    assert helper
    assert "JIRA_KEY.test(key)" in helper.group(1)
    assert "'http:'" in helper.group(1) and "'https:'" in helper.group(1)


def test_the_page_has_no_inline_script_style_or_handler():
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", HTML, flags=re.S | re.I)

    assert scripts, "no script tag at all"
    for attributes, body in scripts:
        assert "src=" in attributes and body.strip() == "", "inline script"
    assert not re.search(r"<style\b", HTML, flags=re.I)
    assert not re.search(r"\sstyle\s*=", HTML, flags=re.I)
    assert not re.search(r"\son[a-z]+\s*=", HTML, flags=re.I)
    assert "javascript:" not in HTML.lower()


def test_the_page_loads_only_files_the_server_serves():
    referenced = set(re.findall(r"""(?:src|href)=["']([^"']+)["']""", HTML))

    assert referenced, "the page references nothing"
    for reference in referenced:
        assert reference.startswith("/static/"), reference
        assert reference.removeprefix("/static/") in web_app.STATIC_FILES, reference


def test_nothing_is_loaded_from_another_site():
    for name, text in (("index.html", HTML), ("app.js", JS), ("app.css", CSS)):
        assert not re.search(r"https?://", strip_comments(text) if name == "app.js" else text), name
    assert "@import" not in CSS


def test_every_file_in_the_folder_is_one_the_server_serves():
    on_disk = {path.name for path in STATIC.iterdir() if path.is_file()}

    assert on_disk == set(web_app.STATIC_FILES) | {"index.html"}


def test_the_page_has_a_handler_for_each_event_the_server_sends():
    sent = set()
    for path in SOURCE.glob("*.py"):
        sent |= set(re.findall(r"""publish\(\s*["']([a-z_]+)["']""", path.read_text(encoding="utf-8")))
    sent |= {"snapshot", "shutdown"}  # written by the hub itself

    block = re.search(r"const handlers = \{(.*?)\};", JS, flags=re.S)
    assert block, "no handlers table in app.js"
    handled = set(re.findall(r"^\s*([a-z_]+)\s*:", block.group(1), flags=re.M))

    assert sent == handled


def test_the_script_calls_only_routes_that_exist():
    source = (SOURCE / "app.py").read_text(encoding="utf-8")
    served = set(re.findall(r'Route\(\s*"(/api/[^"{]*)', source))
    used = set(re.findall(r"""['"`](/api/[a-z/]+)""", JS))

    assert used, "no API calls found"
    assert used <= served, used - served


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_script_parses():
    result = subprocess.run(["node", "--check", str(STATIC / "app.js")], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
