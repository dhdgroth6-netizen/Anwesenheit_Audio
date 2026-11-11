"""Sammelt die Lizenztexte aller mitgelieferten Komponenten in THIRD_PARTY_LICENSES.txt.
Wird von Installer_bauen.bat vor dem Bauen aufgerufen."""
import glob
import importlib.metadata as md
import os
import re
import sys

DIRECT = ["opencv-python", "pycaw", "comtypes", "pygrabber", "pystray", "pillow"]
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "THIRD_PARTY_LICENSES.txt")


def closure(names):
    seen, todo = {}, list(names)
    while todo:
        name = todo.pop()
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue
        key = dist.metadata["Name"].lower()
        if key in seen:
            continue
        seen[key] = dist
        for req in dist.requires or []:
            if "extra ==" in req:
                continue
            marker = req.split(";", 1)[1] if ";" in req else ""
            if "sys_platform" in marker and "win32" not in marker and "!=" not in marker:
                continue
            todo.append(re.split(r"[ ;<>=!~\[(]", req, 1)[0])
    return seen


def license_files(dist):
    files = [f for f in (dist.files or [])
             if re.search(r"(LICEN[CS]E|COPYING|NOTICE)", os.path.basename(str(f)), re.I)
             and "dist-info" in str(f)]
    return [dist.locate_file(f) for f in files]


def main():
    parts = [
        "Anwesenheit Audio – Lizenzen mitgelieferter Komponenten\n"
        "=========================================================\n\n"
        "Dieses Programm enthält die folgenden Komponenten Dritter.\n"
        "This program bundles the following third-party components.\n"
    ]

    def add(title, text):
        parts.append("\n\n" + "#" * 78 + f"\n# {title}\n" + "#" * 78 + "\n\n" + text.strip() + "\n")

    for key, dist in sorted(closure(DIRECT).items()):
        meta = dist.metadata
        lic = meta.get("License-Expression") or meta.get("License") or ""
        head = f"{meta['Name']} {dist.version}"
        texts = []
        for p in license_files(dist):
            try:
                texts.append(f"--- {os.path.basename(str(p))} ---\n" + open(p, encoding="utf-8", errors="replace").read())
            except OSError:
                pass
        if not texts:
            texts.append(f"License: {lic}\n{meta.get('Home-page') or ''}")
        add(head + (f" ({lic.splitlines()[0][:60]})" if lic else ""), "\n\n".join(texts))

    model_lic = os.path.join(HERE, "LICENSE_yunet.txt")
    add("YuNet Face Detection Model (face_detection_yunet_2023mar.onnx) – OpenCV Zoo (MIT)",
        open(model_lic, encoding="utf-8").read())

    py_lic = os.path.join(sys.base_prefix, "LICENSE.txt")
    if os.path.exists(py_lic):
        add(f"Python {sys.version.split()[0]} (PSF License)", open(py_lic, encoding="utf-8", errors="replace").read())

    for terms in glob.glob(os.path.join(sys.base_prefix, "tcl", "tcl8*", "license.terms"))[:1] + \
            glob.glob(os.path.join(sys.base_prefix, "tcl", "tk8*", "license.terms"))[:1]:
        add("Tcl/Tk (" + os.path.basename(os.path.dirname(terms)) + ")",
            open(terms, encoding="utf-8", errors="replace").read())

    add("PyInstaller bootloader",
        "Built with PyInstaller (https://pyinstaller.org). PyInstaller is licensed under the GPL 2.0 "
        "with a special exception that allows distributing the resulting executables under any license.")

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write("".join(parts))
    print(f"{OUT} geschrieben ({len(parts) - 1} Komponenten)")


if __name__ == "__main__":
    main()
