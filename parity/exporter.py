"""Writes the restore kit: report.html, a restore script, software.csv, hardware.json (+ winget-packages.json on Windows)."""
from __future__ import annotations

import csv
import json
import os
import shlex
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote_plus

from .models import App, ScanResult

DEFAULT_SOURCES = {
    "winget": {"Argument": "https://cdn.winget.microsoft.com/cache", "Identifier": "Microsoft.Winget.Source_8wekyb3d8bbwe",
               "Name": "winget", "Type": "Microsoft.PreIndexed.Package"},
    "msstore": {"Argument": "https://storeedgefd.dsx.mp.microsoft.com/v9.0", "Identifier": "StoreEdgeFD",
                "Name": "msstore", "Type": "Microsoft.Rest"},
}


def split_apps(apps: list[App]) -> tuple[list[App], list[App], list[App]]:
    """(reinstall automatically, install by hand, left out) — the automatic list deduped by package."""
    auto, ids = [], set()
    for a in apps:
        if a.selected and a.pkg_id and (a.manager, a.pkg_id) not in ids:
            ids.add((a.manager, a.pkg_id))
            auto.append(a)
    manual = [a for a in apps if a.selected and not a.pkg_id]
    skipped = [a for a in apps if not a.selected]
    return auto, manual, skipped


def disk_summary(result: ScanResult) -> str | None:
    sec = result.section("Storage")
    if not sec:
        return None

    def label(d):
        return f"Disk {d.get('Disk')} ({d.get('Model')}, {d.get('Size (GB)')} GB)"

    me = {"windows": "Windows", "linux": "Linux"}.get(result.platform)
    if not me:
        return None
    other = "Linux" if me == "Windows" else "Windows"
    has = lambda d, s: s in str(d.get("Contains", ""))
    mine = [d for d in sec.items if has(d, f"{me} (")]          # the disk this system runs from
    theirs = [d for d in sec.items if has(d, f"{other} partitions")]
    shared = [d for d in mine if d in theirs]
    if shared:
        return (f"{me} and {other} share {label(shared[0])}. Don't wipe the whole disk: in the installer, "
                f"delete only the {me} partitions.")
    parts = []
    if mine:
        parts.append(f"{me} is on {label(mine[0])}.")
    if theirs:
        them = "it" if len(theirs) == 1 else "them"
        parts.append(f"{other} partitions are on " + ", ".join(label(d) for d in theirs)
                     + f". Unplug {them} before reinstalling so the {me} installer can't touch the {other} bootloader.")
    return " ".join(parts) or None


def driver_links(result: ScanResult) -> list[tuple[str, str]]:
    links = []
    board = (result.section("Motherboard") or None)
    if board and board.items:
        b = board.items[0]
        name = f"{b.get('Manufacturer', '')} {b.get('Model', '')}".strip()
        if name:
            links.append((f"Motherboard support page: {name} (chipset, LAN, Wi-Fi, audio, BIOS)",
                          "https://www.google.com/search?q=" + quote_plus(f"{name} support drivers")))
    vendors = " ".join(str(i.get("Name", "")) for s in ("Graphics", "CPU") for i in (result.section(s).items if result.section(s) else []))
    v = vendors.lower()
    if "nvidia" in v:
        links.append(("NVIDIA graphics driver", "https://www.nvidia.com/en-us/drivers/"))
    if "amd" in v or "radeon" in v:
        links.append(("AMD graphics and chipset drivers", "https://www.amd.com/en/support/download/drivers.html"))
    if "intel" in v:
        links.append(("Intel Driver & Support Assistant", "https://www.intel.com/content/www/us/en/support/detect.html"))
    return links


# ---------------------------------------------------------------- restore files

def _ps(s: str) -> str:
    # PowerShell treats curly single quotes as quotes too.
    for q in "‘’‚‛":
        s = s.replace(q, "'")
    return "'" + s.replace("'", "''") + "'"


def restore_script(result: ScanResult, winget: list[App], manual: list[App]) -> str:
    pkgs = "\n".join(f"  @{{ Id = {_ps(a.pkg_id)}; Source = {_ps(a.manager or 'winget')}; Name = {_ps(a.name)} }}"
                     for a in winget)
    man = "\n".join(f"  {_ps(a.name)}" for a in manual)
    return f"""<#
  Parity restore script, made {result.scanned_at} on {result.computer_name}.
  Run it on the fresh Windows install:
    right-click restore.ps1 > Run with PowerShell
  or, for fewer admin prompts, from an admin terminal:
    powershell -ExecutionPolicy Bypass -File .\\restore.ps1
#>
$ErrorActionPreference = 'Continue'
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {{
  Write-Host 'winget was not found. Update "App Installer" in the Microsoft Store, then run this again.' -ForegroundColor Yellow
  Read-Host 'Press Enter to close'; exit 1
}}
$packages = @(
{pkgs}
)
$manual = @(
{man}
)
$log = Join-Path $PSScriptRoot 'restore-log.txt'
"Restore started $(Get-Date)" | Out-File $log -Encoding utf8
$failed = @(); $i = 0
foreach ($p in $packages) {{
  $i++
  Write-Host ("[{{0}}/{{1}}] {{2}}" -f $i, $packages.Count, $p.Name) -ForegroundColor Cyan
  winget install --id $p.Id --exact --source $p.Source --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
  "$($p.Id): exit $LASTEXITCODE" | Out-File $log -Append -Encoding utf8
  if ($LASTEXITCODE -ne 0) {{ $failed += $p }}
}}
Write-Host ''
Write-Host ("Done. {{0}} of {{1}} finished without errors." -f ($packages.Count - $failed.Count), $packages.Count) -ForegroundColor Green
if ($failed) {{
  Write-Host 'Check these (details in restore-log.txt; "already installed" counts as an error here):' -ForegroundColor Yellow
  $failed | ForEach-Object {{ Write-Host "  - $($_.Name) [$($_.Id)]" }}
}}
if ($manual) {{
  Write-Host ''; Write-Host 'Install these yourself (download links are in report.html):' -ForegroundColor Yellow
  $manual | ForEach-Object {{ Write-Host "  - $_" }}
}}
Read-Host 'Press Enter to close'
"""


SH_MANAGERS = {   # manager: (program to check for, install command, try all packages at once)
    "pacman": ("pacman", "sudo pacman -S --needed --noconfirm", True),
    "aur": ("$AUR", "$AUR -S --needed --noconfirm", True),
    "apt": ("apt-get", "sudo apt-get install -y", True),
    "dnf": ("dnf", "sudo dnf install -y", True),
    "flatpak": ("flatpak", "flatpak install -y --noninteractive", False),
    "snap": ("snap", "sudo snap install", False),
    "brew": ("brew", "brew install", True),
    "cask": ("brew", "brew install --cask", True),
    "mas": ("mas", "mas install", True),
}


def restore_sh(result: ScanResult, auto: list[App], manual: list[App]) -> str:
    lines = []
    for mgr, (tool, cmd, batch) in SH_MANAGERS.items():
        pkgs = [a for a in auto if a.manager == mgr]
        if not pkgs:
            continue
        lines.append(f'if have "{tool}"; then')
        if mgr == "flatpak" and any(a.source == "flathub" for a in pkgs):
            lines.append("  flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo")
        if batch:
            lines.append(f'  batch "{cmd}" ' + " ".join(shlex.quote(a.pkg_id) for a in pkgs))
        else:
            lines += [f'  one "{cmd}" {shlex.join(a.pkg_id.split())}' for a in pkgs]
        hint = " Install Homebrew from https://brew.sh first." if tool == "brew" else ""
        lines.append(f'else\n  echo "Skipping {len(pkgs)} {mgr} packages: {"paru or yay" if mgr == "aur" else tool} isn\'t installed.{hint}"; failed+=("all {mgr} packages")\nfi')
    man = " ".join(shlex.quote(a.name) for a in manual)
    body = "\n".join(lines)
    return f"""#!/usr/bin/env bash
# Parity restore script, made {result.scanned_at} on {result.computer_name}.
# Run it on the fresh install:  bash restore.sh
cd "$(dirname "$0")"
log=restore-log.txt
echo "Restore started $(date)" > "$log"
AUR=$(command -v paru || command -v yay || echo paru)
failed=()
have() {{ command -v "$1" >/dev/null 2>&1; }}
run() {{ echo "==> $*" | tee -a "$log"; "$@" 2>&1 | tee -a "$log"; return "${{PIPESTATUS[0]}}"; }}
# All at once first (fast); if that fails, one at a time so one bad package doesn't block the rest.
batch() {{ local cmd=$1; shift; run $cmd "$@" && return 0; local p; for p in "$@"; do run $cmd "$p" || failed+=("$p"); done; }}
one() {{ local cmd=$1; shift; run $cmd "$@" || failed+=("$*"); }}

{body}

manual=({man})
echo
if [ ${{#failed[@]}} -gt 0 ]; then
  echo "Check these (details in $log):"; printf '  - %s\\n' "${{failed[@]}}"
else
  echo "Everything installed without errors."
fi
if [ ${{#manual[@]}} -gt 0 ]; then
  echo; echo "Install these yourself (download links are in report.html):"; printf '  - %s\\n' "${{manual[@]}}"
fi
"""


def winget_json(result: ScanResult, winget: list[App]) -> dict:
    by_src: dict[str, list] = {}
    for a in winget:
        by_src.setdefault(a.manager or "winget", []).append({"PackageIdentifier": a.pkg_id})
    return {
        "$schema": "https://aka.ms/winget-packages.schema.2.0.json",
        "CreationDate": datetime.now().astimezone().isoformat(),
        "Sources": [{"Packages": pkgs, "SourceDetails": result.winget_sources.get(src) or DEFAULT_SOURCES.get(src, DEFAULT_SOURCES["winget"])}
                    for src, pkgs in by_src.items()],
        "WinGetVersion": "1.0",
    }


# ---------------------------------------------------------------- HTML report

CSS = """
:root{--bg:#fbfbfa;--fg:#1d1d1f;--muted:#5c5c63;--line:#dcdce0;--card:#fff;--accent:#0b57d0;--warn-bg:#fff4d6;--warn-fg:#4d3600}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececef;--muted:#a3a3ab;--line:#33333a;--card:#1c1c20;--accent:#8ab4f8;--warn-bg:#3a2f12;--warn-fg:#ffe3a3}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 "Segoe UI",system-ui,sans-serif}
.skip{position:absolute;left:-999px}.skip:focus{left:16px;top:8px;background:var(--card);padding:8px;z-index:1}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}h1{margin:0 0 4px;font-size:1.9rem}h2{margin:40px 0 8px;font-size:1.35rem}
h3{margin:24px 0 8px;font-size:1.05rem}.meta{color:var(--muted);margin:0}nav ul{display:flex;flex-wrap:wrap;gap:4px 16px;padding:0;list-style:none}
a{color:var(--accent)}:focus-visible{outline:3px solid var(--accent);outline-offset:2px}
.callout{background:var(--warn-bg);color:var(--warn-fg);border-radius:8px;padding:12px 16px;margin:16px 0}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card)}
table{border-collapse:collapse;width:100%;font-size:.94rem}caption{text-align:left;padding:10px 12px;font-weight:600}
th,td{text-align:left;padding:7px 12px;border-top:1px solid var(--line);vertical-align:top}th{font-weight:600;color:var(--muted)}
code{font-family:Consolas,ui-monospace,monospace;font-size:.88em}
.search{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;margin:12px 0}
.search input{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);min-width:min(320px,100%)}
details summary{cursor:pointer;margin:16px 0 8px;font-weight:600}
"""

JS = """
const q=document.getElementById('q'),out=document.getElementById('count');
q.addEventListener('input',()=>{const t=q.value.trim().toLowerCase();let n=0;
document.querySelectorAll('tr[data-s]').forEach(r=>{const m=!t||r.dataset.s.includes(t);r.hidden=!m;if(m)n++;});
out.textContent=t?n+(n===1?' program matches':' programs match'):'';});
"""


def _table(caption: str, headers: list[str], rows: list[list[str]], search: list[str] | None = None) -> str:
    """rows hold pre-escaped HTML cells; search (plain text per row) makes rows filterable."""
    head = "".join(f'<th scope="col">{escape(h)}</th>' for h in headers)
    body = "".join(
        (f'<tr data-s="{escape(search[i].casefold())}">' if search else "<tr>") + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
        for i, r in enumerate(rows))
    return f'<div class="wrap"><table><caption>{escape(caption)}</caption><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _hardware_html(result: ScanResult) -> str:
    out = []
    for s in result.hardware:
        if not s.items:
            continue
        keys = list(dict.fromkeys(k for i in s.items for k in i))
        if len(s.items) == 1:
            out.append(_table(s.title, ["Property", "Value"], [[escape(k), escape(str(s.items[0].get(k, "")))] for k in keys]))
        else:
            out.append(_table(s.title, keys, [[escape(str(i.get(k, ""))) for k in keys] for i in s.items]))
    return "\n".join(out)


def report_html(result: ScanResult, winget: list[App], manual: list[App], skipped: list[App]) -> str:
    e = escape
    disk = disk_summary(result)
    win = result.platform == "windows"
    script = "restore.ps1" if win else "restore.sh"
    run_it = "<code>restore.ps1</code>" if win else "<code>bash restore.sh</code>"
    configs = {"windows": "<code>%APPDATA%</code>", "linux": "<code>~/.config</code>, <code>~/.local/share</code> and your dotfiles",
               "macos": "<code>~/Library/Application Support</code>"}.get(result.platform, "your app settings folders")
    drivers = "".join(f'<li><a href="{e(u)}">{e(t)}</a></li>' for t, u in driver_links(result))
    driver_html = f"""<h2 id="drivers">Drivers to download</h2>
{f'<ul>{drivers}</ul>' if drivers else '<p>No board or GPU details were found.</p>'}
<p>Get these from the maker, not from Windows Update.</p>""" if win else ""
    warn = "".join(f'<p class="callout">{e(w)}</p>' for w in result.warnings)

    def find(a):
        return f'<a href="https://www.google.com/search?q={e(quote_plus(a.name + " download"))}">Find download<span class="skip"> for {e(a.name)}</span></a>'

    w_tbl = _table(f"Reinstalled by {script} ({len(winget)})", ["Program", "Version", "Package"],
                   [[e(a.name), e(a.version), f"<code>{e(a.manager)}: {e(a.pkg_id)}</code>"] for a in winget],
                   [f"{a.name} {a.publisher} {a.pkg_id}" for a in winget])
    m_tbl = _table(f"Install these yourself ({len(manual)})", ["Program", "Version", "Publisher", "Link"],
                   [[e(a.name), e(a.version), e(a.publisher), find(a)] for a in manual],
                   [f"{a.name} {a.publisher}" for a in manual])
    s_tbl = _table(f"Left out ({len(skipped)})", ["Program", "Type", "Version"],
                   [[e(a.name), e(a.category), e(a.version)] for a in skipped],
                   [f"{a.name} {a.publisher}" for a in skipped])

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Parity report: {e(result.computer_name)}</title><style>{CSS}</style></head><body>
<a class="skip" href="#main">Skip to content</a>
<main id="main">
<h1>Parity report: {e(result.computer_name)}</h1>
<p class="meta">{e(result.os_name)} · scanned {e(result.scanned_at)}</p>
<nav aria-label="Sections"><ul><li><a href="#before">Before you reinstall</a></li>{'<li><a href="#drivers">Drivers</a></li>' if win else ''}
<li><a href="#software">Software</a></li><li><a href="#hardware">Hardware</a></li></ul></nav>
{warn}
<h2 id="before">Before you reinstall</h2>
{f'<p class="callout">{e(disk)}</p>' if disk else ''}
<ol><li>Copy this whole folder off the drive you're wiping (a USB stick or another disk).</li>
<li>Save license keys and anything in {configs} you'll miss. They aren't in this report.</li>
<li>On the new install, run {run_it}, then work through "Install these yourself".</li></ol>
{driver_html}
<h2 id="software">Software</h2>
<div class="search"><label for="q">Filter programs</label><input id="q" type="search" autocomplete="off">
<span id="count" role="status" aria-live="polite"></span></div>
{w_tbl}
<p></p>
{m_tbl}
<details><summary>Left out ({len(skipped)})</summary>{s_tbl}</details>
<h2 id="hardware">Hardware</h2>
{_hardware_html(result)}
</main><script>{JS}</script></body></html>"""


def export_kit(result: ScanResult, folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    winget, manual, skipped = split_apps(result.apps)
    files = {
        "report.html": report_html(result, winget, manual, skipped),
        "hardware.json": json.dumps({s.title: s.items for s in result.hardware}, indent=2, default=str),
    }
    if result.platform == "windows":
        files["restore.ps1"] = restore_script(result, winget, manual)
        files["winget-packages.json"] = json.dumps(winget_json(result, winget), indent=2)
    else:
        files["restore.sh"] = restore_sh(result, winget, manual)
    written = []
    for name, text in files.items():
        p = folder / name
        # BOM so Windows PowerShell 5.1 reads non-ASCII program names correctly; plain \n so bash can read restore.sh.
        p.write_text(text, encoding="utf-8-sig" if name.endswith(".ps1") else "utf-8", newline="\n" if name.endswith(".sh") else None)
        if name.endswith(".sh") and os.name == "posix":
            p.chmod(0o755)
        written.append(p)
    csv_path = folder / "software.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:   # BOM so Excel shows accents right
        w = csv.writer(f)
        w.writerow(["Included", "Name", "Version", "Publisher", "Type", "Package manager", "Package", "Source"])
        for a in result.apps:
            w.writerow(["yes" if a.selected else "no", a.name, a.version, a.publisher, a.category, a.manager, a.pkg_id, a.source])
    written.append(csv_path)
    return written
