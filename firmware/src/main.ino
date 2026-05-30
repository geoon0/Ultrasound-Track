/*
  ESP32-S3 3-Channel 40kHz Ultrasonic Envelope Detection
  - Tx : GPIO 1 (40kHz PWM 50%), GPIO 2 (GND)
  - Rx1: GPIO 4 (ADC1_CH3) - Left
  - Rx2: GPIO 5 (ADC1_CH4) - Center
  - Rx3: GPIO 6 (ADC1_CH5) - Right
  - 비대칭 파형 처리: 클리핑되는 Max 무시, Min만 사용 -> Envelope = BASELINE - Min
*/

#include <Arduino.h>
#include <cstring>

#if __has_include("esp_adc/adc_continuous.h")
  #include "esp_adc/adc_continuous.h"
#else
  #include "driver/adc_continuous.h"
#endif
#include "esp_err.h"

// ===================== 설정 =====================
static const uint32_t kSampleRateHz = 80000;   // 전체 (3채널 round-robin -> 채널당 약 26.6kHz)
static const uint32_t kBaud         = 230400;

static const int kTxPin_PWM = 1;
static const int kTxPin_GND = 2;

static const adc_unit_t    kAdcUnit  = ADC_UNIT_1;
static const adc_channel_t kCh1 = ADC_CHANNEL_3; // GPIO4
static const adc_channel_t kCh2 = ADC_CHANNEL_4; // GPIO5
static const adc_channel_t kCh3 = ADC_CHANNEL_5; // GPIO6

static const int     kBaseline       = 1800;   // 1채널에서 검증된 기준값
static const size_t  kFrameSamples   = 240;    // DMA 프레임 (3의 배수여야 함 - 240으로 수정)
static const size_t  kCaptureTarget  = 80;     // 채널당 이만큼 모으면 한 프레임 완료
static const size_t  kMaxReadSamples = 1536;   // 무한루프 방지 상한

// 디버그 모드: true면 각 채널 수집 개수도 함께 출력
static const bool    kDebug          = true;

// ===================== 상태 =====================
static adc_continuous_handle_t s_adc = nullptr;
static adc_digi_pattern_config_t s_pattern[3];
static uint8_t s_frameBuf[kFrameSamples * sizeof(adc_digi_output_data_t)];

static uint16_t s_min1, s_min2, s_min3;
static uint16_t s_max1, s_max2, s_max3;
static size_t   s_cnt1, s_cnt2, s_cnt3;

// 데이터셋용 스무딩(Peak-Hold & Decay) 필터 변수
static float s_env1 = 0.0f;
static float s_env2 = 0.0f;
static float s_env3 = 0.0f;
static const float kDecayRate = 0.90f; // 감쇠율 (1.0에 가까울수록 부드러움, 낮을수록 반응 빠름)

// 데이터셋 출력 주기 (일정한 dt 유지를 위해)
static const uint32_t kOutputIntervalMs = 20; // 50Hz (20ms마다 출력)
static uint32_t s_lastOutTime = 0;

static void dieOnError(esp_err_t err, const char* what) {
  if (err == ESP_OK) return;
  while (true) {
    Serial.printf("[FATAL] %s : 0x%08X\n", what, (unsigned)err);
    delay(1000);
  }
}

static void adcInit() {
  adc_continuous_handle_cfg_t hcfg;
  std::memset(&hcfg, 0, sizeof(hcfg));
  hcfg.max_store_buf_size = (int)(sizeof(s_frameBuf) * 8);
  hcfg.conv_frame_size    = (int)sizeof(s_frameBuf);
  dieOnError(adc_continuous_new_handle(&hcfg, &s_adc), "new_handle");

  std::memset(s_pattern, 0, sizeof(s_pattern));
  const adc_channel_t chs[3] = {kCh1, kCh2, kCh3};
  for (int i = 0; i < 3; ++i) {
    s_pattern[i].atten     = ADC_ATTEN_DB_12;  // 0~3.3V 풀스윙
    s_pattern[i].channel   = chs[i];
    s_pattern[i].unit      = kAdcUnit;
    s_pattern[i].bit_width = ADC_BITWIDTH_12;
  }

  adc_continuous_config_t cfg;
  std::memset(&cfg, 0, sizeof(cfg));
  cfg.pattern_num    = 3;
  cfg.adc_pattern    = s_pattern;
  cfg.sample_freq_hz = kSampleRateHz;
  cfg.conv_mode      = ADC_CONV_SINGLE_UNIT_1;
  cfg.format         = ADC_DIGI_OUTPUT_FORMAT_TYPE2;
  dieOnError(adc_continuous_config(s_adc, &cfg), "config");
}

void setup() {
  Serial.begin(kBaud);
  delay(1000); // USB CDC 안정화 대기

  Serial.println("--- ESP32-S3 3CH Dataset Collection Start ---");

  // Tx: GPIO1 40kHz PWM, GPIO2 GND 고정
  pinMode(kTxPin_GND, OUTPUT);
  digitalWrite(kTxPin_GND, LOW);

  #if ESP_ARDUINO_VERSION_MAJOR >= 3
    ledcAttach(kTxPin_PWM, 40000, 8);
    ledcWrite(kTxPin_PWM, 127);
  #else
    ledcSetup(0, 40000, 8);
    ledcAttachPin(kTxPin_PWM, 0);
    ledcWrite(0, 127);
  #endif

  adcInit();
  dieOnError(adc_continuous_start(s_adc), "start");
}

// 한 프레임 분량을 채울 때까지 읽으면서 채널별 Min, Max 추적
static bool captureFrame() {
  s_min1 = s_min2 = s_min3 = 4095;
  s_max1 = s_max2 = s_max3 = 0;
  s_cnt1 = s_cnt2 = s_cnt3 = 0;
  size_t totalRead = 0;

  while (totalRead < kMaxReadSamples) {
    uint32_t bytesRead = 0;
    esp_err_t ret = adc_continuous_read(
      s_adc, s_frameBuf, (uint32_t)sizeof(s_frameBuf), &bytesRead, 50);

    if (ret == ESP_OK && bytesRead > 0) {
      const size_t n = bytesRead / sizeof(adc_digi_output_data_t);
      const adc_digi_output_data_t* d = (const adc_digi_output_data_t*)s_frameBuf;

      for (size_t i = 0; i < n; ++i) {
        uint16_t raw = d[i].type2.data;
        uint8_t  ch  = d[i].type2.channel;
        totalRead++;

        if (ch == kCh1)      { if (raw < s_min1) s_min1 = raw; if (raw > s_max1) s_max1 = raw; s_cnt1++; }
        else if (ch == kCh2) { if (raw < s_min2) s_min2 = raw; if (raw > s_max2) s_max2 = raw; s_cnt2++; }
        else if (ch == kCh3) { if (raw < s_min3) s_min3 = raw; if (raw > s_max3) s_max3 = raw; s_cnt3++; }
      }
      // 세 채널 모두 목표치 이상 모이면 종료
      if (s_cnt1 >= kCaptureTarget && s_cnt2 >= kCaptureTarget && s_cnt3 >= kCaptureTarget)
        return true;
    } else if (ret == ESP_ERR_TIMEOUT) {
      return false; // 데이터 안 들어옴
    } else if (ret != ESP_OK) {
      dieOnError(ret, "read");
    }
  }
  return true;
}

static inline uint16_t envFromMaxMin(uint16_t mx, uint16_t mn, size_t cnt) {
  if (cnt == 0 || mx < mn) return 0;
  return mx - mn;
}

void loop() {
  if (!captureFrame()) {
    Serial.println(">timeout:1");  
    return;
  }

  // 1. Raw Peak-to-Peak 계산
  uint16_t raw1 = envFromMaxMin(s_max1, s_min1, s_cnt1);
  uint16_t raw2 = envFromMaxMin(s_max2, s_min2, s_cnt2);
  uint16_t raw3 = envFromMaxMin(s_max3, s_min3, s_cnt3);

  // 2. Peak-Hold & Decay 필터 적용 (언더샘플링으로 인한 맥놀이 제거 및 부드러운 곡선 생성)
  s_env1 = (raw1 > s_env1) ? raw1 : (s_env1 * kDecayRate);
  s_env2 = (raw2 > s_env2) ? raw2 : (s_env2 * kDecayRate);
  s_env3 = (raw3 > s_env3) ? raw3 : (s_env3 * kDecayRate);

  // 3. 일정한 시간 간격(dt)으로 데이터셋 출력
  uint32_t now = millis();
  if (now - s_lastOutTime >= kOutputIntervalMs) {
    s_lastOutTime = now;
    
    // Teleplot 형식 (우측의 Export to CSV 버튼으로 바로 저장 가능)
    Serial.printf(">Rx1:%u\n>Rx2:%u\n>Rx3:%u\n", (unsigned)s_env1, (unsigned)s_env2, (unsigned)s_env3);

    if (kDebug) {
      // 디버그 데이터는 필요할 때만 켭니다. 데이터셋 추출 시에는 kDebug = false 권장.
      Serial.printf(">cnt1:%u\n>cnt2:%u\n>cnt3:%u\n", (unsigned)s_cnt1, (unsigned)s_cnt2, (unsigned)s_cnt3);
    }
  }
}
