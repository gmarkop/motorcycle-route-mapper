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


def test_the_paper_map_box_is_sized_by_flex_basis():
    """`height` alone is ignored on a flex item, and the map measured 703x0.

    #map is `flex: 1` inside a column, where the basis decides the main size.
    Setting only a height produced a zero-height box, and `fitBounds` into a
    zero-height box frames nothing at all.
    """
    css = (REPO / "moto_route" / "static" / "style.css").read_text()
    rule = css[css.index("body.print-map #map {"):]
    rule = rule[:rule.index("}")]

    assert "flex:" in rule, "the paper map box needs a flex basis, not just a height"
    assert "359px" in rule and "703px" in rule


def test_the_paper_box_matches_the_printed_box():
    """fitBounds solves for the box it is given, so the two must agree.

    186mm x 95mm of A4 at 96dpi is 703 x 359 CSS pixels. Fitting the route into
    one shape and printing it into another is what showed a fraction of it.
    """
    css = (REPO / "moto_route" / "static" / "style.css").read_text()

    assert "703px" in css and "359px" in css, "screen-side paper box"
    assert "#map { height: 95mm" in css, "print-side box"
    assert round(186 / 25.4 * 96) == 703 and round(95 / 25.4 * 96) == 359


def test_showing_the_profile_tells_leaflet_the_map_shrank():
    """The profile takes 156px off the map, and Leaflet caches its size.

    Found by measuring what printing restored: Leaflet had been carrying a
    container 156px taller than the real one for the whole session, so
    getBounds was wrong and clicks landed slightly off. Printing had been
    repairing it by accident.
    """
    source = (REPO / "moto_route" / "static" / "js" / "app.js").read_text()
    body = source[source.index("function showProfile("):]
    body = body[:body.index("\n}\n")]

    assert "mapview.resized()" in body, \
        "unhiding the profile must tell Leaflet the map got shorter"


def test_a_one_pixel_tile_does_not_count_as_a_loaded_tile():
    """The service worker's placeholder is `complete` with a non-zero width.

    When it has nothing cached and cannot reach the tile server it hands back a
    1x1 transparent PNG, so `naturalWidth > 0` is true for every tile and the
    wait was satisfied by eighteen invisible pixels — a printed sheet with the
    route drawn over nothing. Inspecting a real PDF found exactly that: eight
    images, all marker pins and shadows, not one 256-pixel tile.
    """
    source = (REPO / "moto_route" / "static" / "js" / "mapview.js").read_text()
    body = source[source.index("export function tilesSettled"):]
    body = body[:body.index("\n}\n")]

    assert "naturalWidth === 1" in body, \
        "the 1x1 placeholder must be told apart from a real tile"


def test_the_print_flow_says_when_the_map_will_be_empty():
    """Printing anyway is right; printing silently is not.

    The lists and the profile are most of the sheet's value, so a map that
    cannot be drawn should not cancel the print — but it has to be said, or the
    rider is left working out why the map is blank.
    """
    source = (REPO / "moto_route" / "static" / "js" / "app.js").read_text()

    assert "tiles.blank" in source, "the blank-tile case must be handled"
    assert "showPrintNote" in source, "and surfaced in the page, not the console"


def test_the_ascent_stat_is_always_answered_even_when_the_profile_fails():
    """Left pending, "…" is a promise the failure path has to keep.

    The summary shows "..." for ascent when the file carries no heights, on the
    understanding that the elevation profile will fill it in from the terrain
    model. If `showProfile` only told the stat on its success path, a failed
    or offline lookup left the summary waiting for ever -- an ellipsis that
    never resolves, which reads as a hung app rather than an absent answer.

    So the call has to sit ahead of the early return, not after it.
    """
    source = (JS / "app.js").read_text()
    body = source[source.index("function showProfile(payload)"):]
    body = body[:body.index("\n}\n")]

    told = body.index("panels.setAscentStat(")
    early_return = body.index("wrap.hidden = true;")

    assert told < early_return, (
        "showProfile must answer the ascent stat before it can bail out"
    )


def test_the_summary_does_not_test_ascent_for_truthiness():
    """`stats.ascent_m ? ... : '–'` was wrong for a flat route.

    0 is falsy, so a route that genuinely climbs nothing rendered as "no
    data". The check must be about whether heights exist, not whether the
    total happens to be non-zero.
    """
    source = (JS / "panels.js").read_text()
    summary = source[source.index("export function showSummary"):]
    summary = summary[:summary.index("\n}\n")]
    # Comments quote the old expression to explain why it went; strip them or
    # the prose fails the test that the code passes.
    code = "\n".join(line for line in summary.splitlines()
                     if not line.lstrip().startswith("//"))

    assert "stats.ascent_m ?" not in code, "0 m of climbing is an answer"
    assert "has_elevation" in summary, "the stat must key off the data, not the total"


def test_the_file_s_own_ascent_is_not_overwritten_by_the_terrain_model():
    """The two totals disagree, so whichever lands second would win.

    `models.py` ignores height changes under 3 m to suppress barometer noise;
    the elevation profile sums its samples whole. Both are defensible, but the
    profile arrives a moment after the summary, so without a guard the stat
    visibly changes to a different number just after the rider has read it --
    and the number it changes to is the worse of the two for a file that
    recorded real heights.

    The guard has to release for the next route, or a second file with no
    heights of its own inherits the first one's answer. `showSummary` calls
    the setter on every route, which is what resets it.
    """
    source = (JS / "panels.js").read_text()

    setter = source[source.index("export function setAscentStat"):]
    setter = setter[:setter.index("\n}\n")]
    guard = [line for line in setter.splitlines() if "ascentFromFile &&" in line]
    assert guard, "nothing stops the profile overwriting the file"
    assert "return" in guard[0], (
        "the guard must bail out, not merely record where the number came from"
    )

    summary = source[source.index("export function showSummary"):]
    summary = summary[:summary.index("\n}\n")]
    assert "setAscentStat(" in summary, (
        "showSummary must call the setter on every route, or the guard sticks"
    )
