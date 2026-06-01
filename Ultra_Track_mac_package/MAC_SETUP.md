# macOS (Apple Silicon M1) 셋업 가이드 — Ultra_Track 실시간 시각화

이 문서는 **MacBook M1에서 `run_realtime_visual.py`(실시간 제스처 인식 + 대시보드)를
실행**하기 위한 설치/실행 안내입니다. 작업 에이전트에게 그대로 전달해도 됩니다.

> 추론 로직(자동 트리거 + optuna 모델 + per-sample 정규화)은 Windows 원본과 100% 동일.
> 이 가이드는 **환경 설치 + macOS 특이사항(시리얼 포트 / 한글 폰트)**만 다룹니다.

---

## 1. 사전 준비

- macOS (Apple Silicon, M1 이상)
- Python **3.11 ~ 3.13** (TensorFlow 호환 범위). 미설치 시 권장 설치:
  ```bash
  brew install python@3.12
  ```
- ESP32 보드용 USB-시리얼 드라이버. 보드 USB 칩에 따라 둘 중 하나:
  - **CP210x** (Silicon Labs): https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers
  - **CH340/CH9102** (WCH): https://www.wch.cn/downloads/CH34XSER_MAC_ZIP.html
  - 드라이버 설치 후 macOS **시스템 설정 > 개인정보 보호 및 보안**에서 차단된 확장 프로그램 "허용" 필요할 수 있음.

---

## 2. 가상환경 + 패키지 설치

프로젝트 폴더에서:

```bash
cd Ultra_Track

# 가상환경 생성/활성화
python3.12 -m venv .venv
source .venv/bin/activate

# 패키지 설치
pip install --upgrade pip
pip install -r requirements.txt
```

`requirements.txt` 내용 (참고):

```
tensorflow>=2.18,<2.22   # keras 3 번들 포함 (.keras 모델 로드에 필요)
numpy>=1.26
pyserial>=3.5
matplotlib>=3.8
```

> **Apple Silicon 참고**
> - `pip install tensorflow` 가 arm64 휠로 바로 설치됩니다. `tensorflow-macos`는 더 이상 필요 없습니다.
> - 모델이 작아 **CPU로 충분**하므로 `tensorflow-metal`(GPU 가속)은 설치하지 않아도 됩니다.
>   (metal은 일부 버전에서 호환 이슈가 있어 권장하지 않음.)

설치 확인:
```bash
python -c "import tensorflow as tf, keras, numpy, serial, matplotlib; \
print('tf', tf.__version__, '| keras', keras.__version__)"
```
→ `keras 3.x` 가 보이면 정상 (모델 `.keras` 파일이 keras 3 포맷이라 keras 3 필수).

---

## 3. 시리얼 포트 (macOS는 COM 아님)

Windows의 `COM6` 대신 macOS는 `/dev/cu.usbserial-*` 또는 `/dev/cu.usbmodem*` 형식입니다.

연결된 포트 확인:
```bash
ls /dev/cu.*
# 예: /dev/cu.usbserial-110   또는   /dev/cu.usbmodem14201
```

**스크립트가 자동 탐색**하므로 보통 그냥 실행하면 됩니다. 자동 탐색이 빗나가면 수동 지정:

```bash
# 방법 A: 환경변수
ULTRA_PORT=/dev/cu.usbserial-110 python run_realtime_visual.py

# 방법 B: 실행 인자
python run_realtime_visual.py /dev/cu.usbserial-110
```

> 포트 결정 우선순위: `ULTRA_PORT` 환경변수 → 실행 인자 → 자동 탐색 → (없으면) `COM6`.
> `tty.*`가 아니라 **`cu.*`** 를 쓰세요(읽기 전용 콜아웃 장치라 안정적).

---

## 4. 한글 폰트

스크립트가 설치된 폰트 중 한글 가능한 것을 **자동 선택**합니다
(우선순위: Noto Sans KR → Apple SD Gothic Neo → AppleGothic → …).
**macOS에는 `Apple SD Gothic Neo`가 기본 내장**되어 있어 별도 설치 없이 한글이 표시됩니다.

(선택) Noto Sans KR을 쓰고 싶으면:
```bash
brew install --cask font-noto-sans-cjk-kr
```
설치 후 matplotlib 폰트 캐시 갱신:
```bash
python -c "import matplotlib.font_manager as fm; fm._load_fontmanager(try_read_cache=False)"
```

---

## 5. 실행

```bash
source .venv/bin/activate          # (새 터미널이면)
python run_realtime_visual.py
```

- 시작 시 "baseline 측정" 동안 **센서에서 손을 치워** 두세요(약 1~2초).
- 창에 실시간 파형 + 모션 미터 + 인식 결과가 표시됩니다.
- 종료: 창 닫기 또는 `Ctrl+C`.

---

## 6. 트러블슈팅

| 증상 | 원인 / 해결 |
|------|-------------|
| `시리얼 포트 ...를 열 수 없습니다` | 드라이버 미설치 또는 포트명 오류. `ls /dev/cu.*`로 확인 후 `ULTRA_PORT=`로 지정. 다른 프로그램(Arduino IDE 시리얼 모니터 등)이 포트를 점유 중이면 닫기. |
| `ValueError: ... .keras` 로드 실패 | keras 2 환경. `pip show keras`로 **3.x** 확인 (tensorflow>=2.18 필요). |
| 한글이 □로 깨짐 | 폰트 자동선택 실패. 4번 항목으로 Noto Sans KR 설치 후 캐시 갱신. |
| 파형이 늦게 뜸 / 끊김 | 시리얼 드라이버 문제 가능. 다른 USB 포트/케이블 시도. `DRAW_INTERVAL`(스크립트 상단)을 0.08로 늘려도 됨. |
| 창이 안 뜨고 멈춤 | matplotlib 백엔드 문제. `pip install pyobjc` 후 재시도하거나 `MPLBACKEND=macosx python run_realtime_visual.py`. |

---

## 7. 함께 옮겨야 할 파일

맥북으로 복사할 때 아래는 **반드시 포함**:

```
run_realtime_visual.py        # 실행 스크립트
requirements.txt              # 패키지
model/gesture_model_optuna.keras   # 모델 (없으면 gesture_model_v2.keras 로 폴백)
```

`dataset/`, `debug_captures*/`, `results/`, 노트북(`*.ipynb`) 등은 **실행에 불필요**(재학습/분석용).
재학습까지 할 거면 `train_model.py` + `dataset/` 도 함께 가져가세요.
