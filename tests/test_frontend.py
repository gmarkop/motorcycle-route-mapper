"""Two ordering invariants in the frontend, with no JS test harness to hold them.

Both were real failures, and both are invisible to every other check here: the
Python suite never loads the page, and a broken one still renders enough to
look alive.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
JS = REPO / "moto_route" / "static" / "js"


def test_the_profile_binds_its_handlers_before_drawing_chrome():
    """The crosshair is the feature; the legend is decoration.

    Written the other way round, a page whose HTML is a version behind its
    JavaScript -- which a service worker produces easily -- drew the coloured
    line, failed on the missing legend element, and left the profile inert. The
    two symptoms looked unrelated, and neither named the cause.
    """
    source = (JS / "app.js").read_text()

    binds = source.index("svg.onpointermove = track;")
    legend = source.index("const legend = $('profile-legend');")

    assert binds < legend, "the legend must not be able to cost us the crosshair"


def test_the_demanding_panel_does_not_depend_on_curviness_succeeding():
    """It used to sit behind an early return, and vanished without a word.

    `showDemanding` knows how to say why it has nothing to show. Skipping the
    call entirely leaves no panel and no reason, which reads as a feature that
    was removed rather than one that could not run.
    """
    source = (JS / "panels.js").read_text()
    body = source[source.index("export function showCurviness"):]
    body = body[:body.index("\n}\n")]

    guarded = body[:body.index("showDemanding(payload);")]
    assert "return;" not in guarded, \
        "showDemanding is behind an early return again"
