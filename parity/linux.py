"""Linux: pacman/AUR, apt, dnf, Flatpak and Snap packages; hardware from /sys, /proc, lspci and lsblk."""
from __future__ import annotations

import json
import os
import platform
import re
from pathlib import Path

from .models import App, HardwareSection
from .scanner import categorize, run_out

DMI = Path("/sys/class/dmi/id")
LINUX_PARTS = {"0fc63daf-8483-4772-8e79-3d69d8477de4", "4f68bce3-e8cd-4db1-96e7-fbcaf984b709",
               "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f", "933ac7e1-2eb4-4f13-b844-0e14e2aef915",
               "e6d6d379-f507-44c2-a23c-238f2a3df928", "0x83", "0x8e"}
WINDOWS_PARTS = {"e3c9e316-0b5c-4db8-817d-f92df00215ae", "de94bba4-06d1-4d40-a16a-bfd50179d6ac"}  # MS reserved, WinRE
EFI = {"c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "0xef"}


def _read(p) -> str:
    try:
        return Path(p).read_text(errors="replace").strip()
    except OSError:
        return ""


def _pairs(text: str):
    """Lines of 'name version' as tuples, skipping blanks."""
    for line in text.splitlines():
        parts = line.split(maxsplit=1)
        if parts:
            yield parts[0], parts[1].strip() if len(parts) > 1 else ""


# ---------------------------------------------------------------- packages

def packages(warnings: list[str]) -> list[App]:
    apps: list[App] = []

    def add(name, version, manager, pkg_id=None, source=None, publisher=""):
        a = categorize(App(name=name, version=version, publisher=publisher, source=source or manager,
                           pkg_id=pkg_id or name, manager=manager))
        a.selected = True     # explicitly installed packages are what you'd want back
        apps.append(a)

    # Only packages someone asked for, not their dependencies.
    foreign = set(run_out(["pacman", "-Qqm"]).split())
    for n, v in _pairs(run_out(["pacman", "-Qe"])):
        add(n, v, "aur" if n in foreign else "pacman")
    if not apps:
        manual = set(run_out(["apt-mark", "showmanual"]).split())
        for n, v in _pairs(run_out(["dpkg-query", "-W", "-f=${binary:Package} ${Version}\n"])):
            if n.split(":")[0] in manual or n in manual:
                add(n.split(":")[0], v, "apt")
    if not apps:
        # dnf5 needs the explicit newline, dnf4 adds its own (blank lines are skipped).
        for n, v in _pairs(run_out(["dnf", "repoquery", "--userinstalled", "--qf", "%{name} %{version}\n"])):
            add(n, v, "dnf")
    if not apps:
        warnings.append("No pacman, apt or dnf packages found, so system packages aren't listed.")

    for line in run_out(["flatpak", "list", "--app", "--columns=application,name,version,origin"]).splitlines():
        cols = line.split("\t")
        if len(cols) == 4:
            app_id, name, version, origin = cols
            add(name or app_id, version, "flatpak", pkg_id=f"{origin} {app_id}", source=origin)

    snaps = run_out(["snap", "list"]).splitlines()[1:]
    for line in snaps:
        cols = line.split()
        if len(cols) >= 4:
            notes = cols[5] if len(cols) > 5 else ""
            if re.search(r"\b(base|snapd|core)\b", notes) or cols[0].startswith(("core", "snapd", "bare", "gnome-", "gtk-common")):
                continue          # runtimes snaps pull in on their own
            add(cols[0], cols[1], "snap", pkg_id=cols[0] + (" --classic" if "classic" in notes else ""))
    return apps


# ---------------------------------------------------------------- hardware

def storage(tree: dict, root_source: str = "") -> list[dict]:
    """Turn `lsblk -J` output into disk rows, marking which OS lives where."""
    def walk(dev):
        yield dev
        for c in dev.get("children") or []:
            yield from walk(c)

    root_name = Path(re.sub(r"\[.*\]$", "", root_source)).name   # /dev/nvme0n1p2[/@] -> nvme0n1p2
    rows = []
    for d in tree.get("blockdevices", []):
        if d.get("type") != "disk" or d.get("name", "").startswith(("zram", "loop", "ram")):
            continue
        devs = list(walk(d))
        mounts = {m for x in devs for m in (x.get("mountpoints") or [x.get("mountpoint")]) if m}
        types = {str(x.get("parttype") or "").lower() for x in devs}
        has = []
        if "/" in mounts or (root_name and any(x.get("name") == root_name for x in devs)):
            has.append("Linux (/)")
        elif types & LINUX_PARTS:
            has.append("Linux partitions")
        if types & WINDOWS_PARTS:
            has.append("Windows partitions")
        if types & EFI:
            has.append("EFI boot partition")
        rota = d.get("rota")
        rows.append({"Disk": d.get("name"), "Model": (d.get("model") or "").strip(),
                     "Type": "HDD" if rota in (True, "1", 1) else "SSD", "Bus": (d.get("tran") or "").upper(),
                     "Size (GB)": round(int(d.get("size") or 0) / 1e9), "Contains": ", ".join(has)})
    return rows


def _lsblk() -> dict:
    cols = "NAME,MODEL,SIZE,ROTA,TRAN,TYPE,PARTTYPE,FSTYPE,MOUNTPOINTS"
    out = run_out(["lsblk", "-J", "-b", "-o", cols]) or run_out(["lsblk", "-J", "-b", "-o", cols[:-1]])  # old lsblk: MOUNTPOINT
    return json.loads(out) if out else {}


def hardware() -> list[HardwareSection]:
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', _read("/etc/os-release"), re.M))
    mem = re.search(r"MemTotal:\s+(\d+)", _read("/proc/meminfo"))
    cpuinfo = _read("/proc/cpuinfo")
    first = lambda key: (re.search(rf"^{key}\s*:\s*(.+)$", cpuinfo, re.M) or [None, ""])[1]

    lspci = run_out(["lspci"]).splitlines()
    pci = lambda pat: [{"Name": l.split(": ", 1)[-1]} for l in lspci if re.search(pat, l)]

    net = []
    for p in sorted(Path("/sys/class/net").glob("*")):
        if (p / "device").exists():           # physical adapters only
            speed = _read(p / "speed")
            net.append({"Name": p.name, "Status": _read(p / "operstate"),
                        "Speed": f"{speed} Mbps" if speed.isdigit() and int(speed) > 0 else "", "MAC": _read(p / "address")})

    root = run_out(["findmnt", "-no", "SOURCE", "/"]).strip()
    return [
        HardwareSection("System", [{"Computer name": platform.node(), "Manufacturer": _read(DMI / "sys_vendor"),
                                    "Model": _read(DMI / "product_name"), "OS": osr.get("PRETTY_NAME", "Linux"),
                                    "Kernel": platform.release(),
                                    "Installed RAM (GB)": round(int(mem[1]) / 1024 ** 2, 1) if mem else ""}]),
        HardwareSection("Motherboard", [{"Manufacturer": _read(DMI / "board_vendor"), "Model": _read(DMI / "board_name"),
                                         "Revision": _read(DMI / "board_version")}]),
        HardwareSection("BIOS", [{"Vendor": _read(DMI / "bios_vendor"), "Version": _read(DMI / "bios_version"),
                                  "Date": _read(DMI / "bios_date")}]),
        HardwareSection("CPU", [{"Name": first("model name") or platform.processor() or platform.machine(),
                                 "Cores": first("cpu cores"), "Threads": os.cpu_count()}]),
        HardwareSection("Graphics", pci(r"VGA|3D controller|Display controller")),
        HardwareSection("Storage", storage(_lsblk(), root)),
        HardwareSection("Network", net),
        HardwareSection("Audio", pci(r"Audio device|Multimedia audio")),
    ]
