"""Shared scanning helpers, the Windows scanner (PowerShell + winget), and scan() which picks the right OS."""
from __future__ import annotations

import base64
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from typing import Callable

from .models import App, HardwareSection, ScanResult

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class ScanError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    try:
        # stdin=DEVNULL: windowed PyInstaller builds have no console handle to inherit.
        p = subprocess.run(cmd, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
                           creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]} took longer than {timeout} seconds"
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def run_out(cmd: list[str], timeout: int = 300) -> str:
    """stdout of a command, or "" if it's missing or fails."""
    code, out, _ = _run(cmd, timeout)
    return out if code == 0 else ""


def ps_json(script: str):
    """Run a PowerShell script that prints JSON and return the parsed value."""
    prelude = "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;$ProgressPreference='SilentlyContinue';"
    # EncodedCommand avoids every quoting problem with multi-line scripts.
    enc = base64.b64encode((prelude + script).encode("utf-16-le")).decode()
    code, out, err = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc])
    out = out.lstrip("﻿").strip()
    if not out:
        if code != 0:
            raise ScanError(err.strip() or f"PowerShell exited with code {code}")
        return None
    return json.loads(out)


def as_list(v) -> list:
    return [] if v is None else v if isinstance(v, list) else [v]


# ---------------------------------------------------------------- software

REGISTRY_PS = r"""
$paths = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
         'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
         'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
$items = foreach ($p in $paths) {
  Get-ItemProperty -Path $p -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -and $_.SystemComponent -ne 1 -and -not $_.ParentKeyName -and -not $_.ReleaseType } |
    ForEach-Object { [pscustomobject]@{ Name=[string]$_.DisplayName; Version=[string]$_.DisplayVersion; Publisher=[string]$_.Publisher } }
}
ConvertTo-Json -InputObject @($items) -Depth 3 -Compress
"""

# Only used when winget is missing; winget list gives nicer Store names.
APPX_PS = r"""
$items = Get-AppxPackage -ErrorAction SilentlyContinue |
  Where-Object { -not $_.IsFramework -and -not $_.NonRemovable -and $_.SignatureKind -eq 'Store' } |
  ForEach-Object { [pscustomobject]@{ Name=[string]$_.Name; Version=[string]$_.Version; Publisher=[string]$_.Publisher } }
ConvertTo-Json -InputObject @($items) -Depth 3 -Compress
"""

RUNTIME_RE = re.compile(r"redistributable|\bruntime\b|\.net (framework|desktop|core)|directx|webview2|windows sdk|"
                        r"update health|windows app runtime|xna framework|vulkan ?run ?time", re.I)
DRIVER_RE = re.compile(r"\bdrivers?\b|chipset|realtek|physx|nvidia (graphics|hd audio|frameview|usbc)|"
                       r"intel\(r\) (management|serial io|chipset|rapid storage|graphics)|amd (chipset|install manager)", re.I)
UPDATE_RE = re.compile(r"\(KB\d+\)|security update|^update for", re.I)


def categorize(app: App) -> App:
    if UPDATE_RE.search(app.name):
        app.category = "Update"
    elif RUNTIME_RE.search(app.name):
        app.category = "Runtime"
    elif DRIVER_RE.search(app.name):
        app.category = "Driver"
    # Windows, drivers and installers bring these back on their own.
    app.selected = app.category in ("App", "Store app")
    return app


def parse_winget_list(text: str) -> list[dict]:
    """Parse the fixed-width table `winget list` prints. Best effort: winget truncates long cells with '…'."""
    lines = [ANSI.sub("", l).split("\r")[-1] for l in text.splitlines()]
    hdr_i = next((i for i, l in enumerate(lines) if re.match(r"^\s*Name\s+Id\s+Version", l)), None)
    if hdr_i is None:
        return []
    header = lines[hdr_i]
    starts = [m.start() for m in re.finditer(r"\S+", header)]
    names = header.split()
    rows = []
    for line in lines[hdr_i + 2:]:
        if not line.strip() or set(line.strip()) <= {"-"}:
            continue
        cells = {n: line[s:(starts[j + 1] if j + 1 < len(starts) else None)].strip()
                 for j, (n, s) in enumerate(zip(names, starts))}
        if cells.get("Name") and cells.get("Id"):
            rows.append(cells)
    return rows


def winget_export(warnings: list[str]) -> tuple[dict[str, str], dict]:
    """Return ({package id: source name}, {source name: SourceDetails}) for everything winget can reinstall."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        base = ["winget", "export", "-o", path, "--accept-source-agreements"]
        code, _, err = _run(base + ["--disable-interactivity"])
        if code != 0 and os.path.getsize(path) == 0:
            code, _, err = _run(base)          # older winget without --disable-interactivity
        if os.path.getsize(path) == 0:
            warnings.append("winget export produced nothing, so no programs are marked as restorable.")
            return {}, {}
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    finally:
        os.remove(path)
    ids, sources = {}, {}
    for src in data.get("Sources", []):
        details = src.get("SourceDetails", {})
        sources[details.get("Name", "winget")] = details
        for pkg in src.get("Packages", []):
            ids[pkg["PackageIdentifier"]] = details.get("Name", "winget")
    return ids, sources


def _norm(s: str) -> str:
    return " ".join(s.split()).casefold()


def merge_winget(apps: list[App], rows: list[dict], export: dict[str, str]) -> list[App]:
    """Attach winget IDs to registry apps and add winget/Store-only rows."""
    def resolve(raw: str) -> str | None:
        if raw in export:
            return raw
        if raw.endswith("…"):
            hits = [i for i in export if i.startswith(raw[:-1])]
            return hits[0] if len(hits) == 1 else None
        return None

    name_to_id = {}
    for r in rows:
        pid = resolve(r["Id"])
        if pid:
            name_to_id[_norm(r["Name"])] = (pid, r)

    used = set()
    for a in apps:
        n = _norm(a.name)
        hit = name_to_id.get(n) or next(
            (v for k, v in name_to_id.items() if k.endswith("…") and n.startswith(k[:-1]) and v[0] not in used), None)
        if hit:
            a.pkg_id, a.manager = hit[0], export[hit[0]]
            used.add(hit[0])

    names = {_norm(a.name) for a in apps}
    for pid, src in export.items():
        if pid in used:
            continue
        row = next((r for r in rows if resolve(r["Id"]) == pid), {})
        apps.append(categorize(App(name=row.get("Name", pid).rstrip("…"), version=row.get("Version", ""),
                                   source="winget", pkg_id=pid, manager=src,
                                   category="Store app" if src == "msstore" else "App")))
    # Store apps winget can't reinstall still show up in `winget list` as MSIX\… ids.
    for r in rows:
        if r["Id"].startswith("MSIX\\") and _norm(r["Name"]) not in names:
            apps.append(categorize(App(name=r["Name"].rstrip("…"), version=r.get("Version", ""),
                                       source="Store", category="Store app")))
    return apps


def scan_software(progress: Callable[[str], None], warnings: list[str]) -> tuple[list[App], dict]:
    progress("Reading installed programs…")
    seen, apps = set(), []
    for r in as_list(ps_json(REGISTRY_PS)):
        key = (_norm(r["Name"]), r.get("Version") or "")
        if key not in seen:
            seen.add(key)
            apps.append(categorize(App(name=r["Name"].strip(), version=r.get("Version") or "",
                                       publisher=r.get("Publisher") or "")))

    if not shutil.which("winget"):
        warnings.append("winget isn't installed, so nothing is marked as auto-restorable. Update 'App Installer' from the Microsoft Store.")
        for r in as_list(ps_json(APPX_PS)):
            name = re.sub(r"^[0-9A-F]{8}\.", "", r["Name"])
            pub = re.search(r"CN=([^,]+)", r.get("Publisher") or "")
            apps.append(categorize(App(name=name, version=r.get("Version") or "",
                                       publisher=pub.group(1) if pub else "", source="Store", category="Store app")))
        return apps, {}

    progress("Asking winget what it can reinstall… (this is the slow part)")
    export, sources = winget_export(warnings)
    _, out, _ = _run(["winget", "list", "--accept-source-agreements", "--disable-interactivity"])
    rows = parse_winget_list(out)
    if not rows:
        warnings.append("Couldn't read `winget list`, so some winget matches may be missing.")
    return merge_winget(apps, rows, export), sources


# ---------------------------------------------------------------- hardware

HARDWARE_PS = r"""
$r = [ordered]@{}
function Add-Section([string]$Name, [scriptblock]$Block) {
  try { $r[$Name] = @(& $Block) } catch { $r[$Name] = @([ordered]@{ Error = $_.Exception.Message }) }
}
function D($v) { if ($v) { ([datetime]$v).ToString('yyyy-MM-dd') } else { '' } }

Add-Section 'System' {
  $cs = Get-CimInstance Win32_ComputerSystem; $os = Get-CimInstance Win32_OperatingSystem
  [ordered]@{ 'Computer name'=$env:COMPUTERNAME; Manufacturer=$cs.Manufacturer; Model=$cs.Model;
              Windows=$os.Caption; Version="$($os.Version) (build $($os.BuildNumber))";
              'Installed RAM (GB)'=[math]::Round($cs.TotalPhysicalMemory / 1GB, 1) }
}
Add-Section 'Motherboard' {
  $b = Get-CimInstance Win32_BaseBoard
  [ordered]@{ Manufacturer=$b.Manufacturer; Model=$b.Product; Revision=$b.Version }
}
Add-Section 'BIOS' {
  $b = Get-CimInstance Win32_BIOS
  [ordered]@{ Vendor=$b.Manufacturer; Version=$b.SMBIOSBIOSVersion; Date=(D $b.ReleaseDate) }
}
Add-Section 'CPU' {
  Get-CimInstance Win32_Processor | ForEach-Object {
    [ordered]@{ Name=$_.Name.Trim(); Cores=$_.NumberOfCores; Threads=$_.NumberOfLogicalProcessors;
                'Max clock (MHz)'=$_.MaxClockSpeed; Socket=$_.SocketDesignation } }
}
Add-Section 'Memory' {
  Get-CimInstance Win32_PhysicalMemory | ForEach-Object {
    [ordered]@{ Slot=$_.DeviceLocator; Manufacturer=$_.Manufacturer; 'Part number'=([string]$_.PartNumber).Trim();
                'Size (GB)'=[math]::Round($_.Capacity / 1GB); 'Speed (MT/s)'=$_.Speed } }
}
Add-Section 'Graphics' {
  Get-CimInstance Win32_VideoController | ForEach-Object {
    [ordered]@{ Name=$_.Name; 'Driver version'=$_.DriverVersion; 'Driver date'=(D $_.DriverDate);
                'Current mode'=$_.VideoModeDescription } }
}
Add-Section 'Storage' {
  $winDisk = $null
  try { $winDisk = (Get-Partition -DriveLetter $env:SystemDrive.TrimEnd(':') -ErrorAction Stop).DiskNumber } catch {}
  $linux = '{0fc63daf-8483-4772-8e79-3d69d8477de4}','{4f68bce3-e8cd-4db1-96e7-fbcaf984b709}',
           '{0657fd6d-a4ab-43c4-84e5-0933c84b4f4f}','{933ac7e1-2eb4-4f13-b844-0e14e2aef915}','{e6d6d379-f507-44c2-a23c-238f2a3df928}'
  Get-PhysicalDisk | Sort-Object { [int]$_.DeviceId } | ForEach-Object {
    $n = [int]$_.DeviceId
    $parts = @(Get-Partition -DiskNumber $n -ErrorAction SilentlyContinue)
    $has = @()
    if ($n -eq $winDisk) { $has += "Windows ($env:SystemDrive)" }
    if ($parts | Where-Object { $linux -contains "$($_.GptType)" }) { $has += 'Linux partitions' }
    if ($parts | Where-Object { "$($_.GptType)" -eq '{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}' }) { $has += 'EFI boot partition' }
    [ordered]@{ Disk=$n; Model=$_.FriendlyName; Type="$($_.MediaType)"; Bus="$($_.BusType)";
                'Size (GB)'=[math]::Round($_.Size / 1GB); Health="$($_.HealthStatus)"; Contains=($has -join ', ') } }
}
Add-Section 'Network' {
  Get-NetAdapter -Physical -ErrorAction Stop | ForEach-Object {
    [ordered]@{ Name=$_.Name; Adapter=$_.InterfaceDescription; Status="$($_.Status)"; Speed=$_.LinkSpeed; MAC=$_.MacAddress } }
}
Add-Section 'Audio' {
  Get-CimInstance Win32_SoundDevice | ForEach-Object { [ordered]@{ Name=$_.Name; Manufacturer=$_.Manufacturer } }
}
Add-Section 'Monitors' {
  $dec = { param($a) (($a | Where-Object { $_ -ne 0 }) | ForEach-Object { [char]$_ }) -join '' }
  Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction Stop | ForEach-Object {
    [ordered]@{ Name=(& $dec $_.UserFriendlyName); Maker=(& $dec $_.ManufacturerName); 'Year made'=$_.YearOfManufacture } }
}
$r | ConvertTo-Json -Depth 5 -Compress
"""

# Storage cmdlets sometimes hand back enum numbers instead of names.
MEDIA = {"0": "Unspecified", "3": "HDD", "4": "SSD", "5": "SCM"}
BUS = {"1": "SCSI", "3": "ATA", "7": "USB", "8": "RAID", "10": "SAS", "11": "SATA", "12": "SD", "17": "NVMe"}


def scan_hardware(progress: Callable[[str], None]) -> list[HardwareSection]:
    progress("Reading hardware…")
    data = ps_json(HARDWARE_PS) or {}
    sections = [HardwareSection(title, [dict(i) for i in as_list(items) if i]) for title, items in data.items()]
    for s in sections:
        if s.title == "Storage":
            for d in s.items:
                d["Type"] = MEDIA.get(str(d.get("Type")), d.get("Type"))
                d["Bus"] = BUS.get(str(d.get("Bus")), d.get("Bus"))
    return sections


def scan(progress: Callable[[str], None] = lambda _: None) -> ScanResult:
    warnings: list[str] = []
    sources: dict = {}
    if sys.platform == "win32":
        plat = "windows"
        apps, sources = scan_software(progress, warnings)
        hardware = scan_hardware(progress)
    elif sys.platform == "darwin" or sys.platform.startswith("linux"):
        from . import linux, macos
        plat, mod = ("macos", macos) if sys.platform == "darwin" else ("linux", linux)
        progress("Reading installed packages…")
        apps = mod.packages(warnings)
        progress("Reading hardware…")
        hardware = mod.hardware()
    else:
        raise ScanError(f"Parity doesn't know how to scan {sys.platform} yet.")
    system = next((s.items[0] for s in hardware if s.title == "System" and s.items), {})
    os_name = (f"{system.get('Windows', '')} {system.get('Version', '')}" if plat == "windows"
               else str(system.get("OS", "")))
    apps.sort(key=lambda a: a.name.casefold())
    return ScanResult(apps=apps, hardware=hardware, computer_name=platform.node() or "this computer",
                      os_name=os_name.strip(), scanned_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                      platform=plat, warnings=warnings, winget_sources=sources)
