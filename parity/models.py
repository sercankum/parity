"""Plain data types shared by the scanner, exporter and UI."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class App:
    name: str
    version: str = ""
    publisher: str = ""
    source: str = "Registry"      # where it was found: Registry, Store, winget, a flatpak remote, /Applications…
    pkg_id: str = ""              # arguments for the manager's install command
    manager: str = ""             # winget, msstore, pacman, aur, apt, dnf, flatpak, snap, brew, cask, mas
    category: str = "App"         # App, Store app, Runtime, Driver, Update
    selected: bool = True

    @property
    def restore_label(self) -> str:
        return f"{self.manager}: {self.pkg_id}" if self.pkg_id else "Manual download"


@dataclass
class HardwareSection:
    title: str
    items: list[dict] = field(default_factory=list)


@dataclass
class ScanResult:
    apps: list[App]
    hardware: list[HardwareSection]
    computer_name: str
    os_name: str
    scanned_at: str
    platform: str = "windows"     # windows, linux, macos
    warnings: list[str] = field(default_factory=list)
    winget_sources: dict = field(default_factory=dict)   # source name -> SourceDetails from winget export

    def section(self, title: str) -> HardwareSection | None:
        return next((s for s in self.hardware if s.title == title), None)
