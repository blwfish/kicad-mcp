#pragma once
// Fixture for terminal-only device card tests (v1).
// Devices under test: PIEZO (single ADC GPIO) + TCRT5000 (single digital out).
// ONLY the part names being tested appear in this file — naming any extra
// recognized part would feed the part extractor too (it scans comments) and
// could bleed into unrelated resolution assertions.
//
// No SWITCH card is exercised here to keep the fixture minimal; the SWITCH card
// is covered by unit tests in test_terminal_device_cards.py.

#if CONFIG_IDF_TARGET_ESP32
#define PIEZO_ADC_PIN        35   // Piezo transducer ADC line
#define TCRT5000_DOUT_PIN    34   // Single reflective optical sensor
#else
// Fallback (must be dead branch — excluded by #if selection above)
#define PIEZO_ADC_PIN        -1
#define TCRT5000_DOUT_PIN    -1
#endif
