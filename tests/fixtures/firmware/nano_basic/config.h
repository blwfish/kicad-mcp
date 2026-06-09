#pragma once
// Synthetic Arduino Nano v3 firmware-spec fixture (Phase 2b — first 5V MCU). An
// HX711 load-cell ADC on digital pins, plus an LED orphan — all on broken-out Nano
// digital pins (D0-D13). Proves the generalized core resolves the Nano's D{n} symbol
// pins AND that a 5V board ties its parts to +5V (sourced from the module's own +5V
// pin), not the ESP32/Pico +3V3 rail. Digital-pin peripherals only: the Nano's I2C
// is on analog A4/A5, referenced as "A0"-style names firmware can't yet express as
// integer pins (a known limitation — see arduino-nano-v3.yaml).

// --- HX711 load-cell ADC (bit-banged on any two digital pins) ---
#define HX711_DOUT_PIN    2
#define HX711_SCK_PIN     3

// --- status LED (orphan: no declared peripheral) ---
#define LED_PIN           13

// --- analog sensor input: an Arduino A-pin (resolves to the symbol's A0 pin,
//     not a GPIO number). Was silently dropped before analog-pin support. ---
#define LDR_PIN           A0

// --- counts/config (seam guards: must NOT classify as pins) ---
#define SAMPLE_RATE       80
#define NUM_READINGS      10
