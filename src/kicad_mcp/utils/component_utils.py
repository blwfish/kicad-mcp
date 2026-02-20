"""
Utility functions for working with KiCad component values and properties.
"""
import re
from typing import Any, Dict, Optional, Tuple


def extract_voltage_from_regulator(value: str) -> str:
    """Extract output voltage from a voltage regulator part number or description.

    Args:
        value: Regulator part number or description

    Returns:
        Extracted voltage as a string or "unknown" if not found
    """
    # 78xx/79xx series
    match = re.search(r"78(\d\d)|79(\d\d)", value, re.IGNORECASE)
    if match:
        group = match.group(1) or match.group(2)
        try:
            voltage = int(group)
            if voltage < 50:
                return f"{voltage}V"
        except ValueError:
            pass

    # Look for common voltage indicators
    voltage_patterns = [
        r"(\d+\.?\d*)V",
        r"-(\d+\.?\d*)V",
        r"(\d+\.?\d*)[_-]?V",
        r"[_-](\d+\.?\d*)",
    ]

    for pattern in voltage_patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            try:
                voltage = float(match.group(1))
                if 0 < voltage < 50:
                    if voltage.is_integer():
                        return f"{int(voltage)}V"
                    else:
                        return f"{voltage}V"
            except ValueError:
                pass

    # Check for common fixed voltage regulators
    regulators = {
        "LM7805": "5V",
        "LM7809": "9V",
        "LM7812": "12V",
        "LM7905": "-5V",
        "LM7912": "-12V",
        "LM1117-3.3": "3.3V",
        "LM1117-5": "5V",
        "LM317": "Adjustable",
        "LM337": "Adjustable (Negative)",
        "AP1117-3.3": "3.3V",
        "AMS1117-3.3": "3.3V",
        "L7805": "5V",
        "L7812": "12V",
        "MCP1700-3.3": "3.3V",
        "MCP1700-5.0": "5V",
    }

    for reg, volt in regulators.items():
        if re.search(re.escape(reg), value, re.IGNORECASE):
            return volt

    return "unknown"


def extract_frequency_from_value(value: str) -> str:
    """Extract frequency information from a component value or description.

    Args:
        value: Component value or description (e.g., "16MHz", "Crystal 8MHz")

    Returns:
        Frequency as a string or "unknown" if not found
    """
    frequency_patterns = [
        r"(\d+\.?\d*)[\s-]*([kKmMgG]?)[hH][zZ]",
        r"(\d+\.?\d*)[\s-]*([kKmMgG])",
    ]

    for pattern in frequency_patterns:
        match = re.search(pattern, value, re.IGNORECASE)
        if match:
            try:
                freq = float(match.group(1))
                unit = match.group(2).upper() if match.group(2) else ""

                if freq > 0:
                    if unit == "K":
                        if freq >= 1000:
                            return f"{freq / 1000:.3f}MHz"
                        else:
                            return f"{freq:.3f}kHz"
                    elif unit == "M":
                        if freq >= 1000:
                            return f"{freq / 1000:.3f}GHz"
                        else:
                            return f"{freq:.3f}MHz"
                    elif unit == "G":
                        return f"{freq:.3f}GHz"
                    else:
                        if freq < 1000:
                            return f"{freq:.3f}Hz"
                        elif freq < 1000000:
                            return f"{freq / 1000:.3f}kHz"
                        elif freq < 1000000000:
                            return f"{freq / 1000000:.3f}MHz"
                        else:
                            return f"{freq / 1000000000:.3f}GHz"
            except ValueError:
                pass

    # Check for common crystal frequencies
    if "32.768" in value or "32768" in value:
        return "32.768kHz"
    elif "16M" in value or "16MHZ" in value.upper():
        return "16MHz"
    elif "8M" in value or "8MHZ" in value.upper():
        return "8MHz"
    elif "20M" in value or "20MHZ" in value.upper():
        return "20MHz"
    elif "27M" in value or "27MHZ" in value.upper():
        return "27MHz"
    elif "25M" in value or "25MHZ" in value.upper():
        return "25MHz"

    return "unknown"


def extract_resistance_value(value: str) -> Tuple[Optional[float], Optional[str]]:
    """Extract resistance value and unit from component value.

    Args:
        value: Resistance value (e.g., "10k", "4.7k", "100")

    Returns:
        Tuple of (numeric value, unit) or (None, None) if parsing fails
    """
    match = re.search(r"(\d+\.?\d*)([kKmMrR\u03a9]?)", value)
    if match:
        try:
            resistance = float(match.group(1))
            unit = match.group(2).upper() if match.group(2) else "\u03a9"

            if unit == "R" or unit == "":
                unit = "\u03a9"

            return resistance, unit
        except ValueError:
            pass

    # Handle "4k7" (means 4.7k)
    match = re.search(r"(\d+)[kKmM](\d+)", value)
    if match:
        try:
            value1 = int(match.group(1))
            value2 = int(match.group(2))
            resistance = float(f"{value1}.{value2}")
            unit = (
                "k"
                if "k" in value.lower()
                else "M" if "m" in value.lower() else "\u03a9"
            )
            return resistance, unit
        except ValueError:
            pass

    return None, None


def extract_capacitance_value(value: str) -> Tuple[Optional[float], Optional[str]]:
    """Extract capacitance value and unit from component value.

    Args:
        value: Capacitance value (e.g., "10uF", "4.7nF", "100pF")

    Returns:
        Tuple of (numeric value, unit) or (None, None) if parsing fails
    """
    match = re.search(r"(\d+\.?\d*)([pPnNuU\u03bcF]+)", value)
    if match:
        try:
            capacitance = float(match.group(1))
            unit = match.group(2).lower()

            if "p" in unit or "pf" in unit:
                unit = "pF"
            elif "n" in unit or "nf" in unit:
                unit = "nF"
            elif "u" in unit or "\u03bc" in unit or "uf" in unit or "\u03bcf" in unit:
                unit = "\u03bcF"
            else:
                unit = "F"

            return capacitance, unit
        except ValueError:
            pass

    # Handle "4n7" (means 4.7nF)
    match = re.search(r"(\d+)[pPnNuU\u03bc](\d+)", value)
    if match:
        try:
            value1 = int(match.group(1))
            value2 = int(match.group(2))
            capacitance = float(f"{value1}.{value2}")

            if "p" in value.lower():
                unit = "pF"
            elif "n" in value.lower():
                unit = "nF"
            elif "u" in value.lower() or "\u03bc" in value:
                unit = "\u03bcF"
            else:
                unit = "F"

            return capacitance, unit
        except ValueError:
            pass

    return None, None


def extract_inductance_value(value: str) -> Tuple[Optional[float], Optional[str]]:
    """Extract inductance value and unit from component value.

    Args:
        value: Inductance value (e.g., "10uH", "4.7nH", "100mH")

    Returns:
        Tuple of (numeric value, unit) or (None, None) if parsing fails
    """
    match = re.search(r"(\d+\.?\d*)([pPnNuU\u03bcmM][hH])", value)
    if match:
        try:
            inductance = float(match.group(1))
            unit = match.group(2).lower()

            if "p" in unit:
                unit = "pH"
            elif "n" in unit:
                unit = "nH"
            elif "u" in unit or "\u03bc" in unit:
                unit = "\u03bcH"
            elif "m" in unit:
                unit = "mH"
            else:
                unit = "H"

            return inductance, unit
        except ValueError:
            pass

    # Handle "4u7" (means 4.7uH)
    match = re.search(r"(\d+)[pPnNuU\u03bcmM](\d+)[hH]", value)
    if match:
        try:
            value1 = int(match.group(1))
            value2 = int(match.group(2))
            inductance = float(f"{value1}.{value2}")

            if "p" in value.lower():
                unit = "pH"
            elif "n" in value.lower():
                unit = "nH"
            elif "u" in value.lower() or "\u03bc" in value:
                unit = "\u03bcH"
            elif "m" in value.lower():
                unit = "mH"
            else:
                unit = "H"

            return inductance, unit
        except ValueError:
            pass

    return None, None


def format_resistance(resistance: float, unit: str) -> str:
    """Format resistance value with appropriate unit."""
    if unit == "\u03a9":
        return f"{resistance:.0f}\u03a9" if resistance.is_integer() else f"{resistance}\u03a9"
    elif unit == "k":
        return f"{resistance:.0f}k\u03a9" if resistance.is_integer() else f"{resistance}k\u03a9"
    elif unit == "M":
        return f"{resistance:.0f}M\u03a9" if resistance.is_integer() else f"{resistance}M\u03a9"
    else:
        return f"{resistance}{unit}"


def format_capacitance(capacitance: float, unit: str) -> str:
    """Format capacitance value with appropriate unit."""
    if capacitance.is_integer():
        return f"{int(capacitance)}{unit}"
    else:
        return f"{capacitance}{unit}"


def format_inductance(inductance: float, unit: str) -> str:
    """Format inductance value with appropriate unit."""
    if inductance.is_integer():
        return f"{int(inductance)}{unit}"
    else:
        return f"{inductance}{unit}"


def normalize_component_value(value: str, component_type: str) -> str:
    """Normalize a component value string based on component type."""
    if component_type == "R":
        resistance, unit = extract_resistance_value(value)
        if resistance is not None and unit is not None:
            return format_resistance(resistance, unit)
    elif component_type == "C":
        capacitance, unit = extract_capacitance_value(value)
        if capacitance is not None and unit is not None:
            return format_capacitance(capacitance, unit)
    elif component_type == "L":
        inductance, unit = extract_inductance_value(value)
        if inductance is not None and unit is not None:
            return format_inductance(inductance, unit)

    return value


def get_component_type_from_reference(reference: str) -> str:
    """Determine component type from reference designator."""
    match = re.match(r"^([A-Za-z_]+)", reference)
    if match:
        return match.group(1)
    return ""


def is_power_component(component: Dict[str, Any]) -> bool:
    """Check if a component is likely a power-related component."""
    ref = component.get("reference", "")
    value = component.get("value", "").upper()
    lib_id = component.get("lib_id", "").upper()

    if ref.startswith(("VR", "PS", "REG")):
        return True

    power_terms = [
        "VCC", "VDD", "GND", "POWER", "PWR", "SUPPLY", "REGULATOR", "LDO",
    ]
    if any(term in value or term in lib_id for term in power_terms):
        return True

    regulator_patterns = [
        r"78\d\d",
        r"79\d\d",
        r"LM\d{3}",
        r"LM\d{4}",
        r"AMS\d{4}",
        r"MCP\d{4}",
    ]

    if any(re.search(pattern, value, re.IGNORECASE) for pattern in regulator_patterns):
        return True

    return False
