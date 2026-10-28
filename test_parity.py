"""Quick self-check: python test_parity.py (runs anywhere, no Windows needed)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")   # Windows consoles default to a legacy code page

from parity.demo import demo_result
from parity.exporter import _ps, disk_summary, export_kit
from parity.linux import storage
from parity.models import App
from parity.scanner import categorize, merge_winget, parse_winget_list, scan

WINGET_LIST = """\r   - \r   \\ \r
Name                                   Id                                   Version        Available Source
-------------------------------------------------------------------------------------------------------------
Mozilla Firefox (x64 en-US)            Mozilla.Firefox                      131.0          132.0     winget
Microsoft Visual Studio Code Insiders… Microsoft.VisualStudioCode.Insiders  1.95.0                   winget
Windows Terminal                       9N0DX20HK701                         1.21.2911.0              msstore
Calculator                             MSIX\\Microsoft.WindowsCalculator_11… 11.2409.0.0
Some ARP thing                         ARP\\Machine\\X64\\{ABC}               1.0
"""


def test_parse_and_merge():
    rows = parse_winget_list(WINGET_LIST)
    assert [r["Id"] for r in rows][:3] == ["Mozilla.Firefox", "Microsoft.VisualStudioCode.Insiders", "9N0DX20HK701"], rows
    export = {"Mozilla.Firefox": "winget", "Microsoft.VisualStudioCode.Insiders": "winget", "9N0DX20HK701": "msstore"}
    apps = [categorize(App("Mozilla Firefox (x64 en-US)")),
            categorize(App("Microsoft Visual Studio Code Insiders (User)")),
            categorize(App("Microsoft Visual C++ 2015-2022 Redistributable (x64)"))]
    out = {a.name: a for a in merge_winget(apps, rows, export)}
    assert out["Mozilla Firefox (x64 en-US)"].pkg_id == "Mozilla.Firefox"
    assert out["Microsoft Visual Studio Code Insiders (User)"].pkg_id == "Microsoft.VisualStudioCode.Insiders"  # truncated name match
    assert out["Windows Terminal"].manager == "msstore"                  # winget-only row added
    assert out["Calculator"].source == "Store" and not out["Calculator"].pkg_id
    assert not out["Microsoft Visual C++ 2015-2022 Redistributable (x64)"].selected  # runtime starts left out


def test_export():
    r = demo_result()
    assert "Unplug it" in disk_summary(r)
    assert _ps("O’Brien's") == "'O''Brien''s'"
    with tempfile.TemporaryDirectory() as d:
        files = {p.name: p for p in export_kit(r, Path(d) / "kit")}
        ps1 = files["restore.ps1"].read_text(encoding="utf-8-sig")
        assert "Id = 'Mozilla.Firefox'" in ps1 and "Source = 'msstore'" in ps1
        assert "'Colour Contrast Analyser'" in ps1.split("$manual = @(")[1]          # manual list
        assert "Redistributable" not in ps1                                           # left out
        j = json.loads(files["winget-packages.json"].read_text())
        assert {s["SourceDetails"]["Name"] for s in j["Sources"]} == {"winget", "msstore"}
        html = files["report.html"].read_text()
        assert "NVIDIA graphics driver" in html and "MAG B650 TOMAHAWK" in html


LSBLK = {"blockdevices": [
    {"name": "zram0", "type": "disk", "size": 8000000000},
    {"name": "nvme0n1", "type": "disk", "model": "WD_BLACK SN770 1TB", "size": 1000204886016, "rota": False, "tran": "nvme",
     "children": [
         {"name": "nvme0n1p1", "type": "part", "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "mountpoints": ["/boot"]},
         {"name": "nvme0n1p2", "type": "part", "parttype": "0fc63daf-8483-4772-8e79-3d69d8477de4", "mountpoints": [None],
          "children": [{"name": "luks-abc", "type": "crypt", "mountpoints": ["/home", "/"]}]}]},
    {"name": "nvme1n1", "type": "disk", "model": "Samsung SSD 990 PRO 2TB", "size": 2000398934016, "rota": "0", "tran": "nvme",
     "children": [
         {"name": "nvme1n1p1", "type": "part", "parttype": "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"},
         {"name": "nvme1n1p2", "type": "part", "parttype": "e3c9e316-0b5c-4db8-817d-f92df00215ae"},
         {"name": "nvme1n1p3", "type": "part", "parttype": "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7", "fstype": "ntfs"}]},
]}


def test_linux_storage():
    rows = {r["Disk"]: r for r in storage(LSBLK)}
    assert "zram0" not in rows
    assert rows["nvme0n1"]["Contains"] == "Linux (/), EFI boot partition"       # root found through the LUKS child
    assert rows["nvme1n1"]["Contains"] == "Windows partitions, EFI boot partition"
    assert rows["nvme1n1"]["Size (GB)"] == 2000 and rows["nvme1n1"]["Type"] == "SSD"
    r = demo_result()
    r.platform, r.hardware = "linux", [h for h in r.hardware if h.title != "Storage"]
    from parity.models import HardwareSection
    r.hardware.append(HardwareSection("Storage", list(rows.values())))
    assert "unplug it before reinstalling so the linux installer" in disk_summary(r).lower()
    # Root given only by findmnt (old lsblk without MOUNTPOINTS)
    old = {"blockdevices": [{"name": "sda", "type": "disk", "size": 1, "children": [{"name": "sda2", "type": "part"}]}]}
    assert storage(old, "/dev/sda2[/@]")[0]["Contains"] == "Linux (/)"


def test_restore_sh():
    r = demo_result()
    r.platform = "linux"
    r.apps = [App("firefox", pkg_id="firefox", manager="pacman"), App("Spotify", source="flathub", pkg_id="flathub com.spotify.Client",
                                                                       manager="flatpak"), App("O'Brien Tool")]
    with tempfile.TemporaryDirectory() as d:
        names = {p.name for p in export_kit(r, Path(d))}
        assert "restore.sh" in names and "restore.ps1" not in names
        text = (Path(d) / "restore.sh").read_bytes()
        assert b"\r\n" not in text and b'one "flatpak install -y --noninteractive" flathub com.spotify.Client' in text
        if shutil.which("bash") and sys.platform != "win32":
            subprocess.run(["bash", "-n", str(Path(d) / "restore.sh")], check=True)


def test_real_scan():
    """Scans the machine running the tests. In CI this runs on real Windows, macOS and Linux."""
    r = scan(print)
    system = r.section("System")
    assert system and system.items, r.hardware
    auto = sum(1 for a in r.apps if a.pkg_id)
    summary = (f"{r.os_name} · {len(r.apps)} programs ({auto} restorable) · "
               f"hardware: {', '.join(f'{h.title} {len(h.items)}' for h in r.hardware)}")
    print(f"  {r.platform}: {summary} · warnings: {r.warnings}")
    if os.environ.get("GITHUB_ACTIONS"):      # show up as annotations on the run page
        print(f"::notice title=Scan on {r.platform}::{summary}")
        for w in r.warnings:
            print(f"::warning title=Scan on {r.platform}::{w}")
    with tempfile.TemporaryDirectory() as d:
        assert len(export_kit(r, Path(d))) >= 4


if __name__ == "__main__":
    test_parse_and_merge()
    test_export()
    test_linux_storage()
    test_restore_sh()
    test_real_scan()
    print("all good")
