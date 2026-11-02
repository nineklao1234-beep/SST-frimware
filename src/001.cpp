#include <Arduino.h>
#include <SPI.h>

#define CS_PIN 5

#define CMD_WREN         0x06
#define CMD_ULBPR        0x98
#define CMD_JEDEC_ID     0x9F
#define CMD_SECTOR_ERASE 0x20
#define CMD_CHIP_ERASE   0xC7
#define CMD_PAGE_PROGRAM 0x02
#define CMD_READ_DATA    0x03
#define CMD_READ_STATUS  0x05
#define CMD_READ_CONFIG  0x35

#define FLASH_SIZE_2MB   0x200000  // 2,097,152 Bytes

uint32_t currentSpiFreqHz = 10000000;
String serialRxBuf = "";

// Non-blocking STOP Command Check
bool checkStopCommand() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      serialRxBuf.trim();
      if (serialRxBuf == "STOP") {
        serialRxBuf = "";
        return true;
      }
      serialRxBuf = "";
    } else {
      serialRxBuf += c;
    }
  }
  return false;
}

void waitBusyWithProgress() {
  uint8_t status;
  int step = 0;
  do {
    if (checkStopCommand()) {
      Serial.println("ABORTED");
      return;
    }
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_STATUS);
    status = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    
    delay(5);
    step++;
    if (step % 2 == 0 && step <= 18) {
      int pct = (step / 2) * 10;
      Serial.printf("PROGRESS:%d\n", pct);
    }
  } while (status & 0x01);
  Serial.println("PROGRESS:100");
}

void writeEnable() {
  SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
  digitalWrite(CS_PIN, LOW);
  SPI.transfer(CMD_WREN);
  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();
}

uint8_t getPatternByte(uint32_t offset, int patternType, uint8_t customVal = 0x00) {
  switch (patternType) {
    case 0: return 0xAA;
    case 1: return 0x55;
    case 2: return (offset % 2 == 0) ? 0xAA : 0x55;
    case 3: return (uint8_t)(offset & 0xFF);
    case 4: return 0x00;
    case 5: { // Pseudo-Random PRNG
      uint32_t hash = offset ^ 0x5BF03635;
      hash = (hash ^ (hash >> 16)) * 0x45d9f3b;
      hash = (hash ^ (hash >> 16)) * 0x45d9f3b;
      return (uint8_t)((hash ^ (hash >> 16)) & 0xFF);
    }
    case 6: return customVal;
    default: return 0xFF;
  }
}

bool writePattern(uint32_t addr, uint32_t len, int patternType, uint8_t customVal) {
  uint32_t bytesWritten = 0;
  uint8_t pageBuf[256];
  int lastPct = -1;

  Serial.println("PROGRESS:0");

  while (bytesWritten < len) {
    if (checkStopCommand()) {
      Serial.println("ABORTED");
      return false;
    }

    uint32_t currentAddr = addr + bytesWritten;
    uint16_t chunkSize = 256 - (currentAddr % 256);
    if (bytesWritten + chunkSize > len) {
      chunkSize = len - bytesWritten;
    }

    for (uint16_t i = 0; i < chunkSize; i++) {
      pageBuf[i] = getPatternByte(bytesWritten + i, patternType, customVal);
    }

    writeEnable();
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_PAGE_PROGRAM);
    SPI.transfer((currentAddr >> 16) & 0xFF);
    SPI.transfer((currentAddr >> 8) & 0xFF);
    SPI.transfer(currentAddr & 0xFF);

    for (uint16_t i = 0; i < chunkSize; i++) {
      SPI.transfer(pageBuf[i]);
    }
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    
    uint8_t status;
    do {
      SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
      digitalWrite(CS_PIN, LOW);
      SPI.transfer(CMD_READ_STATUS);
      status = SPI.transfer(0x00);
      digitalWrite(CS_PIN, HIGH);
      SPI.endTransaction();
      delayMicroseconds(50);
    } while (status & 0x01);

    bytesWritten += chunkSize;

    int currentPct = (int)((uint64_t)bytesWritten * 100 / len);
    int currentStep = (currentPct / 10) * 10;
    if (currentStep != lastPct) {
      lastPct = currentStep;
      Serial.printf("PROGRESS:%d\n", lastPct);
    }
  }
  Serial.println("PROGRESS:100");
  Serial.println("WRITE_PAT:OK");
  return true;
}

bool verifyPattern(uint32_t addr, uint32_t len, int patternType, uint8_t customVal, uint32_t &outErrors) {
  outErrors = 0;
  uint32_t firstErrAddr = 0;
  uint8_t expectedErr = 0, actualErr = 0;
  int lastPct = -1;

  Serial.println("PROGRESS:0");

  SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
  digitalWrite(CS_PIN, LOW);
  SPI.transfer(CMD_READ_DATA);
  SPI.transfer((addr >> 16) & 0xFF);
  SPI.transfer((addr >> 8) & 0xFF);
  SPI.transfer(addr & 0xFF);

  for (uint32_t i = 0; i < len; i++) {
    if ((i % 1024 == 0) && checkStopCommand()) {
      digitalWrite(CS_PIN, HIGH);
      SPI.endTransaction();
      Serial.println("ABORTED");
      return false;
    }

    uint8_t actual = SPI.transfer(0x00);
    uint8_t expected = getPatternByte(i, patternType, customVal);

    if (actual != expected) {
      if (outErrors == 0) {
        firstErrAddr = addr + i;
        expectedErr = expected;
        actualErr = actual;
      }
      outErrors++;
    }

    int currentPct = (int)((uint64_t)(i + 1) * 100 / len);
    int currentStep = (currentPct / 10) * 10;
    if (currentStep != lastPct) {
      lastPct = currentStep;
      Serial.printf("PROGRESS:%d\n", lastPct);
    }
  }
  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();

  Serial.println("PROGRESS:100");
  if (outErrors == 0) {
    Serial.println("VERIFY:OK");
  } else {
    Serial.printf("VERIFY:FAIL:%d:0x%06X:EXP_0x%02X_GOT_0x%02X\n", outErrors, firstErrAddr, expectedErr, actualErr);
  }
  return true;
}

bool eraseChip() {
  Serial.println("PROGRESS:0");
  writeEnable();
  SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
  digitalWrite(CS_PIN, LOW);
  SPI.transfer(CMD_CHIP_ERASE);
  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();
  waitBusyWithProgress();
  return true;
}

bool eraseSector(uint32_t addr) {
  Serial.println("PROGRESS:0");
  writeEnable();
  SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
  digitalWrite(CS_PIN, LOW);
  SPI.transfer(CMD_SECTOR_ERASE);
  SPI.transfer((addr >> 16) & 0xFF);
  SPI.transfer((addr >> 8) & 0xFF);
  SPI.transfer(addr & 0xFF);
  digitalWrite(CS_PIN, HIGH);
  SPI.endTransaction();
  waitBusyWithProgress();
  return true;
}

void runLoopTest(int scope, uint32_t addr, uint32_t len, int patType, uint8_t customVal) {
  uint32_t cycle = 1;
  while (true) {
    if (checkStopCommand()) { Serial.println("ABORTED"); break; }
    Serial.printf("CYCLE_START:%u\n", cycle);

    Serial.println("PHASE:ERASE");
    if (scope == 1) {
      if (!eraseChip()) break;
    } else {
      if (!eraseSector(addr)) break;
    }
    if (checkStopCommand()) { Serial.println("ABORTED"); break; }

    Serial.println("PHASE:WRITE");
    if (scope == 1) {
      if (!writePattern(0x000000, FLASH_SIZE_2MB, patType, customVal)) break;
    } else {
      if (!writePattern(addr, len, patType, customVal)) break;
    }
    if (checkStopCommand()) { Serial.println("ABORTED"); break; }

    Serial.println("PHASE:VERIFY");
    uint32_t errs = 0;
    if (scope == 1) {
      if (!verifyPattern(0x000000, FLASH_SIZE_2MB, patType, customVal, errs)) break;
    } else {
      if (!verifyPattern(addr, len, patType, customVal, errs)) break;
    }

    Serial.printf("CYCLE_DONE:%u:%u\n", cycle, errs);
    cycle++;
  }
}

void runAllPatternSweep(bool isLoop) {
  uint32_t cycle = 1;
  do {
    if (checkStopCommand()) { Serial.println("ABORTED"); break; }
    Serial.printf("SWEEP_CYCLE_START:%u\n", cycle);

    for (int pat = 0; pat <= 5; pat++) {
      if (checkStopCommand()) { Serial.println("ABORTED"); return; }
      Serial.printf("PAT_START:%d\n", pat);

      Serial.println("PHASE:ERASE");
      if (!eraseChip()) return;

      Serial.println("PHASE:WRITE");
      if (!writePattern(0x000000, FLASH_SIZE_2MB, pat, 0x00)) return;

      Serial.println("PHASE:VERIFY");
      uint32_t errs = 0;
      if (!verifyPattern(0x000000, FLASH_SIZE_2MB, pat, 0x00, errs)) return;

      Serial.printf("PAT_DONE:%d:%u\n", pat, errs);
    }

    Serial.printf("SWEEP_CYCLE_DONE:%u\n", cycle);
    cycle++;
  } while (isLoop);
}

// Helpers for safe command tokenizing
uint32_t parseHex(String str) {
  str.trim();
  if (str.startsWith("0x") || str.startsWith("0X")) {
    str = str.substring(2);
  }
  return strtoul(str.c_str(), NULL, 16);
}

int splitString(String src, char delim, String tokens[], int maxTokens) {
  int count = 0;
  int start = 0;
  int len = src.length();
  for (int i = 0; i <= len; i++) {
    if (i == len || src.charAt(i) == delim) {
      if (count < maxTokens) {
        tokens[count++] = src.substring(start, i);
      }
      start = i + 1;
    }
  }
  return count;
}

void processCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  String tokens[8];
  int count = splitString(cmd, ':', tokens, 8);
  String action = tokens[0];

  if (action == "SET_FREQ" && count >= 2) {
    int mhz = tokens[1].toInt();
    if (mhz > 0 && mhz <= 80) {
      currentSpiFreqHz = mhz * 1000000;
      Serial.printf("FREQ:OK:%d\n", mhz);
    } else {
      Serial.println("FREQ:ERR");
    }
  }
  else if (action == "READ_ID") {
    Serial.println("PROGRESS:0");
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_JEDEC_ID);
    uint8_t mfg  = SPI.transfer(0x00);
    uint8_t type = SPI.transfer(0x00);
    uint8_t id   = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    Serial.println("PROGRESS:100");
    Serial.printf("ID:%02X %02X %02X\n", mfg, type, id);
  }
  else if (action == "UNLOCK") {
    Serial.println("PROGRESS:0");
    writeEnable();
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_ULBPR);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    delay(10);
    Serial.println("PROGRESS:100");
    Serial.println("UNLOCK:OK");
  }
  else if (action == "READ_REGS") {
    Serial.println("PROGRESS:0");
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_STATUS);
    uint8_t stat = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();

    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_CONFIG);
    uint8_t cfg = SPI.transfer(0x00);
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();

    Serial.println("PROGRESS:100");
    Serial.printf("REGS:%02X:%02X\n", stat, cfg);
  }
  else if (action == "ERASE" && count >= 2) {
    uint32_t addr = parseHex(tokens[1]);
    eraseSector(addr);
    Serial.println("ERASE:OK");
  }
  else if (action == "ERASE_CHIP") {
    eraseChip();
    Serial.println("ERASE_CHIP:OK");
  }
  else if (action == "WRITE_PAT" && count >= 5) {
    uint32_t addr = parseHex(tokens[1]);
    uint32_t len = tokens[2].toInt();
    int patType = tokens[3].toInt();
    uint8_t customVal = (uint8_t)parseHex(tokens[4]);
    writePattern(addr, len, patType, customVal);
  }
  else if (action == "WRITE_FULL_PAT" && count >= 3) {
    int patType = tokens[1].toInt();
    uint8_t customVal = (uint8_t)parseHex(tokens[2]);
    writePattern(0x000000, FLASH_SIZE_2MB, patType, customVal);
  }
  else if (action == "VERIFY" && count >= 5) {
    uint32_t addr = parseHex(tokens[1]);
    uint32_t len = tokens[2].toInt();
    int patType = tokens[3].toInt();
    uint8_t customVal = (uint8_t)parseHex(tokens[4]);
    uint32_t dummyErrs = 0;
    verifyPattern(addr, len, patType, customVal, dummyErrs);
  }
  else if (action == "VERIFY_FULL_PAT" && count >= 3) {
    int patType = tokens[1].toInt();
    uint8_t customVal = (uint8_t)parseHex(tokens[2]);
    uint32_t dummyErrs = 0;
    verifyPattern(0x000000, FLASH_SIZE_2MB, patType, customVal, dummyErrs);
  }
  else if (action == "LOOP_START" && count >= 6) {
    int scope = tokens[1].toInt();
    uint32_t addr = parseHex(tokens[2]);
    uint32_t len = tokens[3].toInt();
    int patType = tokens[4].toInt();
    uint8_t customVal = (uint8_t)parseHex(tokens[5]);
    runLoopTest(scope, addr, len, patType, customVal);
  }
  else if (action == "ALL_PAT_START" && count >= 2) {
    bool isLoop = (tokens[1].toInt() == 1);
    runAllPatternSweep(isLoop);
  }
  else if (action == "READ" && count >= 3) {
    uint32_t addr = parseHex(tokens[1]);
    int len = tokens[2].toInt();
    if (len <= 0) len = 16;
    if (len > 512) len = 512;

    Serial.println("PROGRESS:0");
    SPI.beginTransaction(SPISettings(currentSpiFreqHz, MSBFIRST, SPI_MODE0));
    digitalWrite(CS_PIN, LOW);
    SPI.transfer(CMD_READ_DATA);
    SPI.transfer((addr >> 16) & 0xFF);
    SPI.transfer((addr >> 8) & 0xFF);
    SPI.transfer(addr & 0xFF);

    Serial.print("DATA:");
    for (int i = 0; i < len; i++) {
      uint8_t b = SPI.transfer(0x00);
      Serial.printf("%02X", b);
      if (i < len - 1) Serial.print(" ");
    }
    Serial.println();
    digitalWrite(CS_PIN, HIGH);
    SPI.endTransaction();
    Serial.println("PROGRESS:100");
  }
}

void setup() {
  Serial.begin(921600);
  pinMode(CS_PIN, OUTPUT);
  digitalWrite(CS_PIN, HIGH);
  SPI.begin(18, 19, 23, CS_PIN);
  Serial.println("ESP32_READY");
}

void loop() {
  if (Serial.available()) {
    String input = Serial.readStringUntil('\n');
    processCommand(input);
  }
}