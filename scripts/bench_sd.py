# GPU サーバーの StreamDiffusion ディレクトリに置いて実行する: CUDA_VISIBLE_DEVICES=1 ../.venv/bin/python bench_sd.py --cfg full
# sd_ws_server と同じ初期化で、1枚あたりの時間を 復号/推論/符号化 に分けて測る
import sys, time, io, base64, statistics as st
sys.path.insert(0, '.')
import torch
from PIL import Image, ImageDraw
from utils.wrapper import StreamDiffusionWrapper
import argparse
ap = argparse.ArgumentParser()
ap.add_argument('--cfg', default='full'); ap.add_argument('--size', type=int, default=384)
ap.add_argument('--accel', default='xformers'); ap.add_argument('--n', type=int, default=40)
a = ap.parse_args()
s = StreamDiffusionWrapper(model_id_or_path='stabilityai/sd-turbo', t_index_list=[16, 26], frame_buffer_size=1,
    width=a.size, height=a.size, warmup=10, acceleration=a.accel, mode='img2img', use_denoising_batch=True,
    cfg_type=a.cfg, seed=2, output_type='pil')
s.prepare(prompt='intricate glowing neon wire sculpture', negative_prompt='low quality', num_inference_steps=50, guidance_scale=5)
img = Image.new('RGB', (a.size, a.size)); ImageDraw.Draw(img).ellipse((60, 60, 320, 320), outline=(120, 255, 200), width=4)
buf = io.BytesIO(); img.save(buf, 'JPEG', quality=80); b64 = base64.b64encode(buf.getvalue())
D, I, E = [], [], []
for i in range(a.n):
    t0 = time.perf_counter(); im = Image.open(io.BytesIO(base64.b64decode(b64))).convert('RGB'); t1 = time.perf_counter()
    out = s(image=im); torch.cuda.synchronize(); t2 = time.perf_counter()
    o = io.BytesIO(); (out if isinstance(out, Image.Image) else out[0]).save(o, 'JPEG', quality=50); t3 = time.perf_counter()
    if i >= 5: D.append(t1 - t0); I.append(t2 - t1); E.append(t3 - t2)
ms = lambda x: f'{st.median(x)*1000:.0f}ms(p90 {sorted(x)[int(len(x)*.9)]*1000:.0f})'
t5 = time.perf_counter()
for _ in range(5): s.prepare(prompt='intricate glowing neon wire sculpture', negative_prompt='low quality', num_inference_steps=50, guidance_scale=5)
torch.cuda.synchronize()
print(f'RESULT cfg={a.cfg} size={a.size} accel={a.accel} decode={ms(D)} infer={ms(I)} encode={ms(E)} prepare={(time.perf_counter()-t5)/5*1000:.0f}ms threads={torch.get_num_threads()}', flush=True)
