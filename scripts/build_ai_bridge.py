"""
build_ai_bridge.py
==================
オシロスコープ映像 osc_out を自作 StreamDiffusion サーバー
（streamdiffusion-ws, ws://localhost:8765）へ base64 JPEG で送り、AI 変換結果を
Script TOP `ai_out` に受けて表示する。さらに**音の大きさで StreamDiffusion の
t_index を変調**する（音が大きいほど AI の読み替えが強まる）。

ryok/td-organic-patterns の scripts/build_ai_bridge.py の移植。違いは2点:
  - 送信元: out1（油膜）→ osc_out（オシロスコープ）
  - 音の大きさ: aud_lag（帯域ごとのスペクトル平均）→ ai_energy（osc_lr の RMS を
    Lag でならしたもの）。オシロが描いている音そのものの大きさで変調する

前提:
  build_oscilloscope.py を実行済み（osc_out, osc_lr が在る）。
  GPU 側で streamdiffusion-ws が起動し、Mac 側で SSH トンネル(8765)が通っていること。
  サーバーは --t-index 22 32（2要素）で起動している前提（TINDEX は要素数一致が必須）。
  ★重要（S5/録画で判明）: サーバー起動は以下2点が必須。
  (1) **--guidance-scale 5 --cfg-type full**（2026-09-11）: 既定 guidance_scale=1.2 だと
      プロンプトが拡散に効かず出力が茶色い無地に潰れる。CFGを上げて初めてプロンプトが
      絵を支配し、油膜が虹色オイルスリックに変換される。
  (2) **--size 384 --out-quality 50**（2026-09-14）: 返信JPEGが大きい(512/q85で52-107KB)と
      TDのWebSocket DATが数フレームで受信不能になり映像/録画が静止する（サーバは送信
      できていて受信側の詰まり）。384/q50で~33KBに縮めると継続受信が安定する。
  推奨起動（GPU 番号は空きを nvidia-smi で見て選ぶ）:
    CUDA_VISIBLE_DEVICES=2 python -u sd_ws_server.py --size 384 --acceleration xformers \
      --t-index 22 32 --guidance-scale 5 --cfg-type full --out-quality 50 \
      --prompt "..."

構成（hand-trail-ai の TD 側 3 点セットを移植）:
  websocket1(WebSocket DAT, client) ── callbacks → websocket1_callbacks1(Text DAT)
  ai_out(Script TOP) ── callbacks → ai_out_callbacks(Text DAT)   受信 JPEG をデコード表示
  ai_energy_*(CHOP)      osc_lr → RMS → Lag → ai_energy（音の大きさ）
  ai_driver(Execute DAT)  毎フレーム osc_out を force cook + 送信ドライバ（音の大きさ→TINDEX 変調）

再実行安全: 追加ノードは同名を削除してから作り直す。active は 0 で作るので、
ビルド後に別フレームで websocket1.par.active=1 して接続する
（WebSocket DAT の active は「接続状態」でなく有効フラグ。同一スクリプト内トグルは無効）。
"""

import td

# --- 共有モジュール読込（td_build=ノード構築） ---
import importlib, os, sys
try:
    _SD = os.path.dirname(os.path.abspath(__file__))
except NameError:                                   # Textport へのペースト時
    _SD = os.environ.get('TD_OSC_SCRIPTS',
                         '/Users/ryookada/work/td-oscilloscope-ai/scripts')
if _SD not in sys.path:
    sys.path.insert(0, _SD)
import td_build as tdb
importlib.reload(tdb)

PARENT = '/project1'
WS_ADDR = 'localhost'
WS_PORT = 8765

# --- WebSocket 送受信コールバック（in-flight ガード + 音響 TINDEX 送信） ---------
CALLBACKS_SRC = r'''
# WebSocket DAT (/project1/websocket1) コールバック
#   - osc_out を base64 JPEG にして送信 (send_frame)
#   - in-flight ガード: 返信待ち中は送らない（サーバーを溢れさせない）
#   - send_tindex: 音響駆動の t_index をライブ送信（同じガード下）
#   - 受信 base64 JPEG を保持し Script TOP(ai_out) を再クック
#   - エンコードは cv2（TD の PIL は無関係 venv を掴んで dlopen 失敗するため）
import base64

SRC_TOP = '/project1/osc_out'
DST_TOP = '/project1/ai_out'
SIZE = 512

_latest_jpg_b64 = None
_inflight = False
_sent_at = 0.0
TIMEOUT = 3.0
_last_tindex = (22, 32)

# --- 送受信の回数（README の fps の説明を実測で確かめるため） ---
# sent_*: 実際に送れた回数 / skip: 返信待ちで送れなかったスロット / timeout: 返信が来ず枠を開けた回数
# recv_*: 受け取った回数（img=画像、ok=OK:〜、err=ERR:〜）/ rtt_*: 送信から返信までの秒数（全種類）
stats = {}
_stats_t0 = 0.0


def stats_reset():
    global _stats_t0
    stats.clear()
    stats.update({'sent_img': 0, 'sent_tindex': 0, 'sent_prompt': 0, 'skip': 0, 'timeout': 0,
                  'recv_img': 0, 'recv_ok': 0, 'recv_err': 0, 'rtt_sum': 0.0, 'rtt_n': 0})
    _stats_t0 = absTime.seconds


def stats_report():
    """stats_reset() からの回数と、1秒あたりの回数・返信の平均秒数を返す。"""
    sec = max(absTime.seconds - _stats_t0, 1e-6)
    r = dict(stats)
    r['sec'] = round(sec, 2)
    sent = r['sent_img'] + r['sent_tindex'] + r['sent_prompt']
    r['sent_per_s'] = round(sent / sec, 2)
    r['img_per_s'] = round(r['recv_img'] / sec, 2)
    r['tindex_share'] = round(r['sent_tindex'] / sent, 3) if sent else None
    r['rtt_mean_ms'] = round(r['rtt_sum'] / r['rtt_n'] * 1000, 1) if r['rtt_n'] else None
    return r


stats_reset()


def _count(key):
    stats[key] = stats.get(key, 0) + 1


def _encode(top, size=SIZE):
    import cv2
    import numpy as np
    a = top.numpyArray(delayed=False)                 # float32 RGBA 0..1, 左下原点
    rgb = (np.flipud(a)[:, :, :3] * 255).clip(0, 255).astype('uint8')
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    h, w = bgr.shape[:2]
    s = min(h, w)
    y0, x0 = (h - s) // 2, (w - s) // 2
    bgr = bgr[y0:y0 + s, x0:x0 + s]
    if s != size:
        bgr = cv2.resize(bgr, (size, size))
    ok, enc = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return base64.b64encode(enc.tobytes()).decode('ascii')


def _guard_open():
    global _inflight
    now = absTime.seconds
    if _inflight:
        if now - _sent_at < TIMEOUT:
            _count('skip')
            return False
        debug('[ws] response timeout, releasing inflight')
        _count('timeout')
        _inflight = False
    return True


def send_frame(dat):
    global _inflight, _sent_at
    if not _guard_open():
        return
    try:
        dat.sendText(_encode(op(SRC_TOP)))
        _inflight = True
        _sent_at = absTime.seconds
        _count('sent_img')
    except Exception as e:
        debug('send_frame err:', e)


def send_tindex(dat, a, b):
    global _inflight, _sent_at, _last_tindex
    if not _guard_open():
        return
    try:
        dat.sendText('TINDEX:%d,%d' % (a, b))
        _inflight = True
        _sent_at = absTime.seconds
        _last_tindex = (a, b)
        _count('sent_tindex')
    except Exception as e:
        debug('send_tindex err:', e)


def last_ta():
    return _last_tindex[0]


# --- プロンプト差し替え（build_sections.py が使う） ---
# 画像・TINDEX と同じ in-flight 枠を使うので、ここでは予約だけして、ai_driver が
# 次の送信スロットで最優先に送る（返信待ち中に直接送ると取りこぼす）。
_pending_prompt = None
_prompt_sent_at = None
prompt_rtt = []          # 送信から OK:prompt updated までの秒数（切り替え時の止まりの実測）


def request_prompt(text):
    global _pending_prompt
    _pending_prompt = text


def has_pending_prompt():
    return _pending_prompt is not None


def send_pending_prompt(dat):
    global _inflight, _sent_at, _pending_prompt, _prompt_sent_at
    if _pending_prompt is None or not _guard_open():
        return
    try:
        dat.sendText('PROMPT:' + _pending_prompt)
        _inflight = True
        _sent_at = _prompt_sent_at = absTime.seconds
        _pending_prompt = None
        _count('sent_prompt')
    except Exception as e:
        debug('send_prompt err:', e)


def onConnect(dat):
    global _inflight
    _inflight = False
    debug('[ws] connected', dat.par.netaddress.eval(), dat.par.port.eval())


def onDisconnect(dat):
    global _inflight
    _inflight = False
    debug('[ws] disconnected')


def onReceiveText(dat, rowIndex, message, *args):
    global _latest_jpg_b64, _inflight
    global _prompt_sent_at
    if _inflight:
        stats['rtt_sum'] = stats.get('rtt_sum', 0.0) + absTime.seconds - _sent_at
        _count('rtt_n')
    _inflight = False
    if message.startswith('OK:') or message.startswith('ERR:'):
        _count('recv_ok' if message.startswith('OK:') else 'recv_err')
        if message.startswith('OK:prompt') and _prompt_sent_at is not None:
            prompt_rtt.append(absTime.seconds - _prompt_sent_at)
            _prompt_sent_at = None
        debug('[ws] server:', message)
        return
    _count('recv_img')
    _latest_jpg_b64 = message
    d = op(DST_TOP)
    if d is not None:
        d.cook(force=True)


def onReceiveBinary(dat, contents):
    return


def onReceivePing(dat, contents):
    dat.sendPong(contents)


def onReceivePong(dat, contents):
    return


def onMonitorMessage(dat, message):
    return
'''

# --- Script TOP ai_out: 受信 base64 JPEG を cv2 でデコードして出力 --------------
AI_OUT_SRC = r'''
import base64

CB_PATH = '/project1/websocket1_callbacks1'


def onCook(scriptOp):
    cb = op(CB_PATH)
    b64 = getattr(cb.module, '_latest_jpg_b64', None) if cb else None
    if not b64:
        import numpy as np
        scriptOp.copyNumpyArray(np.zeros((512, 512, 4), np.float32))
        return
    try:
        import cv2
        import numpy as np
        raw = base64.b64decode(b64)
        bgr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB).astype('float32') / 255.0
        rgb = np.flipud(rgb)
        rgba = np.dstack([rgb, np.ones(rgb.shape[:2], 'float32')])
        scriptOp.copyNumpyArray(rgba)
    except Exception as e:
        debug('ai_out onCook err:', e)
'''

# --- Execute DAT ai_driver: 毎フレーム osc_out を force cook + 送信（音の大きさ→TINDEX） --
DRIVER_SRC = r'''
# 1) 毎フレーム osc_out を force cook（残光のフィードバックを送信可否と独立に進める）
# 2) websocket1 が Active なら EVERY フレーム毎に 1 送信スロット。
#    音響エネルギー→t_index を算出し、有意変化かつ制御スロットなら TINDEX を、
#    それ以外は画像フレームを送る（1スロット1送信＝in-flightガードと衝突しない）。
WS = '/project1/websocket1'
CB = '/project1/websocket1_callbacks1'
SRC = '/project1/osc_out'
AUD = '/project1/ai_energy'
EVERY = 3          # 送信を試みる間隔（フレーム）。ガードがあるので小さくてよい
# E_FLOOR〜E_FULL の RMS を 0..1 に写す（E_FLOOR 以下=最弱、E_FULL 以上=最強）。
# ★実音量に必ず合わせること（td-organic-patterns の教訓）。波形そのものの RMS なので、
# organic-patterns のスペクトル平均（0.003〜0.03）より桁が大きい。
# 2026-10-08 実曲（Interactive-MV/music.wav）12秒の実測: p5=0.13 / 中央値=0.27 / p95=0.39。
# 0 起点で写すと静かな所でも半分以上の強さになるので、下限を p5 付近に置く。
E_FLOOR = 0.12
E_FULL = 0.4
# t_index レンジ（2026-10-08 テスト音 440/660Hz の XY で調整、guidance-scale 5 前提）:
#   (22,32) / (12,22): 黒地の細い線はほぼそのまま返る（油膜より入力の情報が少ないため、
#                      organic-patterns の範囲では読み替えが起きない）
#   (10,20): 線の形を保ったままネオン管の質感になり、床に反射が出る（形が読める上限）
#   (6,16) : 形が崩れて別の物（ネオンの花）になる
# 無音=形が読める側(14,24) → 大音量=形が崩れる側(6,16)。音が大きい所で図形が溶ける。
TA_BASE, TA_MIN = 14, 6    # t_index[0]
TB_BASE, TB_MIN = 24, 16   # t_index[1]
# build_sections.py があれば、セクションごとの範囲（sec_state の ta_q/tb_q=無音側、
# ta_l/tb_l=大音量側）で上の既定を置き換える
SEC = '/project1/sec_state'


def _clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


def onFrameStart(frame):
    src = op(SRC)
    if src is not None:
        src.cook(force=True)
    ws = op(WS)
    if ws is None or not ws.par.active.eval():
        return
    if frame % EVERY != 0:
        return
    cb = op(CB).module
    # 音の大きさ（osc_lr の RMS をならしたもの）→ 0..1 正規化
    e = 0.0
    al = op(AUD)
    if al is not None and al.numChans:
        e = al[0].eval()
    n = _clamp((e - E_FLOOR) / (E_FULL - E_FLOOR), 0.0, 1.0)
    taq, tal, tbq, tbl = TA_BASE, TA_MIN, TB_BASE, TB_MIN
    sec = op(SEC)
    if sec is not None:
        taq, tal = sec['ta_q'].eval(), sec['ta_l'].eval()
        tbq, tbl = sec['tb_q'].eval(), sec['tb_l'].eval()
    ta = int(round(taq - (taq - tal) * n))
    tb = int(round(tbq - (tbq - tbl) * n))
    # セクション切り替えのプロンプトは最優先（予約があれば今回のスロットはそれだけ送る）
    if cb.has_pending_prompt():
        cb.send_pending_prompt(ws)
        return
    # 画像フレームを主に流し、TINDEXは数スロットに1回だけ相乗りさせる。
    # ★2026-09-11の録画で判明: 画像とTINDEXは同じin-flight枠を1スロット1回で共有する。
    #   毎回TINDEXを送ると（大音量でenergyが揺れ changed がほぼ毎回true）画像が枯れ、
    #   ai_out が更新されず映像が静止する。slot%4 で TINDEX を1/4に間引き、画像3/4を確保。
    #   「変調が乗らない」ときはこのゲートでなく E_FULL の校正（実音量に合わせる）で直す。
    slot = frame // EVERY
    changed = abs(ta - cb.last_ta()) >= 2
    if changed and slot % 4 == 0:
        cb.send_tindex(ws, ta, tb)
    else:
        cb.send_frame(ws)


def onFrameEnd(frame):
    return


def onStart():
    return


def onCreate():
    return


def onExit():
    return
'''


def build_ai_bridge():
    p = tdb.get_parent(PARENT)
    tdb.require(p, 'osc_out', 'osc_lr', hint='build_oscilloscope.py')

    # 冪等: 既存を先にまとめて掃除
    tdb.destroy(p, 'ai_driver', 'websocket1', 'websocket1_callbacks1', 'ai_out',
                'ai_out_callbacks', 'ai_energy_rms', 'ai_energy_mix', 'ai_energy')

    # 音の大きさ: 左右それぞれの RMS → 平均 → Lag（上がりは速く、下がりはゆっくり）
    rms = tdb.ensure(p, 'ai_energy_rms', 'analyzeCHOP', -600, 480, inputs=['osc_lr'],
                     pars={'function': 'rmspower'})
    mix = tdb.ensure(p, 'ai_energy_mix', 'mathCHOP', -440, 480, inputs=[rms],
                     pars={'chanop': 'avg'})   # 左右を1チャンネルに平均
    tdb.ensure(p, 'ai_energy', 'lagCHOP', -280, 480, inputs=[mix],
               pars={'lag1': 0.05, 'lag2': 0.4})

    # コールバック Text DAT
    cb = tdb.ensure(p, 'websocket1_callbacks1', 'textDAT', -600, 200, text=CALLBACKS_SRC)

    # WebSocket DAT（client）— active=0 で作成（後で別フレームに 1 して接続）。
    # callbacks は OP 参照パラメータ。相対パス './...' は解決されず None になるため
    # 絶対パスで設定する（2026-09-11 実機で確認）。
    ws = tdb.ensure(p, 'websocket1', 'websocketDAT', -420, 200,
                    pars={'netaddress': WS_ADDR, 'port': WS_PORT, 'active': False,
                          'callbacks': PARENT + '/websocket1_callbacks1'})

    # ai_out（Script TOP）+ その callbacks DAT
    tdb.ensure(p, 'ai_out_callbacks', 'textDAT', -600, 60, text=AI_OUT_SRC)
    ao = tdb.ensure(p, 'ai_out', 'scriptTOP', -420, 60,
                    pars={'callbacks': PARENT + '/ai_out_callbacks'})

    # ai_driver（Execute DAT）: 本文を入れてから active/framestart を ON
    drv = tdb.ensure(p, 'ai_driver', 'executeDAT', -420, 340, text=DRIVER_SRC,
                     pars={'active': True, 'framestart': True})

    print('[ai_bridge] build complete. '
          'Ensure GPU server + SSH tunnel(8765) are up, then set '
          "op('/project1/websocket1').par.active = 1 (別フレームで) to connect. "
          'View /project1/ai_out.')
    return dict(ws=ws, cb=cb, ai_out=ao, driver=drv)


if __name__ == '__main__':
    build_ai_bridge()
