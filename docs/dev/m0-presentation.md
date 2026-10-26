# M0.1 — Nautilus presentation result

Date: 2026-10-19. Result: **presentation spike passed in an isolated desktop**.
The daily Files process was not restarted and no extension was installed in it.

## What was actually run

Real Nautilus 50.2.2, nautilus-python 4.1.0, Python 3.14.4, GTK4, and the
installed Yaru icon theme, under Xvfb with software rendering. Ubuntu packages
were downloaded and extracted locally; no administrator access or system package
installation was needed. Bubblewrap hid the home and runtime sockets, made the
host read-only, and gave the experiment no network. The new DBus session had no
desktop services; expected missing Tracker/volume-monitor warnings do not
represent a production integration test.

The disposable extension assigns hard-coded fixture states only. It returns
`IN_PROGRESS`, completes each callback after a 40 ms GLib timer, and calls the
installed GI signature `info_provider_update_complete_invoke(closure, provider,
handle, result)`. That signature's argument order should be checked against the
installed typelib; the reference manual's prose and example differ.

## Evidence

| View | Small/light | Small/dark | Large/light | Large/dark |
| --- | --- | --- | --- | --- |
| Icons | [PNG](../evidence/m0/badges/icon-small-light.png) | [PNG](../evidence/m0/badges/icon-small-dark.png) | [PNG](../evidence/m0/badges/icon-large-light.png) | [PNG](../evidence/m0/badges/icon-large-dark.png) |
| List + status column | [PNG](../evidence/m0/badges/list-small-light.png) | [PNG](../evidence/m0/badges/list-small-dark.png) | [PNG](../evidence/m0/badges/list-large-light.png) | [PNG](../evidence/m0/badges/list-large-dark.png) |

Captures contain the fixture content pane; host sidebar volume labels are
excluded. [Callbacks](../evidence/m0/badges/callbacks.jsonl) record 56 completions
across eight launches: six initial states plus one refreshed state per launch.
The refreshed item goes from the download emblem to a green check after
`invalidate_extension_info()`, without directory navigation. The unmanaged
seventh item never gets a badge or extension attribute.

After the extension file was removed from the sandbox's user extension
directory, a fresh Nautilus launch produced no more extension callbacks and
[no fixture badges](../evidence/m0/badges/uninstalled.png).
[Machine-readable results](../evidence/m0/badges/results.json) and
[icon-name availability](../evidence/m0/badges/icon-availability.json) are included.

## Presentation decision

Use emblems in both icon and list views, with an optional text column in list
view. Dark-style emblems have modest contrast at small sizes; M2 needs clearer
final icons and user readability review. The status text makes the small list emblems easier to interpret and
avoids relying on color alone. Register visible columns by the column's **name**
(`CloudMarksSpike::status` here), rather than its string-attribute name.

Yaru supplies `emblem-downloads`, `emblem-default`, `emblem-synchronizing`,
`emblem-important`, and `emblem-dialog-question`. The isolated session initially
fell back to Adwaita and lacked several of these names; explicitly selecting
Yaru in the sandbox corrected that. M2 must detect theme availability and use a
documented fallback instead of silently losing status marks. A download arrow
is a presentation stand-in for cloud-only state; final cloud/pin icons and
accessible descriptions remain M2 work.

These screenshots establish rendering at two zoom levels and two appearances.
They do not establish large-folder performance, a production helper connection,
cloud state accuracy, or user readability approval. The user can review the
images tomorrow; no desktop restart is needed to inspect them.

## Reproduction

See [the isolated runner](../../spikes/nautilus-badges/README.md). The code is
deliberately disposable and must not be reused as M2's extension.

API references: [InfoProvider](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-info-provider.html),
[ColumnProvider](https://gnome.pages.gitlab.gnome.org/nautilus-python/class-nautilus-python-column-provider.html),
[extension search paths](https://gnome.pages.gitlab.gnome.org/nautilus-python/nautilus-python-overview.html).
