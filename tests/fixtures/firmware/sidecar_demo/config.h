#ifndef CONFIG_H
#define CONFIG_H
// Minimal board for the board.yaml sidecar golden test: ESP32-WROOM + one I2C
// IMU. The board.yaml adds an external power connector firmware can't declare.
#define I2C_SDA_PIN   21
#define I2C_SCL_PIN   22
#define MPU6050_ADDR  0x68
#endif
