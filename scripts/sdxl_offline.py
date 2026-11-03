"""オシロスコープ映像を SDXL + ControlNet でオフライン変換する（S6）。

リアルタイム版（StreamDiffusion, 384px, 約7fps）と同じ曲・同じセクションのプロンプトで、
1枚ずつ時間をかけて描かせたらどう違うかを比べるためのもの。uisato の Oscilloscope Diffusion
（SDXL のオフライン video-to-video）に相当する。

入力: s5_final_pair.mp4 の左半分（osc_out そのもの）を連番 PNG にしたもの（src/00001.png〜、30fps）
出力: out/<名前>/00001.png〜（同じ番号。--step 2 なら奇数番号だけ）

時間方向のちらつき対策は「セクションごとに初期ノイズ（latents）を固定する」だけにしている。
同じノイズから、線の形（ControlNet）だけが変わる画像を描かせるので、色や構図はセクション内で揃いやすい。

使い方（GPU サーバー上）:
  CUDA_VISIBLE_DEVICES=2 .venv/bin/python sdxl_offline.py --pilot          # セクションごとに数枚だけ
  CUDA_VISIBLE_DEVICES=0 .venv/bin/python sdxl_offline.py --start 1 --end 900 --step 2
"""
import argparse
import os
import time

import cv2
import numpy as np
import torch
from PIL import Image

FPS = 30
# 録画のフレーム番号（1始まり）でのセクション境界。88BPM・8小節 = 21.82秒ごとだが、
# 録画では約0.1秒遅れて切り替わっていた（モードが変わるコマを目で確認: 21.85s / 43.7s / 65.6s）
BOUNDS = [1, 656, 1311, 1968]

# scripts/build_sections.py の SECTIONS と同じプロンプト（比較のため揃える）
SECTIONS = [
    ('intro', 'intricate glowing neon wire sculpture, liquid chrome filaments, dark background, '
              'cinematic light, highly detailed'),
    ('build', 'folded white origami paper sculpture, sharp creases, soft studio light, minimal, highly detailed'),
    ('peak', 'molten iridescent liquid metal splash, rainbow chrome, macro photography, dramatic light'),
    ('break', 'sumi-e ink brush stroke on rice paper, black ink, minimal, japanese calligraphy'),
]
NEGATIVE = 'blurry, low quality, text, watermark, signature, frame, border'
PILOT = [200, 450, 800, 1100, 1500, 1800, 2100, 2500]


def section_of(n):
    s = 0
    for i, b in enumerate(BOUNDS):
        if n >= b:
            s = i
    return s


def control_image(path, size, mode, gamma):
    g = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if mode == 'canny':
        g = cv2.Canny(g, 60, 160)
    else:
        # 線そのものを白線として使う。にじみ（bloom）を gamma で落として芯だけ残す
        g = (np.power(g.astype(np.float32) / 255.0, gamma) * 255.0).clip(0, 255).astype(np.uint8)
    g = cv2.resize(g, (size, size), interpolation=cv2.INTER_CUBIC)
    return Image.fromarray(np.stack([g] * 3, axis=-1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', default='src')
    ap.add_argument('--out', default='out/run')
    ap.add_argument('--start', type=int, default=1)
    ap.add_argument('--end', type=int, default=2646)
    ap.add_argument('--step', type=int, default=1)
    ap.add_argument('--pilot', action='store_true')
    ap.add_argument('--size', type=int, default=1024)
    ap.add_argument('--steps', type=int, default=25)
    ap.add_argument('--cfg', type=float, default=6.0)
    ap.add_argument('--cond-scale', type=float, default=0.8)
    ap.add_argument('--control', choices=['line', 'canny'], default='canny')
    ap.add_argument('--gamma', type=float, default=1.8)
    ap.add_argument('--seed', type=int, default=1234)
    a = ap.parse_args()

    from diffusers import AutoencoderKL, ControlNetModel, StableDiffusionXLControlNetPipeline

    dt = torch.float16
    cn = ControlNetModel.from_pretrained('diffusers/controlnet-canny-sdxl-1.0', variant='fp16', torch_dtype=dt)
    vae = AutoencoderKL.from_pretrained('madebyollin/sdxl-vae-fp16-fix', torch_dtype=dt)
    pipe = StableDiffusionXLControlNetPipeline.from_pretrained(
        'stabilityai/stable-diffusion-xl-base-1.0', controlnet=cn, vae=vae, variant='fp16', torch_dtype=dt,
    ).to('cuda')
    pipe.set_progress_bar_config(disable=True)

    os.makedirs(a.out, exist_ok=True)
    frames = PILOT if a.pilot else list(range(a.start, a.end + 1, a.step))

    # セクションごとに初期ノイズを固定する
    lat_shape = (1, 4, a.size // 8, a.size // 8)
    latents = [torch.randn(lat_shape, generator=torch.Generator('cpu').manual_seed(a.seed + i), dtype=torch.float32)
               .to('cuda', dt) for i in range(len(SECTIONS))]

    times = []
    for n in frames:
        dst = os.path.join(a.out, '%05d.png' % n)
        if os.path.exists(dst):
            continue
        s = section_of(n)
        ctrl = control_image(os.path.join(a.src, '%05d.png' % n), a.size, a.control, a.gamma)
        t0 = time.time()
        img = pipe(
            prompt=SECTIONS[s][1], negative_prompt=NEGATIVE, image=ctrl,
            num_inference_steps=a.steps, guidance_scale=a.cfg,
            controlnet_conditioning_scale=a.cond_scale,
            latents=latents[s].clone(), width=a.size, height=a.size,
        ).images[0]
        times.append(time.time() - t0)
        img.save(dst)
        if a.pilot:
            ctrl.save(os.path.join(a.out, '%05d_ctrl.png' % n))
        print('frame %05d %s %.2fs' % (n, SECTIONS[s][0], times[-1]), flush=True)

    if times:
        t = np.array(times[1:] if len(times) > 1 else times)
        print('DONE n=%d median=%.2fs/frame' % (len(times), float(np.median(t))), flush=True)


if __name__ == '__main__':
    main()
