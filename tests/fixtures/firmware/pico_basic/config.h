#pragma once
// Synthetic Raspberry Pi Pico firmware-spec fixture (Phase 2 MCU expansion).
// The same I2C + MCP23017 + HX711 shape as the canonical ESP32 fixture, retargeted
// to the RaspberryPi-Pico: pins are Pico GP numbers (all broken out on the module —
// GP0-22, GP26-28), the module self-regulates 3V3 (no external LDO), and there are
// no EN/BOOT straps. Proves the generalized wiring core resolves GPIO{n} symbol
// pins and sources +3V3 from the module's own 3V3 output pin.

// --- I2C (I2C0 default pins) ---
#define I2C_SDA       4
#define I2C_SCL       5
#define I2C_FREQ      400000   // not a pin (seam guard)

// --- MCP23017 GPIO expander ---
#define MCP23017_ADDR     0x27
#define MCP23017_INT_PIN  6
#define MCP_IODIRA        0x00 // a register, NOT an I2C address (seam guard)

// --- HX711 load-cell ADC ---
#define HX711_DOUT_PIN    2
#define HX711_SCK_PIN     3

// --- counts/config (seam guards: must NOT classify as pins) ---
#define NUM_SENSORS       6
#define MAX_SENSORS       16
