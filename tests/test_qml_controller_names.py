"""Every name the QML reads from a Python object exists on that object.

QML reads a member a QObject does not have as `undefined` and says nothing: a
renamed property leaves a blank label or a wrong condition, a renamed slot
throws only when its button is pressed, and a `Connections` handler whose
signal is gone never runs. The meta-objects read here are exactly what QML can
see, so these tests need no window and no application data.

The Python objects reach the QML as the window's initial properties
(`appController` as `app`, `galleryModel`, `wheelInputFilter`), as the eight
controllers `app` hands out and the two list models they hand out, as
component properties and JS locals bound to any of these (`property var
records`, `const library = dialog.app ? dialog.app.library : null`), and as a
view's `model:` (`Connections { target: grid.model }`). Reads through all of
these are checked. A form the scan cannot follow (`app?.x`, `app["x"]`, a chain
continued on the next line) fails the test instead of passing unseen.
"""

from pathlib import Path
import re
import unittest

try:
    from tests import app_data_isolation  # noqa: F401  (never this PC's real app data)
except ModuleNotFoundError:      # run as a script: the tests folder is the path
    import app_data_isolation  # noqa: F401

from luvbox import qt_app, qt_gallery, qt_library, qt_local_folder, qt_records, qt_wheel


ROOT = Path(__file__).resolve().parents[1]
QML = ROOT / "assets" / "qt"
# What `app.<name>` hands out, by the property's name.
CHILDREN = {
    "accounts": qt_app.AccountController, "browserSetup": qt_app.BrowserSetupController,
    "download": qt_app.DownloadController, "library": qt_app.LibraryController,
    "management": qt_app.ManagementController, "records": qt_app.RecordsController,
    "schedule": qt_app.ScheduleController, "updates": qt_app.UpdateController,
    "localFolder": qt_local_folder.LocalFolderController,
    "localManagement": qt_local_folder.LocalManagementController,
}
# List models a controller hands out, by the property's name, and who has it.
MODELS = {"unsavedModel": (qt_records.UnsavedModel, "records"),
          "duplicateGroupModel": (qt_library.DuplicateGroupModel, "library"),
          "folderModel": (qt_gallery.GalleryModel, "localFolder")}
CLASSES = dict(CHILDREN, app=qt_app.ApplicationController, galleryModel=qt_gallery.GalleryModel,
               wheelInputFilter=qt_wheel.WheelInputFilter,
               **{name: cls for name, (cls, _owner) in MODELS.items()})
# Names that mean the same object in every file.
GLOBAL = {"app": "app", "appController": "app", "galleryModel": "galleryModel",
          "wheelInputFilter": "wheelInputFilter"}
# `property var <name>` given from outside, and what it has to be bound to there.
DECLARED = {**{name: (name, rf"\bapp\.{name}\b") for name in CHILDREN},
            "galleryModel": ("galleryModel", r"\bgalleryModel\b"),
            "inputFilter": ("wheelInputFilter", r"\bwheelInputFilter\b")}
# Every QObject wrapper in QML also answers to these.
GENERIC = {"objectName", "toString", "destroy", "hasOwnProperty", "deleteLater"}


def members(cls):
    """(property names; every property, method and signal name; signal names) QML sees on `cls`."""
    meta = cls.staticMetaObject
    properties = {meta.property(index).name() for index in range(meta.propertyCount())}
    names, signals = set(properties) | GENERIC, set()
    for index in range(meta.methodCount()):
        method = meta.method(index)
        name = bytes(method.name()).decode()
        names.add(name)
        if method.methodType() == method.MethodType.Signal:
            signals.add(name)
    return properties, names, signals


KNOWN = {kind: members(cls) for kind, cls in CLASSES.items()}


_MASKED = {}


def mask(text):
    """(code, masked): comments blanked; in `masked` the inside of strings too.

    Both keep every offset and line break, so positions and line numbers
    found in one hold in the other and in the file.
    """
    if text not in _MASKED:
        _MASKED[text] = _mask(text)
    return _MASKED[text]


def _mask(text):
    code, masked = list(text), list(text)
    index, quote, length = 0, None, len(text)
    while index < length:
        char = text[index]
        if quote:
            if char == "\\" and index + 1 < length:
                for position in (index, index + 1):
                    if text[position] != "\n":
                        masked[position] = " "
                index += 2
                continue
            if char == quote:
                quote = None
            elif char != "\n":
                masked[index] = " "
            index += 1
        elif text.startswith("//", index) or text.startswith("/*", index):
            end = text.find("\n", index) if text[index + 1] == "/" else text.find("*/", index + 2) + 2
            end = length if end < (1 if text[index + 1] == "/" else 2) else end
            for position in range(index, end):
                if text[position] != "\n":
                    code[position] = masked[position] = " "
            index = end
        else:
            if char in "\"'`":
                quote = char
            index += 1
    return "".join(code), "".join(masked)


_CLOSING = {}


def closing(text):
    """{offset of each `{`: offset of its `}`} for a masked text, worked out once."""
    if text not in _CLOSING:
        pairs, opened = {}, []
        for index, char in enumerate(text):
            if char == "{":
                opened.append(index)
            elif char == "}" and opened:
                pairs[opened.pop()] = index
        _CLOSING.clear()
        _CLOSING[text] = pairs
    return _CLOSING[text]


def blocks(text, word):
    """(start, end) of the body of each `word { ... }`, braces matched."""
    pairs = closing(text)
    for match in re.finditer(rf"(?<![\w.]){word}\s*\{{", text):
        yield match.end(), pairs.get(match.end() - 1, len(text))


def top_level(body):
    """A block's own text, without what its nested braces hold."""
    kept, depth = [], 0
    for char in body:
        if char == "}":
            depth -= 1
        if depth == 0:
            kept.append(char)
        if char == "{":
            depth += 1
    return "".join(kept)


def kind_of(expression, names, views=None):
    """The kind of Python object an expression ends on, or None for anything else.

    `a ? b : null` is read as b: the guard only keeps the binding quiet
    while the object is missing.
    """
    if "?" in expression:
        branches = [branch.strip() for branch in expression.split("?", 1)[1].split(":")]
        kinds = {kind_of(branch, names, views) for branch in branches if branch not in ("null", "undefined")}
        return kinds.pop() if len(kinds) == 1 else None
    chain = re.fullmatch(r"\s*((?:\w+\.)*\w+)\s*;?\s*", expression)
    if not chain:
        return None
    parts = chain.group(1).split(".")
    if views and len(parts) == 2 and parts[1] == "model" and parts[0] in views:
        return views[parts[0]]
    if parts[-1] in MODELS and len(parts) > 1:
        return parts[-1]
    if "app" in parts:
        rest = parts[parts.index("app") + 1:]
        if not rest:
            return "app"
        return rest[0] if len(rest) == 1 and rest[0] in CHILDREN else None
    return names.get(parts[-1]) if len(parts) <= 2 else None


def names_in(masked):
    """{name: kind} for every name that holds a Python object in this file."""
    names = dict(GLOBAL)
    for name in re.findall(r"property var (\w+)\b", masked):
        if name in DECLARED:
            names[name] = DECLARED[name][0]
    bound = re.findall(r"property (?:var|QtObject) (\w+):[ \t]*([^\n;]+)", masked)
    bound += re.findall(r"(?:const|let|var)\s+(\w+)\s*=[ \t]*([^\n;]+)", masked)
    for name, expression in bound:
        kind = kind_of(expression, names)
        if kind:
            names[name] = kind
    return names


def views_in(masked, names):
    """{id: kind} for each view whose `model:` is a Python model, for `<id>.model`."""
    views, pairs = {}, closing(masked)
    for match in re.finditer(r"(?<![\w.])model:[ \t]*([^\n;]+)", masked):
        kind = kind_of(match.group(1), names)
        enclosing = [opening for opening, end in pairs.items() if opening < match.start() < end]
        if not kind or not enclosing:
            continue
        opening = max(enclosing)
        own = top_level(masked[opening + 1:pairs[opening]])
        view = re.search(r"(?<![\w.])id:\s*(\w+)", own)
        if view and match.group(0) in own:
            views[view.group(1)] = kind
    return views


def scan(sources):
    """(problems, member reads, Connections handlers) over {file name: QML text}."""
    problems, reads, handlers = [], 0, 0
    for name, text in sources.items():
        code, masked = mask(text)
        held = names_in(masked)
        views = views_in(masked, held)
        alternatives = "|".join(map(re.escape, sorted(held, key=len, reverse=True)))
        # The first name in a chain that holds an object decides: window.app.library.X
        # is app -> library -> X even where `library` is also a local.
        reading = re.compile(rf"(?<![\w.])(?:\w+\.)*?({alternatives})\.(\w+)(?:\.(\w+))?")
        unreadable = re.compile(rf"(?<![\w.])(?:\w+\.)*?({alternatives})(?:\.(\w+))?\s*(\?\.|\[)")
        for number, line in enumerate(masked.splitlines(), 1):
            if re.match(r"\s*\.\w", line):
                problems.append(f"{name}:{number}: a chain continued on this line; keep it on one line")
            for match in unreadable.finditer(line):
                holder, first, how = match.groups()
                if first is None or (held[holder] == "app" and first in CHILDREN):
                    problems.append(f"{name}:{number}: {match.group(0).strip()} cannot be checked; write {holder}.name")
            for match in reading.finditer(line):
                holder, first, second = match.groups()
                kind = held[holder]
                reads += 1
                if first not in KNOWN[kind][1]:
                    problems.append(f"{name}:{number}: {holder}.{first}")
                elif second and kind == "app" and first in CHILDREN and second not in KNOWN[first][1]:
                    problems.append(f"{name}:{number}: {holder}.{first}.{second}")
                elif second and first in MODELS and MODELS[first][1] == kind and second not in KNOWN[first][1]:
                    problems.append(f"{name}:{number}: {holder}.{first}.{second}")
        for start, end in blocks(masked, "Connections"):
            line = masked[:start].count("\n") + 1
            own = top_level(masked[start:end])
            target = re.search(r"(?<![\w.])target:[ \t]*([^\n;]+)", own)
            if not target:
                continue  # listens to its parent, a QML item
            kind = kind_of(target.group(1), held, views)
            if kind is None:
                continue  # a QML item or a model made in QML
            found = re.findall(r"function on([A-Z]\w*)\s*\(", own) + re.findall(r"(?<![\w.])on([A-Z]\w*)\s*:", own)
            for handler in found:
                handlers += 1
                signal = handler[0].lower() + handler[1:]
                if signal not in KNOWN[kind][2]:
                    problems.append(f"{name}:{line}: Connections on {kind}: on{handler}, no signal {signal}")
        for start, end in blocks(masked, "Binding"):
            target = re.search(r"(?<![\w.])target:[ \t]*([^\n;]+)", top_level(masked[start:end]))
            bound = re.search(r"(?<![\w.])property:\s*\"(\w+)\"", top_level(code[start:end]))
            kind = kind_of(target.group(1), held, views) if target else None
            if kind and bound:
                reads += 1
                if bound.group(1) not in KNOWN[kind][0]:
                    problems.append(f"{name}:{masked[:start].count(chr(10)) + 1}: Binding on {kind}: no property "
                                    f"{bound.group(1)}")
    return problems, reads, handlers


def sources():
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(QML.glob("*.qml"))}


class ControllerNameTests(unittest.TestCase):
    def test_the_lists_match_what_the_application_hands_out(self):
        meta = qt_app.ApplicationController.staticMetaObject
        handed = {meta.property(index).name() for index in range(meta.propertyOffset(), meta.propertyCount())
                  if meta.property(index).typeName() == "QObject*"}
        self.assertEqual(handed, set(CHILDREN))
        for model, (_cls, owner) in MODELS.items():
            self.assertIn(model, KNOWN[owner][0], f"{owner}.{model}")
        # The window's initial properties: a new object handed in has to be added here.
        launch = (ROOT / "luvbox" / "qt_app.py").read_text(encoding="utf-8")
        initial = re.search(r"setInitialProperties\(\{(.*?)\}\)", launch, re.S).group(1)
        self.assertEqual(set(re.findall(r'"(\w+)":', initial)),
                         {"appController", "galleryModel", "wheelInputFilter", "uiFontFamily", "visible"})
        self.assertIn('"galleryModel": controller._library.model', initial)
        self.assertIn("self.model = GalleryModel(", (ROOT / "luvbox" / "qt_library.py").read_text(encoding="utf-8"))
        self.assertIn("readonly property var app: appController", sources()["MainWindow.qml"])

    def test_every_object_a_component_is_given_from_outside_is_that_object(self):
        # `property var records` is read as the records controller only when
        # it is bound to app.records, here or where the component is used.
        everything = sources()
        joined = "\n".join(mask(text)[1] for text in everything.values())
        for name, text in everything.items():
            masked = mask(text)[1]
            for declared in re.findall(r"property var (\w+)\b", masked):
                if declared not in DECLARED:
                    continue
                source = DECLARED[declared][1]
                own = re.search(rf"property var {declared}:[^\n]*{source}", masked)
                given = re.search(rf"(?<![\w.]){declared}:\s*[^\n]*{source}", joined)
                self.assertTrue(own or given, f"{name}: property var {declared}")

    def test_every_member_the_qml_reads_from_a_python_object_exists(self):
        problems, reads, handlers = scan(sources())
        self.assertEqual(problems, [])
        # The scan still finds what it looks for (about 730 reads and 20 handlers in 1.10.3).
        self.assertGreater(reads, 600)
        self.assertGreater(handlers, 15)

    def test_the_scan_notices_a_misspelled_name_in_each_form(self):
        everything = sources()
        planted = [
            ("MainWindow.qml", "app.version", "app.versio", "app.versio"),
            ("MainWindow.qml", "window.app.library.aiCategoryCounts", "window.app.library.aiCategoryCount",
             "app.library.aiCategoryCount"),
            ("DuplicateReview.qml", "library.duplicatesSettling", "library.duplicatesSetling",
             "library.duplicatesSetling"),
            ("ConnectionDialog.qml", "accounts.browserConnecting", "accounts.browserConecting",
             "accounts.browserConecting"),
            ("MainWindow.qml", "library.setUserTag(changes.userTag)", "library.setUserTags(changes.userTag)",
             "library.setUserTags"),
            ("SaveReviewDialog.qml", "targetApp.respondReview(", "targetApp.respondReviw(",
             "targetApp.respondReviw"),
            ("MainWindow.qml", "window.galleryModel.search(displayText)", "window.galleryModel.serch(displayText)",
             "galleryModel.serch"),
            ("DuplicateReview.qml", "galleryModel.loadMore()", "galleryModel.loadMor()", "galleryModel.loadMor"),
            ("MainWindow.qml", "function onReviewCleared", "function onReviewClear", "no signal reviewClear"),
            ("ConnectionDialog.qml", "function onConnectionCompleted", "function onConnectionComplete",
             "no signal connectionComplete"),
            ("GalleryWheel.qml", "function onWheelRequested", "function onWheelRequest", "no signal wheelRequest"),
            ("GalleryWheel.qml", 'property: "view"', 'property: "veiw"', "no property veiw"),
            ("UnsavedReview.qml", "function onModelReset", "function onModelResett", "no signal modelResett"),
            # A comment or a brace in a comment or string must not hide what follows.
            ("ConnectionDialog.qml", "target: dialog.accounts",
             "target: dialog.accounts  // { the account list\n        function onChangedd() {}",
             "no signal changedd"),
            # Guarded locals and properties, the usual style here.
            ("StatisticsDialog.qml", "    function", "    function probe() { const library = dialog.app ? "
             "dialog.app.library : null; return library.aiCategoryCountz }\n    function",
             "library.aiCategoryCountz"),
            # Forms the scan cannot follow fail instead of passing unseen.
            ("MainWindow.qml", "window.app.library.aiCategoryCounts", "window.app.library?.aiCategoryCounts",
             "cannot be checked"),
            ("MainWindow.qml", "title: app ?", 'title: app["versio"] || app ?', "cannot be checked"),
        ]
        for name, old, new, expected in planted:
            with self.subTest(new):
                self.assertIn(old, everything[name])
                changed = dict(everything, **{name: everything[name].replace(old, new, 1)})
                problems = scan(changed)[0]
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(expected, problems[0])

    def test_comments_and_strings_are_not_read_as_code(self):
        code, masked = mask('x: "a // b { app.foo" + app.version /* app.bar } */ // app.baz\ny: "\\\\" + app.qux')
        self.assertNotIn("app.foo", masked)
        self.assertNotIn("app.bar", code)
        self.assertNotIn("app.baz", code)
        self.assertIn('"a // b { app.foo"', code)
        self.assertIn("app.version", masked)
        self.assertIn("app.qux", masked)  # a string ending in a backslash still closes
        self.assertEqual(len(code), len(masked))
        problems = scan({"Probe.qml": 'Item {\n    Connections {\n        function onWidthChanged() {}\n    }\n'
                                      '    property string s: "library.db { " + app.version\n}\n'})[0]
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
