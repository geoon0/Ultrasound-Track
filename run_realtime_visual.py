# -*- coding: utf-8 -*-
"""실시간 제스처 인식 + 시연용 대시보드 (matplotlib).

run_realtime_auto.py 의 자동 감지/추론 로직(자동 트리거 + optuna 모델 +
per-sample 정규화)을 그대로 유지하면서, 발표/시연용으로 보기 좋은
다크 테마 대시보드를 실시간으로 그린다.

화면 구성
  - 상단      : 타이틀 + 현재 상태(대기/녹화/분석) + 실시간 변화량 게이지
  - 좌측(대형): 3채널 초음파 신호 실시간 파형 (baseline 대비, 스크롤)
  - 우측 상단 : 최종 인식 결과 카드 (클래스 + 확신도, 방향 표시)
  - 우측 하단 : 클래스별 확률 막대

실행 환경: conda base (tensorflow 2.21 / keras 3) — `python run_realtime_visual.py`
종료: 창 닫기 또는 Ctrl+C
"""
import serial
import os
import sys
import time
from collections import deque

import numpy as np
import tensorflow as tf

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Rectangle

# ===================== 설정 (추론 로직: run_realtime_auto.py 와 동일) =====================
BAUD = 230400


def resolve_port():
    """시리얼 포트 결정 순서: 환경변수 ULTRA_PORT > 실행 인자 > 자동탐색 > 기본값.

    Windows: COMx / macOS: /dev/cu.usbserial-* 또는 /dev/cu.usbmodem* (자동탐색).
    수동 지정:  ULTRA_PORT=/dev/cu.usbserial-110 python run_realtime_visual.py
           또는  python run_realtime_visual.py /dev/cu.usbserial-110
    """
    if os.environ.get('ULTRA_PORT'):
        return os.environ['ULTRA_PORT']
    if len(sys.argv) > 1:
        return sys.argv[1]
    try:
        from serial.tools import list_ports
        devices = [p.device for p in list_ports.comports()]
        for dev in devices:
            low = dev.lower()
            if any(k in low for k in ('usbserial', 'usbmodem', 'wchusb', 'slab')):
                return dev
        if devices:
            return devices[0]
    except Exception:
        pass
    return 'COM6'   # 윈도우 기본값


PORT = resolve_port()
CLASSES = ["LEFT", "RIGHT", "PUSH", "IDLE"]
SAMPLES_PER_GESTURE = 100
NUM_CHANNELS = 3

CONF_THRESHOLD = 0.95     # 확신도 커트라인
TRIGGER_THRESHOLD = 160   # 최근 5프레임 채널 변화량이 이 값을 넘으면 시작
POST_TRIGGER_FRAMES = 80  # 트리거 후 추가 수집 프레임
COOLDOWN_FRAMES = 50      # 인식 완료 후 대기 프레임

DEBUG_SAVE = True
DEBUG_DIR = "debug_captures_auto"

# --- 시각화 설정 ---
BASELINE_FRAMES = 50      # 시작 시 baseline 측정 프레임 수
HIST_LEN = 180            # 파형에 표시할 최근 프레임 수

# ===================== 디자인 팔레트 (다크 테마) =====================
C_BG       = '#0e1116'    # 전체 배경
C_PANEL    = '#161b22'    # 패널 배경
C_GRID     = '#262d38'    # 그리드
C_TEXT     = '#e6edf3'    # 기본 텍스트
C_SUBTEXT  = '#8b949e'    # 보조 텍스트
C_ACCENT   = '#58a6ff'    # 포인트

# 채널 색 (Rx1 왼쪽 / Rx2 중앙 / Rx3 오른쪽)
SIG_COLORS = {'Rx1': '#ff7b72', 'Rx2': '#56d364', 'Rx3': '#79c0ff'}
# 클래스 색
CLASS_COLORS = {"LEFT": '#79c0ff', "RIGHT": '#ffa657',
                "PUSH": '#56d364', "IDLE": '#6e7681'}
# 방향 표시 기호 (이모지 아님 — 기하 마커)
DIR_MARK = {"LEFT": '◀', "RIGHT": '▶', "PUSH": '▼', "IDLE": '○'}

# ===================== 모델 로딩 =====================
print("=" * 56)
print("인공지능 모델 로딩 중... 잠시만 기다려주세요.")
print("=" * 56)
model_path = os.path.join("model", "gesture_model_optuna.keras")
if not os.path.exists(model_path):
    model_path = os.path.join("model", "gesture_model_v2.keras")
model = tf.keras.models.load_model(model_path)
print(f"로딩 완료: {model_path}\n")

# ===================== 시리얼 연결 =====================
print(f"시리얼 포트 연결 시도: {PORT} @ {BAUD}")
try:
    ser = serial.Serial(PORT, BAUD, timeout=0.1)
except serial.SerialException:
    print(f"[오류] 시리얼 포트 {PORT}를 열 수 없습니다.")
    print("       포트를 직접 지정하세요:  ULTRA_PORT=<포트> python run_realtime_visual.py")
    print("       (macOS 포트 목록 확인:  ls /dev/cu.*)")
    raise SystemExit(1)

ser.reset_input_buffer()

# 시리얼 파싱 상태
rx_state = 0
cur = [0, 0, 0]


def read_frame_blocking():
    """완성된 1프레임 [Rx1, Rx2, Rx3] 을 반환. 데이터가 없으면 None."""
    global rx_state
    line = ser.readline().decode('utf-8', errors='ignore').strip()
    if not line:
        return None
    try:
        if rx_state == 0 and line.startswith(">Rx1:"):
            cur[0] = int(line.split(":")[1]); rx_state = 1
        elif rx_state == 1 and line.startswith(">Rx2:"):
            cur[1] = int(line.split(":")[1]); rx_state = 2
        elif rx_state == 2 and line.startswith(">Rx3:"):
            cur[2] = int(line.split(":")[1]); rx_state = 0
            return list(cur)
    except (ValueError, IndexError):
        rx_state = 0
    return None


# ===================== baseline 측정 =====================
print("baseline 측정 중... 센서에서 손을 완전히 치워주세요 (약 1~2초).")
base_buf = []
while len(base_buf) < BASELINE_FRAMES:
    f = read_frame_blocking()
    if f is not None:
        base_buf.append(f)
base = np.mean(base_buf, axis=0)
print(f"baseline -> Rx1={base[0]:.0f}  Rx2={base[1]:.0f}  Rx3={base[2]:.0f}\n")

# ===================== matplotlib 화면 구성 =====================
# 한글 폰트: 설치된 것 중 첫 번째를 선택 (Win=Noto/Malgun, macOS=Apple SD Gothic Neo)
import matplotlib.font_manager as fm

_KR_CANDIDATES = ['Noto Sans KR', 'Apple SD Gothic Neo', 'AppleGothic',
                  'Malgun Gothic', 'NanumGothic', 'Noto Sans CJK KR']
_available = {f.name for f in fm.fontManager.ttflist}
_kr_font = next((c for c in _KR_CANDIDATES if c in _available), 'DejaVu Sans')
print(f"한글 폰트: {_kr_font}")

plt.rcParams.update({
    'font.family': _kr_font,
    'font.sans-serif': _KR_CANDIDATES + ['DejaVu Sans'],
    'axes.unicode_minus': False,
    'figure.facecolor': C_BG,
    'savefig.facecolor': C_BG,
    'text.color': C_TEXT,
    'axes.edgecolor': C_GRID,
    'axes.labelcolor': C_SUBTEXT,
    'xtick.color': C_SUBTEXT,
    'ytick.color': C_SUBTEXT,
})

fig = plt.figure(figsize=(15, 8))
try:
    fig.canvas.manager.set_window_title("Ultra Track  -  Real-time Gesture Recognition")
except Exception:
    pass

# 행: [헤더, 본문], 열: [파형(대형), 우측 패널]
gs = fig.add_gridspec(2, 2, width_ratios=[1.55, 1], height_ratios=[0.16, 1],
                      left=0.045, right=0.965, top=0.95, bottom=0.08,
                      wspace=0.18, hspace=0.28)


def style_panel(ax):
    ax.set_facecolor(C_PANEL)
    for s in ax.spines.values():
        s.set_color(C_GRID)
    return ax


# --- 헤더 (전체 폭) ---
ax_head = fig.add_subplot(gs[0, :])
ax_head.set_facecolor(C_BG)
ax_head.set_xlim(0, 1); ax_head.set_ylim(0, 1)
ax_head.axis('off')
ax_head.text(0.0, 0.72, "ULTRA TRACK", fontsize=22, fontweight='bold',
             color=C_TEXT, va='center', ha='left')
ax_head.text(0.0, 0.24, "초음파 3채널 실시간 제스처 인식", fontsize=11,
             color=C_SUBTEXT, va='center', ha='left')

# 상태 표시 (가운데)
status_txt = ax_head.text(0.52, 0.5, "대기 중", fontsize=15, fontweight='bold',
                          color=C_ACCENT, va='center', ha='center')

# ---- 모션 신호세기 미터 (세그먼트 / VU 미터 스타일) ----
N_SEG = 28
MAX_SCALE = TRIGGER_THRESHOLD * 2.0        # 미터 최대 스케일
THR_FRAC = TRIGGER_THRESHOLD / MAX_SCALE   # 트리거 임계 위치(0~1)
SEG_GAP = 0.32                             # 세그먼트 사이 간격 비율
SEG_DIM = '#222a35'                        # 꺼진 세그먼트 색

ax_head.text(0.625, 0.84, "MOTION", fontsize=9.5, color=C_SUBTEXT,
             va='center', ha='left', fontweight='bold')
motion_val = ax_head.text(1.0, 0.84, "0", fontsize=12, color=C_SUBTEXT,
                          va='center', ha='right', fontweight='bold')

ax_meter = ax_head.inset_axes([0.625, 0.10, 0.375, 0.46])
ax_meter.set_xlim(0, 1); ax_meter.set_ylim(0, 1)
ax_meter.axis('off')

seg_w = 1.0 / N_SEG
meter_segs = []
seg_zone_color = []
for i in range(N_SEG):
    frac = (i + 0.5) / N_SEG
    if frac < 0.55:
        zc = '#3fb950'        # 안정 구간 (초록)
    elif frac < THR_FRAC:
        zc = '#d29922'        # 경계 (주황)
    else:
        zc = '#f85149'        # 트리거 이상 (빨강)
    seg_zone_color.append(zc)
    rect = Rectangle((i * seg_w + seg_w * SEG_GAP / 2, 0.18),
                     seg_w * (1 - SEG_GAP), 0.64,
                     facecolor=SEG_DIM, edgecolor='none')
    ax_meter.add_patch(rect)
    meter_segs.append(rect)

# 트리거 임계 마커 (얇은 세로선 + 라벨)
ax_meter.axvline(THR_FRAC, color=C_TEXT, lw=1.0, alpha=0.55, zorder=5)
ax_meter.text(THR_FRAC, 1.18, "TRIG", fontsize=6.5, color=C_SUBTEXT,
              va='bottom', ha='center', clip_on=False)

# --- (좌) 3채널 실시간 신호 ---
ax_sig = style_panel(fig.add_subplot(gs[1, 0]))
ax_sig.set_title("실시간 신호  ·  3채널 (baseline 대비)", fontsize=12,
                 fontweight='bold', color=C_TEXT, loc='left', pad=10)
ax_sig.set_xlim(0, HIST_LEN)
ax_sig.set_ylim(-60, 420)
ax_sig.set_xlabel("프레임 (50 Hz)")
ax_sig.grid(True, color=C_GRID, alpha=0.6, linewidth=0.8)
ax_sig.axhline(0, color=C_SUBTEXT, alpha=0.4, lw=0.8)
sig_lines = {
    'Rx1': ax_sig.plot([], [], color=SIG_COLORS['Rx1'], lw=2.0, label='Rx1  왼쪽')[0],
    'Rx2': ax_sig.plot([], [], color=SIG_COLORS['Rx2'], lw=2.0, label='Rx2  중앙')[0],
    'Rx3': ax_sig.plot([], [], color=SIG_COLORS['Rx3'], lw=2.0, label='Rx3  오른쪽')[0],
}
leg = ax_sig.legend(loc='upper left', fontsize=9, framealpha=0.0,
                    labelcolor=C_TEXT, ncol=3)
# 녹화 중 표시용 배경 띠 (처음엔 숨김)
rec_band = ax_sig.axvspan(0, HIST_LEN, color='#ff7b72', alpha=0.0)

# --- (우상) 결과 카드 ---
ax_res = style_panel(fig.add_subplot(gs[1, 1]))
ax_res.set_xlim(0, 1); ax_res.set_ylim(0, 1)
ax_res.set_xticks([]); ax_res.set_yticks([])
ax_res.text(0.5, 0.9, "인식 결과", fontsize=11, color=C_SUBTEXT,
            va='center', ha='center')
res_mark = ax_res.text(0.5, 0.55, DIR_MARK["IDLE"], fontsize=68,
                       color=C_SUBTEXT, va='center', ha='center')
res_label = ax_res.text(0.5, 0.22, "READY", fontsize=26, fontweight='bold',
                        color=C_SUBTEXT, va='center', ha='center')
res_conf = ax_res.text(0.5, 0.08, "", fontsize=12, color=C_SUBTEXT,
                       va='center', ha='center')

# 결과 카드 테두리 강조용
res_border = FancyBboxPatch((0.02, 0.02), 0.96, 0.96,
                            boxstyle="round,pad=0.0,rounding_size=0.04",
                            linewidth=2.5, edgecolor=C_PANEL, facecolor='none',
                            transform=ax_res.transAxes)
ax_res.add_patch(res_border)

# --- (우하) 확률 막대 ---
# 우측 패널을 위/아래로 나누기 위해 결과 카드 아래에 별도 축을 인셋으로 배치
# (gridspec 2x2 에서는 우측 한 칸이므로, 결과축 영역을 위쪽에 두고 막대는 같은 칸 하단에 인셋)
ax_prob = ax_res.inset_axes([0.0, -0.62, 1.0, 0.5])
style_panel(ax_prob)
ax_prob.set_title("클래스별 확률", fontsize=11, color=C_SUBTEXT,
                  loc='left', pad=6)
ax_prob.set_xlim(0, 1.0)
y_pos = np.arange(len(CLASSES))
ax_prob.set_ylim(-0.6, len(CLASSES) - 0.4)
ax_prob.invert_yaxis()
prob_bars = ax_prob.barh(y_pos, [0] * len(CLASSES),
                         color=[CLASS_COLORS[c] for c in CLASSES], height=0.6)
ax_prob.set_yticks(y_pos)
ax_prob.set_yticklabels(CLASSES, color=C_TEXT, fontsize=10)
ax_prob.set_xticks([0, 0.5, 1.0])
ax_prob.set_xticklabels(["0%", "50%", "100%"])
ax_prob.grid(True, axis='x', color=C_GRID, alpha=0.5)
prob_texts = [ax_prob.text(0.012, i, "0.0%", va='center', ha='left',
                           fontsize=10, color=C_BG, fontweight='bold')
              for i in y_pos]

plt.ion()
plt.show(block=False)

# ===================== 상태 변수 =====================
buffer = deque(maxlen=SAMPLES_PER_GESTURE)
state = "WAITING"
post_trigger_count = 0
cooldown_count = 0

hist_sig = deque(maxlen=HIST_LEN)   # [dRx1, dRx2, dRx3]
live_variation = 0.0

print("실시간 제스처 자동 감지 + 시각화 가동.")
print(f"   [설정] 민감도(Trigger Threshold): {TRIGGER_THRESHOLD}  /  모델: {os.path.basename(model_path)}")
print("   화면 창에서 제스처를 시연하세요. (종료: 창 닫기 또는 Ctrl+C)")
print("-" * 56)
print("대기 중...")


def set_status(text, color):
    status_txt.set_text(text)
    status_txt.set_color(color)


def set_gauge(value):
    """변화량을 세그먼트 미터로 표시 (켜진 세그먼트는 구간색, 나머지는 어둡게)."""
    ratio = float(np.clip(value / MAX_SCALE, 0.0, 1.0))
    lit = int(round(ratio * N_SEG))
    for i, rect in enumerate(meter_segs):
        rect.set_facecolor(seg_zone_color[i] if i < lit else SEG_DIM)
    over = value > TRIGGER_THRESHOLD
    motion_val.set_text(f"{int(value)}")
    motion_val.set_color('#f85149' if over else C_SUBTEXT)


def update_signal():
    if len(hist_sig) == 0:
        return
    sig = np.array(hist_sig)
    x = np.arange(len(sig))
    sig_lines['Rx1'].set_data(x, sig[:, 0])
    sig_lines['Rx2'].set_data(x, sig[:, 1])
    sig_lines['Rx3'].set_data(x, sig[:, 2])
    ymax = max(420, float(np.max(sig)) * 1.15)
    ymin = min(-60, float(np.min(sig)) * 1.15)
    ax_sig.set_ylim(ymin, ymax)


def update_result(pred, cls, conf, accepted):
    for bar, p in zip(prob_bars, pred):
        bar.set_width(float(p))
    for txt, p in zip(prob_texts, pred):
        txt.set_text(f"{p * 100:.1f}%")

    if accepted:
        col = CLASS_COLORS.get(cls, C_ACCENT)
        res_mark.set_text(DIR_MARK.get(cls, '○')); res_mark.set_color(col)
        res_label.set_text(cls); res_label.set_color(col)
        res_conf.set_text(f"확신도 {conf * 100:.1f}%"); res_conf.set_color(C_TEXT)
        res_border.set_edgecolor(col)
    else:
        res_mark.set_text(DIR_MARK.get(cls, '○')); res_mark.set_color(C_SUBTEXT)
        res_label.set_text(cls if cls else "?"); res_label.set_color(C_SUBTEXT)
        res_conf.set_text(f"무시됨  ·  확신도 {conf * 100:.1f}%")
        res_conf.set_color(C_SUBTEXT)
        res_border.set_edgecolor(C_PANEL)


def show_recording(on):
    rec_band.set_alpha(0.10 if on else 0.0)


def redraw():
    fig.canvas.draw_idle()
    fig.canvas.flush_events()


def process_frame(f):
    """프레임 1개를 버퍼/히스토리/상태머신에 반영. (화면은 그리지 않음)"""
    global state, post_trigger_count, cooldown_count, live_variation

    buffer.append(list(f))
    d = np.array(f, dtype=np.float64) - base
    hist_sig.append([float(d[0]), float(d[1]), float(d[2])])

    if len(buffer) < SAMPLES_PER_GESTURE:
        return

    recent = np.array(list(buffer)[-5:])
    live_variation = float(np.max(np.ptp(recent, axis=0)))

    if state == "COOLDOWN":
        cooldown_count -= 1
        if cooldown_count <= 0:
            state = "WAITING"
            show_recording(False)
            set_status("대기 중", C_ACCENT)
            print("대기 중...")

    elif state == "WAITING":
        set_status("대기 중", C_ACCENT)
        if live_variation > TRIGGER_THRESHOLD:
            state = "RECORDING"
            post_trigger_count = POST_TRIGGER_FRAMES
            show_recording(True)
            print(f"움직임 감지 (Variation: {live_variation:.0f}) "
                  f"-> 잔여 {POST_TRIGGER_FRAMES}프레임 녹화 시작...")

    elif state == "RECORDING":
        post_trigger_count -= 1
        done = POST_TRIGGER_FRAMES - post_trigger_count
        set_status(f"녹화 중  {done}/{POST_TRIGGER_FRAMES}", '#ff7b72')
        if post_trigger_count <= 0:
            set_status("분석 중...", '#ffa657')
            capture = np.array(buffer, dtype=np.float64)

            # Per-Sample 정규화 (train_model.py 와 동일)
            baseline = np.mean(capture[:5], axis=0)
            norm_arr = capture - baseline
            max_val = np.max(np.abs(norm_arr))
            if max_val > 0:
                norm_arr = norm_arr / max_val

            pred = model.predict(
                norm_arr.reshape(1, SAMPLES_PER_GESTURE, NUM_CHANNELS),
                verbose=0)[0]
            idx = int(np.argmax(pred))
            cls = CLASSES[idx]
            conf = float(pred[idx])
            accepted = (cls != "IDLE" and conf > CONF_THRESHOLD)

            probs = "  ".join(f"{c} {p * 100:4.1f}%"
                              for c, p in zip(CLASSES, pred))
            if accepted:
                print(f"\n>>> 제스처 감지: [{cls}]   |  {probs}")
            else:
                print(f"\n(무시: {cls}, 확신도 부족)   |  {probs}")
            print("-" * 56)

            update_result(pred, cls, conf, accepted)
            show_recording(False)

            if DEBUG_SAVE:
                os.makedirs(DEBUG_DIR, exist_ok=True)
                fn = os.path.join(
                    DEBUG_DIR,
                    f"{cls}_{conf * 100:.0f}_{int(time.time() * 1000)}.csv")
                np.savetxt(fn, capture.astype(int), fmt='%d',
                           delimiter=',', header='Rx1,Rx2,Rx3',
                           comments='')

            state = "COOLDOWN"
            cooldown_count = COOLDOWN_FRAMES


DRAW_INTERVAL = 0.05   # 화면 갱신 주기 (초) — 약 20fps. 시리얼 읽기와 분리.
last_draw = 0.0

try:
    while True:
        if not plt.fignum_exists(fig.number):
            print("\n창이 닫혔습니다. 시스템을 종료합니다.")
            break

        # ----- (핵심) 들어와 있는 시리얼 프레임을 전부 소진해 지연 누적 방지 -----
        # 화면을 그리는 동안 쌓인 데이터를 한 번에 따라잡는다.
        drained = 0
        while ser.in_waiting > 0 and drained < 120:
            f = read_frame_blocking()
            if f is not None:
                process_frame(f)
                drained += 1

        # ----- 화면은 시간 기준으로만 갱신 (프레임 수와 무관) -----
        now = time.time()
        if now - last_draw >= DRAW_INTERVAL:
            update_signal()
            set_gauge(live_variation)
            redraw()
            last_draw = now

        plt.pause(0.001)   # GUI 이벤트 처리 + CPU 양보

except KeyboardInterrupt:
    print("\n시스템을 종료합니다.")
finally:
    if 'ser' in locals() and ser.is_open:
        ser.close()
    plt.ioff()
