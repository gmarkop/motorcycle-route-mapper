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


def test_print_keeps_the_flex_order_that_puts_the_map_on_top():
    """`display: block` in the print block was the obvious move and was wrong.

    The sidebar comes first in the document — only `order` puts the map above
    it — so block layout printed the panels first, and threw away the flex
    basis that gives the map its height along with them. Rendered, that was a
    sheet with the lists at the top and no map at all.
    """
    css = (REPO / "moto_route" / "static" / "style.css").read_text()
    printed = css[css.index("@media print"):]

    assert "#layout { display: flex;" in printed, \
        "print must keep flex, or the sidebar leads and the map collapses"
    assert "#map { height:" in printed, \
        "the map needs an explicit print height; flex-basis will not apply"


def test_the_profile_has_ink_for_paper():
    """Its grid and labels are SVG attributes, which a stylesheet cannot reach.

    Picked against a dark panel, they are invisible on white, so the profile is
    redrawn for print rather than restyled.
    """
    source = (REPO / "moto_route" / "static" / "js" / "app.js").read_text()

    assert "PROFILE_INK" in source
    assert "drawProfile(state.profile, true)" in source, \
        "the print path must redraw the profile for paper"


def test_printing_without_the_dialog_prints_the_page():
    """The blank page on an iPad, and on Cmd-P everywhere else.

    The section rules read as "hide unless data-print names this one", and an
    absent attribute names nothing — so the browser's own print command, which
    never sets it, hid every section. `body[data-print]` is what makes the
    rules apply only when someone actually chose.
    """
    css = (REPO / "moto_route" / "static" / "style.css").read_text()
    printed = css[css.index("@media print"):]

    hides = [line for line in printed.splitlines()
             if "data-print~=" in line]
    assert hides, "no section rules found"
    for line in hides:
        assert "body[data-print]" in line, \
            f"unguarded rule hides a section when nobody chose: {line.strip()}"


def test_paper_preparation_is_not_hung_off_the_dialog():
    """Print can start from the browser, not just from our button.

    Filling the header and redrawing the profile in paper ink belong on
    beforeprint, or Share > Print produces a page with an empty header and a
    profile drawn for a dark screen.
    """
    source = (REPO / "moto_route" / "static" / "js" / "app.js").read_text()
    listener = source.index("addEventListener('beforeprint'")
    guard = source.index("typeof dialog.showModal !== 'function'")

    assert listener < guard, \
        "beforeprint must be wired before the dialog-capability return"
