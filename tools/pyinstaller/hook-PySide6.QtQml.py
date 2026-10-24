"""Collect the QML imports used by LuvBox, rather than every Qt add-on."""

from pathlib import Path, PurePosixPath

from PyInstaller.utils.hooks.qt import add_qt6_dependencies, pyside6_library_info


hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
binaries = [
    (source, destination)
    for source, destination in binaries
    if Path(source).parent.name.casefold() != "qmltooling"
]

QML_MODULES = (
    "QtQml", "QtQml/Models", "QtQml/WorkerScript", "QtCore",
    "QtQuick", "QtQuick/Window", "QtQuick/Layouts", "QtQuick/Templates",
    "QtQuick/Controls", "QtQuick/Controls/impl",
    "QtQuick/Controls/Basic", "QtQuick/Controls/Basic/impl",
    "QtQuick/Controls/Fusion", "QtQuick/Controls/Fusion/impl",
    "QtQuick/Dialogs", "QtQuick/Dialogs/quickimpl",
    "Qt/labs/folderlistmodel", "QtMultimedia",
)

if pyside6_library_info.version is not None:
    location = pyside6_library_info.location
    source = Path(location.get("QmlImportsPath") or location["Qml2ImportsPath"]).resolve()
    destination = PurePosixPath(pyside6_library_info.qt_rel_dir) / "qml"
    for module in QML_MODULES:
        qmldir = source / module / "qmldir"
        if not qmldir.is_file():
            raise RuntimeError(f"Required Qt QML module is missing: {module}")
        module_binaries, module_datas = pyside6_library_info._process_qml_plugin(qmldir)
        if not module_binaries and not module_datas:
            raise RuntimeError(f"Required Qt QML module dependencies are unavailable: {module}")
        for files, collected in ((module_binaries, binaries), (module_datas, datas)):
            for path in files:
                relative = path.relative_to(source) if path.is_dir() else path.parent.relative_to(source)
                collected.append((str(path), str(destination / relative.as_posix())))
