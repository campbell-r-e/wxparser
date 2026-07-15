"""SAME/EAS code lookups (originators + event codes). Small, stable, bundled."""

from __future__ import annotations

# Originator codes
ORIGINATORS: dict[str, str] = {
    "EAS": "Emergency Alert System",
    "CIV": "Civil authorities",
    "WXR": "National Weather Service",
    "PEP": "Primary Entry Point (national)",
    "EAN": "Emergency Action Notification",
}

# Event codes -> human label. Covers the NWS/EAS set commonly seen on NWR.
EVENT_CODES: dict[str, str] = {
    # National
    "EAN": "Emergency Action Notification",
    "EAT": "Emergency Action Termination",
    "NIC": "National Information Center",
    "NPT": "National Periodic Test",
    "RMT": "Required Monthly Test",
    "RWT": "Required Weekly Test",
    "NMN": "Network Message Notification",
    # Weather — warnings
    "TOR": "Tornado Warning",
    "SVR": "Severe Thunderstorm Warning",
    "FFW": "Flash Flood Warning",
    "FLW": "Flood Warning",
    "SMW": "Special Marine Warning",
    "SQW": "Snow Squall Warning",
    "EWW": "Extreme Wind Warning",
    "BZW": "Blizzard Warning",
    "IBW": "Ice Storm Warning",
    "WSW": "Winter Storm Warning",
    "HWW": "High Wind Warning",
    "HUW": "Hurricane Warning",
    "TRW": "Tropical Storm Warning",
    "SSW": "Storm Surge Warning",
    "TSW": "Tsunami Warning",
    "DSW": "Dust Storm Warning",
    "FRW": "Fire Warning",
    "AVW": "Avalanche Warning",
    "CFW": "Coastal Flood Warning",
    "CDW": "Civil Danger Warning",
    "LEW": "Law Enforcement Warning",
    "HMW": "Hazardous Materials Warning",
    "NUW": "Nuclear Power Plant Warning",
    "RHW": "Radiological Hazard Warning",
    "SPW": "Shelter In Place Warning",
    "VOW": "Volcano Warning",
    "EQW": "Earthquake Warning",
    # Weather — watches
    "TOA": "Tornado Watch",
    "SVA": "Severe Thunderstorm Watch",
    "FFA": "Flash Flood Watch",
    "FLA": "Flood Watch",
    "BZA": "Blizzard Watch",
    "WSA": "Winter Storm Watch",
    "HWA": "High Wind Watch",
    "HUA": "Hurricane Watch",
    "TRA": "Tropical Storm Watch",
    "SSA": "Storm Surge Watch",
    "TSA": "Tsunami Watch",
    "CFA": "Coastal Flood Watch",
    "AVA": "Avalanche Watch",
    # Statements / advisories / admin
    "SVS": "Severe Weather Statement",
    "SPS": "Special Weather Statement",
    "FFS": "Flash Flood Statement",
    "FLS": "Flood Statement",
    "HLS": "Hurricane Statement",
    "WSV": "Winter Weather Statement",
    "EVI": "Evacuation Immediate",
    "CEM": "Civil Emergency Message",
    "LAE": "Local Area Emergency",
    "ADR": "Administrative Message",
    "BLU": "Blue Alert",
    "CAE": "Child Abduction Emergency",
    "DMO": "Practice/Demo Warning",
}


def originator_label(code: str) -> str:
    return ORIGINATORS.get(code.upper(), code)


def event_label(code: str) -> str:
    return EVENT_CODES.get(code.upper(), code)
