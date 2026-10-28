"""Sample data for `python main.py --demo`, so the UI can be tried away from Windows."""
from .models import App, HardwareSection, ScanResult
from .scanner import categorize


def demo_result() -> ScanResult:
    raw = [
        ("Mozilla Firefox (x64 en-US)", "131.0", "Mozilla", "Mozilla.Firefox"),
        ("Visual Studio Code", "1.94.2", "Microsoft Corporation", "Microsoft.VisualStudioCode"),
        ("Figma", "124.4.7", "Figma, Inc.", "Figma.Figma"),
        ("Steam", "2.10.91.91", "Valve Corporation", "Valve.Steam"),
        ("Discord", "1.0.9166", "Discord Inc.", "Discord.Discord"),
        ("7-Zip 24.08 (x64)", "24.08", "Igor Pavlov", "7zip.7zip"),
        ("NVDA", "2024.3.1", "NV Access", "NVAccess.NVDA"),
        ("Bambu Studio", "1.10.1.50", "Bambulab", "Bambulab.Bambustudio"),
        ("Python 3.12.7 (64-bit)", "3.12.7150.0", "Python Software Foundation", "Python.Python.3.12"),
        ("Colour Contrast Analyser", "3.5.4", "TPGi", ""),
        ("Logitech G HUB", "2024.8.6", "Logitech", ""),
        ("Some Licensed Plugin Pro", "4.2", "Small Vendor Ltd", ""),
        ("Microsoft Visual C++ 2015-2022 Redistributable (x64)", "14.40.33810.0", "Microsoft Corporation", ""),
        ("NVIDIA Graphics Driver 566.03", "566.03", "NVIDIA Corporation", ""),
        ("Realtek Audio Driver", "6.0.9600.1", "Realtek Semiconductor Corp.", ""),
        ("Microsoft Edge WebView2 Runtime", "130.0.2849.56", "Microsoft Corporation", ""),
    ]
    apps = [categorize(App(name=n, version=v, publisher=p, pkg_id=w, manager="winget" if w else ""))
            for n, v, p, w in raw]
    apps.append(categorize(App(name="WhatsApp", version="2.2440.9", source="winget",
                               pkg_id="9NKSQGP7F2NH", manager="msstore", category="Store app")))
    apps.sort(key=lambda a: a.name.casefold())
    hw = [
        HardwareSection("System", [{"Computer name": "DEMO-PC", "Manufacturer": "Micro-Star International",
                                    "Windows": "Microsoft Windows 11 Pro", "Version": "10.0.26100 (build 26100)",
                                    "Installed RAM (GB)": 31.9}]),
        HardwareSection("Motherboard", [{"Manufacturer": "Micro-Star International Co., Ltd.", "Model": "MAG B650 TOMAHAWK WIFI (MS-7D75)", "Revision": "1.0"}]),
        HardwareSection("CPU", [{"Name": "AMD Ryzen 7 7800X3D 8-Core Processor", "Cores": 8, "Threads": 16, "Max clock (MHz)": 4201, "Socket": "AM5"}]),
        HardwareSection("Memory", [{"Slot": f"DIMM {i}", "Manufacturer": "Kingston", "Part number": "KF560C36-16", "Size (GB)": 16, "Speed (MT/s)": 6000} for i in (1, 2)]),
        HardwareSection("Graphics", [{"Name": "NVIDIA GeForce RTX 4070 SUPER", "Driver version": "32.0.15.6603", "Driver date": "2024-10-17"}]),
        HardwareSection("Storage", [
            {"Disk": 0, "Model": "Samsung SSD 990 PRO 2TB", "Type": "SSD", "Bus": "NVMe", "Size (GB)": 1863, "Health": "Healthy", "Contains": "Windows (C:), EFI boot partition"},
            {"Disk": 1, "Model": "WD_BLACK SN770 1TB", "Type": "SSD", "Bus": "NVMe", "Size (GB)": 932, "Health": "Healthy", "Contains": "Linux partitions, EFI boot partition"},
        ]),
        HardwareSection("Network", [{"Name": "Ethernet", "Adapter": "Realtek Gaming 2.5GbE Family Controller", "Status": "Up", "Speed": "2.5 Gbps"}]),
    ]
    return ScanResult(apps=apps, hardware=hw, computer_name="DEMO-PC", os_name="Microsoft Windows 11 Pro 10.0.26100",
                      scanned_at="2026-10-27 14:30", warnings=[])
