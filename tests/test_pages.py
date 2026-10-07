"""The static preview (scripts/build_pages.py) and its install prompt.

The prompt a visitor gives their own agent to install noodle lives in ONE file;
the build puts it on the landing page and in the preview's "send" message, and
the README quotes it. A README that drifts from the file tells people one thing
on GitHub and another on the site — pinned here.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT = (ROOT / "scripts" / "pages" / "install-prompt.txt").read_text().strip()
BUILD = (ROOT / "scripts" / "build_pages.py").read_text()
LANDING = (ROOT / "scripts" / "pages" / "index.html").read_text()


def test_the_readme_quotes_the_install_prompt_verbatim():
    assert PROMPT in (ROOT / "README.md").read_text()


def test_the_prompt_makes_the_agent_check_before_it_installs():
    for must in ("README.md", "AGENTS.md", "security", "docker info", "stop", "cad_help", "cad_notes"):
        assert must in PROMPT, must


def test_landing_and_preview_get_the_prompt_from_the_file():
    assert "{{INSTALL_PROMPT}}" in LANDING and 'id="install-agent"' in LANDING
    assert "INSTALL_PROMPT = (ROOT" in BUILD and '"{{INSTALL_PROMPT}}"' in BUILD
    assert "__copyInstall" in BUILD and "STATIC_PREVIEW" in BUILD
