#ifndef CONFIG_H
#define CONFIG_H
// Expander-terminals integration fixture: ESP32-WROOM + HX711 + an MCP23017 I2C
// GPIO expander (U3). The board.yaml taps the MCP's GPA0..GPA5 out to 6 labeled
// TCRT5000 reflectance-sensor terminals — firmware can't know this (the sensors
// are addressed by MCP register at runtime, no per-sensor #define).
#define I2C_SDA_PIN    21
#define I2C_SCL_PIN    22
#define MCP23017_ADDR  0x27
#define HX711_DOUT_PIN 16
#define HX711_SCK_PIN  17
#endif
