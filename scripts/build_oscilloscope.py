"""
build_oscilloscope.py
=====================
音のサンプルそのものを線で描くオシロスコープ映像を組む。この映像を AI 変換
（自作 StreamDiffusion）に送り、別の素材に読み替える作品の映像ソースになる。

  osc_tone_l (440Hz) ─┐
  osc_tone_r (660Hz) ─┴ osc_tone ─┐
  aud_in（あれば）─────────────────┴ osc_src ─ osc_lr ─ osc_draw ─┬─ osc_comp ─ osc_glow ─ osc_bloom ─ osc_out
                     (0=テスト音 / 1=実音)  (l, r)  (Script TOP)    │  ↑ max
                                                                  osc_fb ─ osc_decay（残光）

3つの描き方（osc_ctrl の mode チャンネルで切り替え）
---------------------------------------------------
  0 XY（リサージュ）: x = 左, y = 右。ステレオの位相関係がそのまま図形になる。
    左440Hz・右660Hz（2:3）なら横3・縦2の山を持つ閉じた図形が止まって見える。
  1 波形: x = 時間, y = (左+右)/2。上向きゼロ交差を起点に描く（トリガ）ので、
    周期音なら波形が流れずに止まって見える。
  2 極座標: 角度 = 時間, 半径 = 0.55 + 0.4·振幅。音の揺れが円周のうねりになる。

なぜ Script TOP + cv2 で描くか
------------------------------
  CHOP to SOP → Geometry → Render の経路は線幅を出すのに Line MAT とカメラ設定が要り、
  ノードが増える。描くのは毎フレーム数百点の折れ線だけなので、numpy で座標を作って
  cv2.polylines（アンチエイリアス付き）で描く方が短く、線幅も1行で変えられる。
  cv2 は TD 同梱の Python で import できる（AI 送信の JPEG エンコードでも使う）。

  オーディオ系 CHOP はタイムスライスされ、1フレームぶんのサンプル（48kHz/60fps で
  約800点）だけを出す。オシロの「1画面ぶんの波形」にちょうど合う。

残光（蛍光体の尾）
------------------
  前フレームの結果（osc_fb）を osc_decay で少し暗くし、今フレームの線と max で
  重ねる。add だと重なった所が白く飛ぶので max にする。decay（osc_ctrl）が尾の長さ。

使い方
------
1. このスクリプトを実行（/project1 の osc_* ノードだけを作り直す。他のノードには触らない）
2. /project1/osc_out を見る。既定はテスト音・XY モード
3. 切り替え:
     op('/project1/osc_src').par.index = 1           # 実音（aud_in）へ
     op('/project1/osc_ctrl').par.value0 = 0|1|2     # mode: XY / 波形 / 極座標
   テスト音の周波数は osc_tone_l / osc_tone_r の frequency で変える
"""

import td

# --- 共有モジュール読込（td_param_bus=式の合成 / td_build=ノード構築） ---
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
RES = 512                 # AI 側は 384〜512 で送るので合わせる
TONE_L, TONE_R = 440.0, 660.0
X0, Y0 = -600, -1400      # ノード配置の基準（既存ネットワークの下に置く）
FLOAT_FMT = 'rgba16float'  # 残光ループのピクセル形式

# osc_ctrl の初期値: mode / gain（振幅→画面の倍率）/ thick（線幅px）/ decay（残光の残り率）
CTRL = {'mode': 0, 'gain': 0.85, 'thick': 2, 'decay': 0.88}

DRAW_CB = r'''
# Script TOP (/project1/osc_draw) コールバック: osc_lr の l/r を折れ線で描く
import numpy as np

SRC = '/project1/osc_lr'
CTRL = '/project1/osc_ctrl'
COLOR = (150, 255, 190, 255)        # 蛍光体の緑白（RGBA）


def _ctrl(name, default):
    c = op(CTRL)
    ch = c[name] if c is not None else None
    return ch.eval() if ch is not None else default


def _points(l, r, mode, gain, win):
    """-1..1 の (x, y) 配列を返す。win = 1画面に描くサンプル数。"""
    if mode == 1:                                   # 波形: 上向きゼロ交差でトリガ
        m = (l + r) * 0.5
        up = np.where((m[:-1] < 0) & (m[1:] >= 0))[0]
        half = win // 2                             # 残り半分はトリガ点を探す余地
        up = up[up + half <= len(m)]
        if len(up):
            # 和音は1周期に上向きゼロ交差が複数あり、どれで揃えるかがフレームごとに
            # 変わると波形が2重3重に見える。一番急な交差（周期ごとに同じ位相）に揃える
            slope = m[up + 1] - m[up]
            start = int(up[np.argmax(slope >= slope.max() * 0.95)])
        else:
            start = max(0, len(m) - half)
        m = m[start:start + half]
        x = np.linspace(-1.0, 1.0, len(m))
        return x, m * gain
    l, r = l[-win:], r[-win:]                       # 直近 win サンプル
    if mode == 2:                                   # 極座標
        m = (l + r) * 0.5
        th = np.linspace(0.0, 2.0 * np.pi, len(m))
        rad = 0.55 + 0.4 * m * gain
        return rad * np.cos(th), rad * np.sin(th)
    return l * gain, r * gain                       # XY


def onSetupParameters(scriptOp):
    return


def onCook(scriptOp):
    import cv2
    w = scriptOp.par.resolutionw.eval()
    h = scriptOp.par.resolutionh.eval()
    img = np.zeros((h, w, 4), dtype=np.uint8)
    src = op(SRC)
    if src is not None and src.numChans >= 1 and src.numSamples >= 2:
        l = src[0].numpyArray()
        r = src[1].numpyArray() if src.numChans >= 2 else l
        # 1画面 = 1/60 秒ぶん。タイムスライスの長さはクックの間隔で変わる
        # （取りこぼすと数千点になる）ので、描く量は固定する
        win = max(2, int(src.rate / 60))
        x, y = _points(l, r, int(_ctrl('mode', 0)), _ctrl('gain', 0.85), win)
        # TD の numpy 配列は下が0行目なので、y を上向きにそのまま行番号へ写す
        px = np.stack([(x * 0.5 + 0.5) * (w - 1), (y * 0.5 + 0.5) * (h - 1)], axis=1)
        pts = np.round(px).astype(np.int32).reshape(-1, 1, 2)
        cv2.polylines(img, [pts], False, COLOR, max(1, int(_ctrl('thick', 2))), cv2.LINE_AA)
    scriptOp.copyNumpyArray(img)
'''


def build_oscilloscope():
    p = tdb.get_parent(PARENT)
    has_aud = p.op('aud_in') is not None

    names = ['osc_tone_l', 'osc_tone_r', 'osc_tone', 'osc_src', 'osc_lr', 'osc_ctrl',
             'osc_draw_cb', 'osc_draw', 'osc_fb', 'osc_decay', 'osc_comp', 'osc_glow',
             'osc_bloom', 'osc_out']
    tdb.destroy(p, *names)

    # --- 音源: テスト音（左右別周波数）と実音を切り替える ---
    tl = tdb.ensure(p, 'osc_tone_l', 'audiooscillatorCHOP', X0, Y0,
                    pars={'frequency': TONE_L, 'amp': 0.8})
    tr = tdb.ensure(p, 'osc_tone_r', 'audiooscillatorCHOP', X0, Y0 - 100,
                    pars={'frequency': TONE_R, 'amp': 0.8})
    tone = tdb.ensure(p, 'osc_tone', 'mergeCHOP', X0 + 160, Y0 - 50, inputs=[tl, tr])
    src_inputs = [tone] + (['aud_in'] if has_aud else [])
    src = tdb.ensure(p, 'osc_src', 'switchCHOP', X0 + 320, Y0 - 50,
                     inputs=src_inputs, pars={'index': 0})
    # 入力側のチャンネル名（chan1/chan2 など）を l/r にそろえる。'*' に対して
    # renameto のリストを先頭から順に当てる
    lr = tdb.ensure(p, 'osc_lr', 'renameCHOP', X0 + 480, Y0 - 50, inputs=[src],
                    pars={'renamefrom': '*', 'renameto': 'l r'})

    # --- 操作値（constant CHOP の name0..3 / value0..3） ---
    ctrl = tdb.ensure(p, 'osc_ctrl', 'constantCHOP', X0 + 480, Y0 - 200)
    for i, (k, v) in enumerate(CTRL.items()):
        tdb.set_pars(ctrl, {f'name{i}': k, f'value{i}': v})

    # --- 描画（Script TOP） ---
    cb = tdb.ensure(p, 'osc_draw_cb', 'textDAT', X0 + 640, Y0 - 200, text=DRAW_CB)
    draw = tdb.ensure(p, 'osc_draw', 'scriptTOP', X0 + 640, Y0 - 50, res=(RES, RES),
                      pars={'callbacks': cb.path})

    # --- 残光: 前フレーム × decay と今フレームを max で重ねる ---
    #   8bit のままだと暗い画素が 4×0.88=3.52→4 と丸め戻されて消えず、前のモードの
    #   線がうっすら残り続ける。ループの3ノードは 16bit float にする
    fb = tdb.ensure(p, 'osc_fb', 'feedbackTOP', X0 + 800, Y0 - 200, inputs=[draw],
                    pars={'format': FLOAT_FMT})
    decay = tdb.ensure(p, 'osc_decay', 'levelTOP', X0 + 960, Y0 - 200, inputs=[fb],
                       pars={'brightness1': ('expr', "op('osc_ctrl')['decay']"),
                             'format': FLOAT_FMT})
    comp = tdb.ensure(p, 'osc_comp', 'compositeTOP', X0 + 960, Y0 - 50,
                      inputs=[draw, decay], pars={'operand': 'maximum', 'format': FLOAT_FMT})
    fb.par.top = comp.name

    # --- にじみ（グロー）を足す ---
    glow = tdb.ensure(p, 'osc_glow', 'blurTOP', X0 + 1120, Y0 - 200, inputs=[comp],
                      pars={'size': 12})
    bloom = tdb.ensure(p, 'osc_bloom', 'compositeTOP', X0 + 1120, Y0 - 50,
                       inputs=[comp, glow], pars={'operand': 'add'})
    out = tdb.ensure(p, 'osc_out', 'nullTOP', X0 + 1280, Y0 - 50, inputs=[bloom])

    print(f'[oscilloscope] build complete. view {out.path}. '
          f"source: osc_src.index 0=test tone {TONE_L:g}/{TONE_R:g}Hz"
          + (', 1=aud_in' if has_aud else ' (aud_in not found)')
          + "; mode: op('osc_ctrl').par.value0 = 0 XY / 1 wave / 2 polar")
    return out


if __name__ == '__main__':
    build_oscilloscope()
