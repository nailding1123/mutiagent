"""Compatibility installer for Python environments with pip < 21.3.

Modern installers read project metadata from ``pyproject.toml``. Apple's
system Python still ships an older pip that needs a setup.py entry point for
editable installs and console-script generation.
"""

from pathlib import Path

from setuptools import find_packages, setup


ROOT = Path(__file__).parent


setup(
    name="multiagent",
    version="2.5.0",
    description="A local group-chat bridge for Claude Code and Codex CLI.",
    long_description=(ROOT / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    python_requires=">=3.9",
    packages=find_packages(include=("multiagent_cli", "multiagent_cli.*")),
    package_data={"multiagent_cli": ["web/*"]},
    include_package_data=True,
    entry_points={
        "console_scripts": ["multiagent=multiagent_cli.web_launcher:main"],
    },
)
