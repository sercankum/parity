"""macOS: Homebrew formulae and casks, Mac App Store apps, /Applications; hardware from system_profiler."""
from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shutil
from pathlib import Path

from .models import App, HardwareSection
from .scanner import categorize, run_out


def _tool(name: str) -> str | None:
    # Apps opened from Finder get a bare PATH without Homebrew on it.
    return shutil.which(name) or next(
        (p for p in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}") if os.path.exists(p)), None)


def _app_info(app: Path) -> tuple[str, str, str]:
    try:
        with open(app / "Contents" / "Info.plist", "rb") as f:
            p = plistlib.load(f)
    except (OSError, plistlib.InvalidFileException, ValueError):
        p = {}
    name = p.get("CFBundleDisplayName") or p.get("CFBundleName") or app.stem
    return str(name), str(p.get("CFBundleShortVersionString") or p.get("CFBundleVersion") or ""), str(p.get("CFBundleIdentifier") or "")


def packages(warnings: list[str]) -> list[App]:
    apps: list[App] = []
    covered: set[str] = set()     # .app bundles that a cask already accounts for

    brew = _tool("brew")
    if brew:
        info = json.loads(run_out([brew, "info", "--json=v2", "--installed"]) or "{}")
        for f in info.get("formulae", []):
            inst = f.get("installed") or []
            if any(i.get("installed_on_request") for i in inst):
                apps.append(categorize(App(name=f["name"], version=inst[0].get("version", ""), source="Homebrew",
                                           pkg_id=f["name"], manager="brew")))
        for c in info.get("casks", []):
            for art in c.get("artifacts", []):
                if isinstance(art, dict):
                    covered.update(x for x in art.get("app", []) if isinstance(x, str))
            apps.append(categorize(App(name=(c.get("name") or [c["token"]])[0], version=str(c.get("installed") or c.get("version") or ""),
                                       source="Homebrew", pkg_id=c["token"], manager="cask")))
    else:
        warnings.append("Homebrew isn't installed, so nothing outside the App Store can be reinstalled automatically.")

    mas_ids: dict[str, tuple[str, str]] = {}
    mas = _tool("mas")
    for line in run_out([mas, "list"]).splitlines() if mas else []:
        m = re.match(r"\s*(\d+)\s+(.+?)\s+\(([^)]*)\)\s*$", line)
        if m:
            mas_ids[m[2].casefold()] = (m[1], m[3])

    for folder in (Path("/Applications"), Path.home() / "Applications"):
        for app in sorted(folder.glob("*.app")):
            if app.name in covered:
                continue
            name, version, bundle_id = _app_info(app)
            if (app / "Contents" / "_MASReceipt").exists():
                hit = mas_ids.get(name.casefold())
                apps.append(App(name=name, version=version, source="App Store", category="Store app",
                                pkg_id=hit[0] if hit else "", manager="mas" if hit else ""))
            elif not bundle_id.startswith("com.apple."):     # Safari and friends come with macOS
                apps.append(categorize(App(name=name, version=version, source=str(folder))))
    if not mas and any(a.category == "Store app" for a in apps):
        warnings.append("Install `mas` (brew install mas) to reinstall App Store apps automatically.")
    return apps


def hardware() -> list[HardwareSection]:
    types = ["SPHardwareDataType", "SPDisplaysDataType", "SPStorageDataType", "SPNetworkDataType", "SPAudioDataType"]
    data = json.loads(run_out(["system_profiler", "-json", *types], timeout=120) or "{}")
    hw = (data.get("SPHardwareDataType") or [{}])[0]
    gpus = data.get("SPDisplaysDataType") or []
    return [
        HardwareSection("System", [{"Computer name": platform.node(), "Model": hw.get("machine_name", ""),
                                    "Model ID": hw.get("machine_model", ""), "OS": f"macOS {platform.mac_ver()[0]}",
                                    "Chip": hw.get("chip_type") or hw.get("cpu_type", ""),
                                    "Cores": hw.get("number_processors", ""), "Memory": hw.get("physical_memory", "")}]),
        HardwareSection("Graphics", [{"Name": g.get("sppci_model") or g.get("_name", ""), "Cores": g.get("sppci_cores", "")}
                                     for g in gpus]),
        HardwareSection("Monitors", [{"Name": d.get("_name", ""), "Resolution": d.get("_spdisplays_resolution", "")}
                                     for g in gpus for d in g.get("spdisplays_ndrvs") or []]),
        HardwareSection("Storage", [{"Volume": v.get("_name", ""), "Drive": (v.get("physical_drive") or {}).get("device_name", ""),
                                     "Format": v.get("file_system", ""), "Size (GB)": round(int(v.get("size_in_bytes") or 0) / 1e9),
                                     "Mounted at": v.get("mount_point", "")} for v in data.get("SPStorageDataType") or []]),
        HardwareSection("Network", [{"Name": n.get("_name", ""), "Type": n.get("type", ""), "Interface": n.get("interface", "")}
                                    for n in data.get("SPNetworkDataType") or []]),
        HardwareSection("Audio", [{"Name": d.get("_name", "")} for a in data.get("SPAudioDataType") or []
                                  for d in a.get("_items") or []]),
    ]
