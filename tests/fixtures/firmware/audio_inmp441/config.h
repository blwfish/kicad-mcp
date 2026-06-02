#pragma once
// Part-resolution test fixture: the audio-node config (identical pins to
// audio_s3) whose I2S microphone bus NAMES its part in a comment. The ONLY part
// named here is the mic itself — naming any other part in a comment would (
// correctly) feed the resolver too. See test_part_resolution_inmp441.py for what
// this exercises.

#if CONFIG_IDF_TARGET_ESP32S3
// --- I2S output bus 0 (stereo amp pair) ---
#define CMCA_I2S_BCLK         15
#define CMCA_I2S_LRC          16
#define CMCA_I2S_DIN          17
#define CMCA_I2S_SAMPLE_RATE  22050   // config, NOT a pin (seam guard)
// --- I2S output bus 1 ---
#define CMCA_I2S2_BCLK        35
#define CMCA_I2S2_LRC         36
#define CMCA_I2S2_DIN         37
// --- I2C OLED ---
#define CMCA_OLED_SDA         47
#define CMCA_OLED_SCL         48
#define CMCA_OLED_ADDR        0x3C
// --- UART presence sensor (LD2410) ---
#define CMCA_PRESENCE_RX_PIN  18
#define CMCA_PRESENCE_TX_PIN  21
// --- I2S microphone: INMP441 ---
#define CMCA_MIC_BCLK         8
#define CMCA_MIC_WS           9
#define CMCA_MIC_SD           10
#else
// legacy WROOM-32 prototype block (must be dropped by #if selection)
#define CMCA_I2S_BCLK         26
#define CMCA_I2S_LRC          25
#define CMCA_I2S_DIN          27
#endif
