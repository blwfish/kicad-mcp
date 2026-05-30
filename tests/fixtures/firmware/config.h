#pragma once
// Synthetic firmware-spec fixture for the firmware->PCB integration harness.
// A representative ESP32 board: I2C bus with an MCP23017 GPIO expander (0x27)
// and an HX711 load-cell ADC, plus a few orphan signals and deliberate
// seam-guard non-pin macros. Committed so the harness needs no external tree.

// --- I2C ---
#define I2C_SDA       21
#define I2C_SCL       22
#define I2C_FREQ      400000   // not a pin (seam guard)

// --- MCP23017 GPIO expander ---
#define MCP23017_ADDR     0x27
#define MCP23017_INT_PIN  13
#define MCP_IODIRA        0x00 // a register, NOT an I2C address (seam guard)

// --- HX711 load-cell ADC ---
#define HX711_DOUT_PIN    16
#define HX711_SCK_PIN     17

// --- orphan signals (no declared peripheral) ---
#define I2S_SCK_PIN       18
#define I2S_WS_PIN        19
#define I2S_SD_PIN        23
#define PIEZO_ADC_PIN     35

// --- counts/config in GPIO range (seam guards: must NOT classify as pins) ---
#define NUM_SENSORS       6
#define MAX_SENSORS       16
