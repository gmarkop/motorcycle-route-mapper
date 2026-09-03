"""Guards against documentation drifting away from the code.

A setting documented under the wrong name is worse than an undocumented one:
it looks configurable, the reader sets it, and nothing happens. This caught a
`MOTO_TILE_URL` that never existed.
"""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Files that name settings and are meant to be accurate about them.
DOCUMENTS = [
    "README.md",
    "DEPLOY.md",
    "deploy/moto-route.env.example",
    "deploy/moto-route.service",
]

SETTING = re.compile(r"\bMOTO_[A-Z_]+\b")


def section_keys(unit_text: str) -> dict[str, set[str]]:
    """Map each systemd section to the directive names it actually contains.

    Parsed line by line rather than by splitting on "[Service]": that string
    also appears in the file's own comments, and splitting on it silently
    truncates the section before the directives being checked.
    """
    sections: dict[str, set[str]] = {}
    current = ""
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, set())
        elif "=" in line and current:
            sections[current].add(line.split("=", 1)[0].strip())
    return sections


def real_settings() -> set[str]:
    source = (REPO / "moto_route" / "config.py").read_text()
    return set(re.findall(r'_env_\w+\("(MOTO_[A-Z_]+)"', source))


@pytest.mark.parametrize("document", DOCUMENTS)
def test_documented_settings_all_exist(document):
    text = (REPO / document).read_text()
    mentioned = set(SETTING.findall(text))
    invented = mentioned - real_settings()

    assert not invented, (
        f"{document} names settings that config.py does not define: "
        f"{sorted(invented)}"
    )


def test_every_setting_is_documented_somewhere():
    mentioned: set[str] = set()
    for document in DOCUMENTS:
        mentioned |= set(SETTING.findall((REPO / document).read_text()))

    missing = real_settings() - mentioned
    assert not missing, f"settings with no mention in any document: {sorted(missing)}"


def test_deploy_files_are_present_and_consistent():
    unit = (REPO / "deploy" / "moto-route.service").read_text()
    install = (REPO / "deploy" / "install.sh").read_text()

    # The security posture the README and DEPLOY.md both promise.
    assert "--host 127.0.0.1" in unit, "the service must bind to loopback only"
    assert "NoNewPrivileges=true" in unit
    assert "StateDirectory=moto-route" in unit

    # systemd silently ignores these under [Service]; they only work in [Unit].
    unit_keys = section_keys(unit)
    assert "StartLimitIntervalSec" in unit_keys["Unit"], \
        "StartLimitIntervalSec belongs in [Unit] or systemd ignores it"
    assert "StartLimitBurst" in unit_keys["Unit"]
    assert "ExecStart" in unit_keys["Service"]

    assert install.startswith("#!"), "install.sh needs a shebang"
    assert "set -euo pipefail" in install


def test_install_script_is_executable():
    assert (REPO / "deploy" / "install.sh").stat().st_mode & 0o111, \
        "install.sh must be executable"


# ------------------------------------------------------------------- licensing

def test_license_file_is_present_and_complete():
    licence = (REPO / "LICENSE").read_text()

    assert "MIT License" in licence
    assert "Copyright (c)" in licence
    # The clauses that make it MIT rather than a copyright notice with good manners.
    assert "Permission is hereby granted, free of charge" in licence
    assert "WITHOUT WARRANTY OF ANY KIND" in licence


def test_license_has_a_real_copyright_holder():
    """Guards against committing the template with its placeholder intact."""
    licence = (REPO / "LICENSE").read_text()

    assert "__COPYRIGHT_HOLDER__" not in licence, \
        "substitute the real copyright holder before committing"

    holder_line = next(line for line in licence.splitlines()
                       if line.startswith("Copyright (c)"))
    # "Copyright (c) <year> <name>" — four fields at minimum.
    assert len(holder_line.split()) >= 4, f"no name on the copyright line: {holder_line!r}"


def test_readme_agrees_with_the_license_file():
    readme = (REPO / "README.md").read_text()

    assert "MIT licensed" in readme, "the README must state the licence it ships under"
    assert "[LICENSE](LICENSE)" in readme, "and link to the file"
    # An MIT licence on the code grants nothing over the map data it fetches.
    assert "ODbL" in readme and "not mine to give" in readme


def test_vendored_license_is_still_shipped():
    """Leaflet is BSD-2-Clause; redistributing it means keeping its licence."""
    vendored = REPO / "moto_route" / "static" / "vendor" / "leaflet" / "LICENSE"

    assert vendored.exists(), "Leaflet's licence file must ship with the vendored copy"
    assert "Copyright" in vendored.read_text()
