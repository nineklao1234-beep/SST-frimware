#include <Arduino.h>
#include <SPI.h>

// =========================================================================
// HARDWARE PIN DEFINITIONS (ESP32 <-> SST26VF016B)
// =========================================================================
#define PIN_CS    5
#define PIN_SCK   18
#define PIN_MISO  19
#define PIN_MOSI  23
#define PIN_WP    4   // Write Protect (High = Normal Mode)
#define PIN_HOLD  2   // Hold (High = Normal Mode)

// Flash Memory Specification (SST26VF016B: 2MB / 16Mbit)
#define FLASH_SIZE_BYTES  0x200000 // 2,097,152 Bytes
#define SECTOR_SIZE_BYTES 4096     // 4 KB
#define PAGE_SIZE_BYTES   256      // 256 Bytes
#define BLOCK_BUF_SIZE    4096     // High-Speed Block Buffer (4KB)

// Global SPI Settings
uint32_t spi_freq = 10000000; // Default 10 MHz
SPISettings spiSettings(spi_freq, MSBFIRST, SPI_MODE0);

// Global Static RAM Buffers
static uint8_t rx_buf[BLOCK_BUF_SIZE];
static uint8_t tx_buf[BLOCK_BUF_SIZE];

// =========================================================================
// SST26VF016B SPI INSTRUCTION CODES
// =========================================================================
#define CMD_READ_ID       0x9F
#define CMD_READ_STATUS   0x05
#define CMD_READ_CONFIG   0x35
#define CMD_WRITE_ENABLE  0x06
#define CMD_GLOBAL_UNLOCK 0x98
#define CMD_SECTOR_ERASE  0x20
#define CMD_CHIP_ERASE    0xC7
#define CMD_PAGE_PROGRAM  0x02
#define CMD_READ_DATA     0x03

// =========================================================================
// FUNCTION PROTOTYPES (FORWARD DECLARATIONS FOR PLATFORMIO)
// =========================================================================
void updateSpiSettings(uint32_t freq_hz);
uint8_t readStatusRegister();
uint8_t readConfigRegister();
void writeEnable();
bool waitBusyWithProgress(uint32_t timeout_ms, uint32_t total_expected_ms = 0);
bool unlockProtection();
bool eraseSector(uint32_t raw_addr);
bool eraseChip();
uint8_t getPatternByte(uint32_t offset, uint8_t pat_type, uint8_t custom_hex);
bool writePage(uint32_t addr, uint8_t *data, uint16_t len);
bool writePatternRange(uint32_t start_addr, uint32_t len, uint8_t pat_type, uint8_t custom_hex);
bool verifyPatternRange(uint32_t start_addr, uint32_t len, uint8_t pat_type, uint8_t custom_hex);
void runAllPatternSweep(bool is_loop);
void readDataHex(uint32_t start_addr, uint32_t len);

// =========================================================================
// HELPER & LOW-LEVEL FUNCTIONS
// =========================================================================

void updateSpiSettings(uint32_t freq_hz) {
    spi_freq = freq_hz;
    spiSettings = SPISettings(spi_freq, MSBFIRST, SPI_MODE0);
}

uint8_t readStatusRegister() {
    SPI.beginTransaction(spiSettings);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(CMD_READ_STATUS);
    uint8_t stat = SPI.transfer(0x00);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
    return stat;
}

uint8_t readConfigRegister() {
    SPI.beginTransaction(spiSettings);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(CMD_READ_CONFIG);
    uint8_t cfg = SPI.transfer(0x00);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
    return cfg;
}

void writeEnable() {
    SPI.beginTransaction(spiSettings);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(CMD_WRITE_ENABLE);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
}

bool waitBusyWithProgress(uint32_t timeout_ms, uint32_t total_expected_ms) {
    uint32_t start_time = millis();
    int last_pct = -1;

    while (readStatusRegister() & 0x01) {
        uint32_t elapsed = millis() - start_time;

        if (elapsed > timeout_ms) {
            Serial.println("ERR:TIMEOUT");
            return false;
        }

        if (Serial.available()) {
            String cmd = Serial.readStringUntil('\n');
            cmd.trim();
            if (cmd == "STOP") {
                Serial.println("ABORTED");
                return false;
            }
        }

        if (total_expected_ms > 0) {
            int pct = (elapsed * 100) / total_expected_ms;
            if (pct > 99) pct = 99;
            if (pct != last_pct) {
                Serial.printf("PROGRESS:%d\n", pct);
                last_pct = pct;
            }
        }

        delay(1);
    }
    return true;
}

bool unlockProtection() {
    writeEnable();
    SPI.beginTransaction(spiSettings);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(CMD_GLOBAL_UNLOCK);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();
    delay(5);
    Serial.println("UNLOCK:OK");
    return true;
}

bool eraseSector(uint32_t raw_addr) {
    uint32_t aligned_addr = raw_addr & ~0xFFF;

    writeEnable();

    SPI.beginTransaction(spiSettings);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(CMD_SECTOR_ERASE);
    SPI.transfer((aligned_addr >> 16) & 0xFF);
    SPI.transfer((aligned_addr >> 8) & 0xFF);
    SPI.transfer(aligned_addr & 0xFF);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();

    if (!waitBusyWithProgress(500)) {
        return false;
    }

    Serial.printf("ERASE:OK:0x%06X\n", aligned_addr);
    return true;
}

bool eraseChip() {
    writeEnable();

    SPI.beginTransaction(spiSettings);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(CMD_CHIP_ERASE);
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();

    if (!waitBusyWithProgress(5000, 4000)) {
        return false;
    }

    Serial.println("ERASE_CHIP:OK");
    return true;
}

// =========================================================================
// PATTERN GENERATOR
// =========================================================================
uint8_t getPatternByte(uint32_t offset, uint8_t pat_type, uint8_t custom_hex) {
    switch (pat_type) {
        case 0: return 0xFF;
        case 1: return 0x00;
        case 2: return 0xAA;
        case 3: return 0x55;
        case 4: return custom_hex;
        case 5: { // Pseudo-Random
            uint32_t hash = (offset ^ 0x5BF03635) & 0xFFFFFFFF;
            hash = ((hash ^ (hash >> 16)) * 0x45d9f3b) & 0xFFFFFFFF;
            hash = ((hash ^ (hash >> 16)) * 0x45d9f3b) & 0xFFFFFFFF;
            return (hash ^ (hash >> 16)) & 0xFF;
        }
        case 6: return (offset % 2 == 0) ? 0xAA : 0x55;
        case 7: return offset & 0xFF;
        default: return 0xFF;
    }
}

// =========================================================================
// WRITE ENGINE
// =========================================================================

bool writePage(uint32_t addr, uint8_t *data, uint16_t len) {
    writeEnable();

    SPI.beginTransaction(spiSettings);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(CMD_PAGE_PROGRAM);
    SPI.transfer((addr >> 16) & 0xFF);
    SPI.transfer((addr >> 8) & 0xFF);
    SPI.transfer(addr & 0xFF);
    for (uint16_t i = 0; i < len; i++) {
        SPI.transfer(data[i]);
    }
    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();

    return waitBusyWithProgress(100);
}

bool writePatternRange(uint32_t start_addr, uint32_t len, uint8_t pat_type, uint8_t custom_hex) {
    uint8_t page_buf[PAGE_SIZE_BYTES];
    uint32_t written = 0;
    int last_pct = -1;

    while (written < len) {
        if (Serial.available()) {
            String cmd = Serial.readStringUntil('\n');
            cmd.trim();
            if (cmd == "STOP") {
                Serial.println("ABORTED");
                return false;
            }
        }

        uint32_t curr_addr = start_addr + written;
        uint16_t chunk = min((uint32_t)PAGE_SIZE_BYTES, len - written);

        for (uint16_t i = 0; i < chunk; i++) {
            page_buf[i] = getPatternByte(curr_addr + i, pat_type, custom_hex);
        }

        if (!writePage(curr_addr, page_buf, chunk)) {
            return false;
        }

        written += chunk;
        int pct = (written * 100) / len;
        if (pct != last_pct) {
            Serial.printf("PROGRESS:%d\n", pct);
            last_pct = pct;
        }
    }

    Serial.println("WRITE_PAT:OK");
    return true;
}

// =========================================================================
// HIGH-SPEED BLOCK VERIFY ENGINE (SPI.transferBytes + UART Rate Limit Filter)
// =========================================================================

bool verifyPatternRange(uint32_t start_addr, uint32_t len, uint8_t pat_type, uint8_t custom_hex) {
    uint32_t err_count = 0;
    uint32_t first_fail_addr = 0;
    int last_pct = -1;
    uint32_t verified_bytes = 0;
    
    int32_t last_reported_page = -1;

    memset(tx_buf, 0x00, sizeof(tx_buf));

    while (verified_bytes < len) {
        if (Serial.available()) {
            String cmd = Serial.readStringUntil('\n');
            cmd.trim();
            if (cmd == "STOP") {
                Serial.println("ABORTED");
                return false;
            }
        }

        uint32_t curr_addr = start_addr + verified_bytes;
        uint32_t chunk = min((uint32_t)BLOCK_BUF_SIZE, len - verified_bytes);

        SPI.beginTransaction(spiSettings);
        digitalWrite(PIN_CS, LOW);
        SPI.transfer(CMD_READ_DATA);
        SPI.transfer((curr_addr >> 16) & 0xFF);
        SPI.transfer((curr_addr >> 8) & 0xFF);
        SPI.transfer(curr_addr & 0xFF);

        SPI.transferBytes(tx_buf, rx_buf, chunk);

        digitalWrite(PIN_CS, HIGH);
        SPI.endTransaction();

        for (uint32_t i = 0; i < chunk; i++) {
            uint32_t abs_addr = curr_addr + i;
            uint8_t exp_b = getPatternByte(abs_addr, pat_type, custom_hex);

            if (rx_buf[i] != exp_b) {
                if (err_count == 0) {
                    first_fail_addr = abs_addr;
                }
                err_count++;

                int32_t curr_page = abs_addr / 256;
                if (curr_page != last_reported_page) {
                    last_reported_page = curr_page;
                    Serial.printf("FAIL_ADDR:0x%06X:Read=0x%02X,Exp=0x%02X\n", abs_addr, rx_buf[i], exp_b);
                }
            }
        }

        verified_bytes += chunk;

        int pct = (verified_bytes * 100) / len;
        if (pct != last_pct) {
            Serial.printf("PROGRESS:%d\n", pct);
            last_pct = pct;
        }
    }

    if (err_count == 0) {
        Serial.println("VERIFY:OK");
        return true;
    } else {
        Serial.printf("VERIFY:FAIL:%d:0x%06X\n", err_count, first_fail_addr);
        return false;
    }
}

// =========================================================================
// OPTIMIZED ALL-PATTERN SWEEP ENGINE
// =========================================================================
void runAllPatternSweep(bool is_loop) {
    uint8_t pat_list[] = {0, 1, 2, 3, 4, 5, 6, 7};
    uint8_t custom_vals[] = {0x00, 0x00, 0x00, 0x00, 0xA5, 0x00, 0x00, 0x00};

    uint32_t cycle_cnt = 1;

    while (true) {
        Serial.printf("SWEEP_CYCLE_START:%d\n", cycle_cnt);

        for (int p = 0; p < 8; p++) {
            uint8_t pat = pat_list[p];
            uint8_t c_val = custom_vals[p];

            Serial.printf("PAT_START:%d\n", pat);

            Serial.println("PHASE:ERASING");
            for (uint32_t sec = 0; sec < 512; sec++) {
                uint32_t sec_addr = sec * SECTOR_SIZE_BYTES;
                if (!eraseSector(sec_addr)) {
                    return;
                }
                int pct = ((sec + 1) * 100) / 512;
                Serial.printf("PROGRESS:%d\n", pct);
            }

            Serial.println("PHASE:WRITING");
            if (!writePatternRange(0x000000, FLASH_SIZE_BYTES, pat, c_val)) {
                return;
            }

            Serial.println("PHASE:VERIFYING");
            if (!verifyPatternRange(0x000000, FLASH_SIZE_BYTES, pat, c_val)) {
                if (Serial.available()) return;
            }

            Serial.printf("PAT_DONE:%d\n", pat);
        }

        Serial.printf("SWEEP_CYCLE_DONE:%d\n", cycle_cnt);

        if (!is_loop) break;
        cycle_cnt++;
    }
}

void readDataHex(uint32_t start_addr, uint32_t len) {
    uint32_t read_len = min((uint32_t)BLOCK_BUF_SIZE, len);

    memset(tx_buf, 0x00, read_len);

    SPI.beginTransaction(spiSettings);
    digitalWrite(PIN_CS, LOW);
    SPI.transfer(CMD_READ_DATA);
    SPI.transfer((start_addr >> 16) & 0xFF);
    SPI.transfer((start_addr >> 8) & 0xFF);
    SPI.transfer(start_addr & 0xFF);

    SPI.transferBytes(tx_buf, rx_buf, read_len);

    digitalWrite(PIN_CS, HIGH);
    SPI.endTransaction();

    Serial.print("DATA:");
    for (uint32_t i = 0; i < read_len; i++) {
        Serial.printf("%02X", rx_buf[i]);
        if (i < read_len - 1) Serial.print(" ");
    }
    Serial.println();
}

void setup() {
    Serial.begin(921600);
    
    pinMode(PIN_CS, OUTPUT);
    digitalWrite(PIN_CS, HIGH);

    pinMode(PIN_WP, OUTPUT);
    digitalWrite(PIN_WP, HIGH);

    pinMode(PIN_HOLD, OUTPUT);
    digitalWrite(PIN_HOLD, HIGH);

    SPI.begin(PIN_SCK, PIN_MISO, PIN_MOSI, PIN_CS);
    updateSpiSettings(10000000);
}

void loop() {
    if (Serial.available()) {
        String line = Serial.readStringUntil('\n');
        line.trim();

        if (line == "READ_ID") {
            SPI.beginTransaction(spiSettings);
            digitalWrite(PIN_CS, LOW);
            SPI.transfer(CMD_READ_ID);
            uint8_t mfg = SPI.transfer(0x00);
            uint8_t type = SPI.transfer(0x00);
            uint8_t cap = SPI.transfer(0x00);
            digitalWrite(PIN_CS, HIGH);
            SPI.endTransaction();

            Serial.printf("ID:%02X %02X %02X\n", mfg, type, cap);

        } else if (line == "UNLOCK") {
            unlockProtection();

        } else if (line == "READ_REGS") {
            uint8_t sr = readStatusRegister();
            uint8_t cr = readConfigRegister();
            Serial.printf("REGS:%02X:%02X\n", sr, cr);

        } else if (line.startsWith("SET_FREQ:")) {
            uint32_t freq_mhz = line.substring(9).toInt();
            if (freq_mhz > 0 && freq_mhz <= 80) {
                updateSpiSettings(freq_mhz * 1000000);
                Serial.println("FREQ:OK");
            } else {
                Serial.println("FREQ:ERR");
            }

        } else if (line.startsWith("ERASE:")) {
            uint32_t addr = strtoul(line.substring(6).c_str(), NULL, 16);
            eraseSector(addr);

        } else if (line == "ERASE_CHIP") {
            eraseChip();

        } else if (line.startsWith("READ:")) {
            int p1 = line.indexOf(':', 5);
            uint32_t addr = strtoul(line.substring(5, p1).c_str(), NULL, 16);
            uint32_t len = line.substring(p1 + 1).toInt();
            readDataHex(addr, len);

        } else if (line.startsWith("WRITE_PAT:")) {
            int p1 = line.indexOf(':', 10);
            int p2 = line.indexOf(':', p1 + 1);
            int p3 = line.indexOf(':', p2 + 1);

            uint32_t addr = strtoul(line.substring(10, p1).c_str(), NULL, 16);
            uint32_t len = line.substring(p1 + 1, p2).toInt();
            uint8_t pat = line.substring(p2 + 1, p3).toInt();
            uint8_t c_val = strtoul(line.substring(p3 + 1).c_str(), NULL, 16);

            writePatternRange(addr, len, pat, c_val);

        } else if (line.startsWith("WRITE_FULL_PAT:")) {
            int p1 = line.indexOf(':', 15);
            uint8_t pat = line.substring(15, p1).toInt();
            uint8_t c_val = strtoul(line.substring(p1 + 1).c_str(), NULL, 16);

            writePatternRange(0x000000, FLASH_SIZE_BYTES, pat, c_val);

        } else if (line.startsWith("VERIFY:")) {
            int p1 = line.indexOf(':', 7);
            int p2 = line.indexOf(':', p1 + 1);
            int p3 = line.indexOf(':', p2 + 1);

            uint32_t addr = strtoul(line.substring(7, p1).c_str(), NULL, 16);
            uint32_t len = line.substring(p1 + 1, p2).toInt();
            uint8_t pat = line.substring(p2 + 1, p3).toInt();
            uint8_t c_val = strtoul(line.substring(p3 + 1).c_str(), NULL, 16);

            verifyPatternRange(addr, len, pat, c_val);

        } else if (line.startsWith("VERIFY_FULL_PAT:")) {
            int p1 = line.indexOf(':', 16);
            uint8_t pat = line.substring(16, p1).toInt();
            uint8_t c_val = strtoul(line.substring(p1 + 1).c_str(), NULL, 16);

            verifyPatternRange(0x000000, FLASH_SIZE_BYTES, pat, c_val);

        } else if (line.startsWith("ALL_PAT_START:")) {
            bool is_loop = line.substring(14).toInt() == 1;
            runAllPatternSweep(is_loop);
        }
    }
}