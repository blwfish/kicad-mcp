#ifndef CONFIG_H
#define CONFIG_H

#include <Arduino.h>

// ===== VERSION INFORMATION =====
#define FIRMWARE_VERSION_BASE "0.5.0"

#ifndef BUILD_GIT_HASH
#define BUILD_GIT_HASH "dev"
#endif
#ifndef BUILD_TIME
#define BUILD_TIME __DATE__ " " __TIME__
#endif

#define FIRMWARE_VERSION FIRMWARE_VERSION_BASE "-" BUILD_GIT_HASH

// ===== I2C CONFIGURATION =====
// ESP32-WROOM default I2C pins (differs from ESP32-C3 which uses GPIO 8/9)
#define I2C_SDA_PIN         21
#define I2C_SCL_PIN         22
#define I2C_CLOCK_HZ        400000   // 400 kHz Fast Mode

// ===== I2C DEVICE ADDRESSES =====
#define MPU6050_ADDR        0x68     // IMU #1 (AD0=LOW, required)
#define MPU6050_ADDR_2      0x69     // IMU #2 (AD0=HIGH, optional)
#define OLED_ADDR           0x3C
// #define INA219_ADDR      0x40     // Phase 3: track power monitor

// ===== MPU-6050 REGISTERS =====
#define MPU6050_REG_SMPLRT_DIV      0x19
#define MPU6050_REG_CONFIG          0x1A
#define MPU6050_REG_GYRO_CONFIG     0x1B
#define MPU6050_REG_ACCEL_CONFIG    0x1C
#define MPU6050_REG_ACCEL_XOUT_H   0x3B   // Start of 14-byte burst read
#define MPU6050_REG_TEMP_OUT_H      0x41
#define MPU6050_REG_PWR_MGMT_1     0x6B
#define MPU6050_REG_WHO_AM_I       0x75

// MPU-6050 configuration values
#define MPU6050_ACCEL_RANGE_2G      0x00   // +-2g  (16384 LSB/g)
#define MPU6050_GYRO_RANGE_250      0x00   // +-250 deg/s (131 LSB/deg/s)
#define MPU6050_DLPF_20HZ           0x04   // ~20 Hz bandwidth, ~8.5ms delay
#define MPU6050_SMPLRT_DIV_100HZ    0x09   // 1000/(9+1) = 100 Hz
#define MPU6050_CLOCK_PLL_XGYRO     0x01   // PLL with X-axis gyro reference
#define MPU6050_WHO_AM_I_EXPECTED   0x68

// Conversion factors
#define ACCEL_LSB_PER_G     16384.0f
#define GYRO_LSB_PER_DPS    131.0f
#define TEMP_LSB_PER_DEG    340.0f
#define TEMP_OFFSET_DEG     36.53f

// ===== MQTT CONFIGURATION =====
#define MQTT_DEFAULT_PREFIX     "/cova"
#define MQTT_DEFAULT_NAME       "geometry-car"
#define MQTT_NVS_NAMESPACE      "mqtt"
#define MQTT_PORT               1883
#define MQTT_BUFFER_SIZE        2048
#define MQTT_HEALTH_INTERVAL_MS 300000UL  // 5 minutes

// ===== WIFI CONFIGURATION =====
#define WIFI_AP_SSID        "GeometryCar"
#define WIFI_AP_CHANNEL     1
#define WIFI_AP_MAX_CONN    4
#define WIFI_STA_CONNECT_TIMEOUT_MS  10000   // 10s to connect in STA mode
#define WIFI_NVS_NAMESPACE  "wifi"
#define WIFI_NVS_KEY_SSID   "ssid"
#define WIFI_NVS_KEY_PASS   "pass"

// ===== WEBSOCKET CONFIGURATION =====
#define WS_PATH             "/ws"
#define WS_SEND_INTERVAL_MS     100     // 100 ms = 10 Hz raw sample batches
#define WS_SUMMARY_INTERVAL_MS  1000    // 1000 ms = 1 Hz summary frames
#define WS_CLEANUP_INTERVAL_MS  2000    // 2 sec = cleanup dead connections
#define WS_SAMPLE_BATCH_SIZE    10      // Samples per WebSocket frame

// WebSocket frame types (server -> client)
#define WS_FRAME_RAW_SAMPLES    0x01
#define WS_FRAME_SUMMARY        0x02
#define WS_FRAME_REC_STATUS     0x03    // Recording state notification

// WebSocket command types (client -> server)
#define WS_CMD_START_RECORDING  0x10
#define WS_CMD_STOP_RECORDING   0x11
#define WS_CMD_CALIBRATE        0x12

// Wire format: 30 bytes per sample (no struct padding)
// IMU1: timestamp(4) + accel(6) + gyro(6) + temp(2) = 18
// IMU2: accel(6) + gyro(6) = 12  (no timestamp, no temp)
#define WS_SAMPLE_WIRE_SIZE     30

// ===== DUAL-CORE IMU TASK (ESP32 only, not C3/single-core) =====
#define IMU_TASK_STACK_SIZE     4096
#define IMU_TASK_PRIORITY       5       // Above Arduino loop (1), below WiFi (23)
#define IMU_TASK_CORE           0
#define I2C_MUTEX_TIMEOUT_MS    50      // Max wait for I2C bus

// ===== TIMING CONSTANTS =====
#define IMU_SAMPLE_INTERVAL_US      10000   // 10 ms = 100 Hz
#define OLED_UPDATE_INTERVAL_MS     200     // 200 ms = 5 Hz
#define SERIAL_OUTPUT_INTERVAL_MS   100     // 100 ms = 10 Hz
#define STATS_REPORT_INTERVAL_MS    10000   // 10 sec = sample rate reporting
#define SUMMARY_INTERVAL_MS         1000    // 1 sec = 1 Hz summary computation

// ===== RING BUFFER =====
#define RING_BUFFER_SIZE    1024            // 1024 samples = 10.24 sec at 100Hz

// ===== DATA STRUCTURES =====

struct imu_sample_t {
    uint32_t timestamp_ms;
    // IMU #1 (front truck, 0x68)
    int16_t accel_x;
    int16_t accel_y;
    int16_t accel_z;
    int16_t gyro_x;
    int16_t gyro_y;
    int16_t gyro_z;
    int16_t temperature;
    // IMU #2 (rear truck, 0x69) — zero-filled when single IMU
    int16_t accel_x2;
    int16_t accel_y2;
    int16_t accel_z2;
    int16_t gyro_x2;
    int16_t gyro_y2;
    int16_t gyro_z2;
};

struct summary_1s_t {
    uint32_t timestamp_ms;
    // IMU #1
    float accel_x_rms;
    float accel_y_rms;
    float accel_z_rms;
    float accel_x_peak;
    float accel_y_peak;
    float accel_z_peak;
    float gyro_x_rms;
    float gyro_y_rms;
    float gyro_z_rms;
    float gyro_x_mean;
    float gyro_y_mean;
    float gyro_z_mean;
    float temperature;
    uint32_t sample_count;
    float sample_rate;
    // IMU #2 — zero when single IMU
    float accel_x2_rms;
    float accel_y2_rms;
    float accel_z2_rms;
    float accel_x2_peak;
    float accel_y2_peak;
    float accel_z2_peak;
    float gyro_x2_rms;
    float gyro_y2_rms;
    float gyro_z2_rms;
    float gyro_x2_mean;
    float gyro_y2_mean;
    float gyro_z2_mean;
    // Accel means (for grade/tilt computation) — appended to preserve layout
    float accel_x_mean;      // IMU #1 longitudinal (grade)
    float accel_y_mean;      // IMU #1 lateral (superelevation)
    float accel_z_mean;      // IMU #1 vertical (near 1.0g on level)
};

// ===== FLASH LOGGING =====
#define SURVEY_DIR              "/surveys"
#define SURVEY_HEADER_SIZE      64
#define SURVEY_SAMPLE_SIZE      30      // Same as WS_SAMPLE_WIRE_SIZE (no padding)
#define SURVEY_MAGIC            "GEOM"
#define SURVEY_VERSION_SINGLE   1       // Version 1: single IMU (18 bytes/sample)
#define SURVEY_VERSION_DUAL     2       // Version 2: dual IMU (30 bytes/sample)
#define FLASH_MIN_FREE_BYTES    32768   // Reserve 32KB for web UI + overhead

struct __attribute__((packed)) survey_header_t {
    char     magic[4];           // "GEOM"
    uint8_t  version;            // SURVEY_VERSION_SINGLE or _DUAL
    uint8_t  sample_size;        // SURVEY_SAMPLE_SIZE (30)
    uint16_t sample_rate_hz;     // 100
    uint8_t  accel_range_g;      // 2 (±2g)
    uint8_t  gyro_range_dps;     // 250 (stored as uint8_t, 250 fits)
    uint8_t  imu_count;          // 1 or 2
    uint8_t  reserved1[1];
    uint32_t start_time_ms;      // millis() at recording start
    char     car_id[16];         // "GeometryCar\0..."
    uint8_t  reserved2[32];      // Pad to 64 bytes total
};

// ===== TRUCK CONFIGURATION =====
// Default values for standard Bettendorf trucks on 55' flat car.
// Stored in NVS, overridable from browser Car Setup UI.
#define TRUCK_NVS_NAMESPACE     "truck"
#define TRUCK_NVS_KEY_AXLES     "axles"     // uint8_t: axles per truck
#define TRUCK_NVS_KEY_AXLE_SP   "axle_sp"   // float: axle spacing in mm
#define TRUCK_NVS_KEY_TRUCK_SP  "truck_sp"  // float: truck spacing in mm

#define TRUCK_DEFAULT_AXLES       2       // 2 axles per truck (standard)
#define TRUCK_DEFAULT_AXLE_SP     16.5f   // mm (~4.7' prototype in HO)
#define TRUCK_DEFAULT_TRUCK_SP    54.0f   // mm (bolster-to-bolster, 55' flat car)

// ===== BUZZER CONFIGURATION =====
// Audible speed indicator: blips to guide hand-push survey speed
#define BUZZER_PIN              25      // GPIO 25 (DAC1 + LEDC capable)
#define BUZZER_LEDC_CHANNEL     0       // LEDC channel (0-15, no conflict with U8g2)
#define BUZZER_LEDC_RESOLUTION  8       // 8-bit duty resolution

// Tone frequencies (Hz)
#define BUZZER_FREQ_SLOW        800     // Below target speed — low tone
#define BUZZER_FREQ_FAST        2400    // Above target speed — high tone

// Speed tolerance
#define BUZZER_SPEED_DEADBAND   1.0f    // +/- 1 scale mph = silent (on target)
#define BUZZER_SPEED_MAX_ERR    5.0f    // Error clamp for blip rate scaling

// Blip timing (milliseconds)
#define BUZZER_BLIP_DURATION_MS 40      // Length of each tone blip
#define BUZZER_MIN_INTERVAL_MS  100     // Fastest blip rate (far from target)
#define BUZZER_MAX_INTERVAL_MS  1000    // Slowest blip rate (near target)

// Motion detection threshold
#define BUZZER_MOTION_AZ_RMS_MIN  1.005f  // Below this = stationary, buzzer silent

// Speed estimation (method 0: azRms proxy)
#define BUZZER_AZRMS_STATIONARY   1.005f  // Below = 0 mph
#define BUZZER_AZRMS_MAX_SPEED    1.05f   // Maps to ~20 mph
#define BUZZER_AZRMS_SPEED_RANGE  20.0f   // Scale mph at AZRMS_MAX_SPEED

// Speed estimation (method 1: leaky accel integration)
#define BUZZER_ACCEL_DECAY        0.8f    // Per-second velocity decay factor
#define BUZZER_GRAVITY_MMS2       9810.0f // mm/s^2

// NVS storage
#define BUZZER_NVS_NAMESPACE    "buzzer"
#define BUZZER_NVS_KEY_ENABLED  "enabled"   // bool
#define BUZZER_NVS_KEY_SPEED    "speed"     // float: target speed in scale mph
#define BUZZER_NVS_KEY_MUTE     "mute"      // bool
#define BUZZER_NVS_KEY_METHOD   "method"    // uint8_t: 0=azRms, 1=accel integration

// WebSocket command
#define WS_CMD_BUZZER_MUTE      0x13

// HO scale speed conversion: mph * 447.04 / 87.0 = model mm/s
#define SCALE_MPH_TO_MODEL_MMS  (447.04f / 87.0f)

#endif // CONFIG_H
