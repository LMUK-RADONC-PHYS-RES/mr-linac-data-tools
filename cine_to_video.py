#!/usr/bin/env python3
"""
Turn the cine stacks written by decode_message_dump.py into MP4 videos.

All planes of the same run (``cine_plane*_g<N>_*.mha``) are shown side by side on a
common time axis: the video is sampled at a fixed output rate and every panel shows
the most recent frame acquired for that plane, so playback follows real acquisition
time (scaled by --speed).

Requires: numpy, Pillow, imageio-ffmpeg  (pip install imageio-ffmpeg — bundles ffmpeg)

    python3 cine_to_video.py data_test/a3i/output_conversion/cine
    python3 cine_to_video.py data_test/a3i/output_conversion/cine --speed 4
"""
import argparse
import glob
import os
import re
from collections import defaultdict

import numpy as np
from PIL import Image, ImageDraw, ImageFont

try:
    import imageio_ffmpeg
except ImportError:
    raise SystemExit('imageio-ffmpeg is required:  pip install imageio-ffmpeg')

ORIENT = {0: 'sagittal', 1: 'coronal', 2: 'axial'}


def read_mha(path):
    data = open(path, 'rb').read()
    end = data.index(b'ElementDataFile = LOCAL\n') + len('ElementDataFile = LOCAL\n')
    hdr = dict(l.split(' = ', 1) for l in data[:end].decode().splitlines())
    nx, ny, nz = map(int, hdr['DimSize'].split())
    tm = np.array(hdr['TransformMatrix'].split(), float).reshape(3, 3)  # rows = axis directions
    arr = np.frombuffer(data[end:], '<u2').reshape(nz, ny, nx)
    normal = tm[2]
    orient = ORIENT.get(int(np.argmax(np.abs(normal))), 'oblique')
    return arr, orient


def to_rgb(frame, lo, hi, scale):
    g = np.clip((frame.astype(np.float32) - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
    im = Image.fromarray(g).resize((frame.shape[1] * scale, frame.shape[0] * scale), Image.BICUBIC)
    return im.convert('RGB')


def make_video(stacks, out_path, speed=1.0, out_fps=25, panel_px=480):
    """stacks: list of (label, frames[n,h,w], timestamps_us[n])."""
    t0 = min(ts[0] for _, _, ts in stacks)
    t1 = max(ts[-1] for _, _, ts in stacks)
    scale = max(1, round(panel_px / max(a.shape[1] for _, a, _ in stacks)))
    windows = [np.percentile(a, [0.5, 99.5]) for _, a, _ in stacks]
    pw = max(a.shape[2] for _, a, _ in stacks) * scale
    ph = max(a.shape[1] for _, a, _ in stacks) * scale
    bar = 34
    W = -(-pw * len(stacks) // 16) * 16
    H = -(-(ph + 2 * bar) // 16) * 16
    try:
        font = ImageFont.load_default(size=18)
    except TypeError:
        font = ImageFont.load_default()

    n_out = int((t1 - t0) / 1e6 / speed * out_fps) + 1
    writer = imageio_ffmpeg.write_frames(out_path, (W, H), fps=out_fps, codec='libx264',
                                         pix_fmt_out='yuv420p', quality=8, macro_block_size=16)
    writer.send(None)
    cache = {}
    for k in range(n_out):
        t = t0 + k * speed / out_fps * 1e6
        canvas = Image.new('RGB', (W, H), (0, 0, 0))
        d = ImageDraw.Draw(canvas)
        for i, ((label, arr, ts), (lo, hi)) in enumerate(zip(stacks, windows)):
            j = int(np.searchsorted(ts, t, side='right')) - 1
            x0 = i * pw
            if j < 0:
                d.text((x0 + 8, bar + 8), f'{label}: waiting…', fill=(160, 160, 160), font=font)
                continue
            key = (i, j)
            if key not in cache:
                cache = {kk: v for kk, v in cache.items() if kk[0] != i}
                cache[key] = to_rgb(arr[j], lo, hi, scale)
            canvas.paste(cache[key], (x0, bar))
            d.text((x0 + 8, 8), f'{label}   frame {j + 1}/{len(ts)}', fill=(255, 255, 255), font=font)
        d.text((8, bar + ph + 8), f't = {(t - t0) / 1e6:7.2f} s   ({speed:g}x)',
               fill=(255, 220, 120), font=font)
        writer.send(np.asarray(canvas).tobytes())
    writer.close()
    print(f'  {out_path}: {n_out} frames, {n_out / out_fps:.1f} s, {W}x{H}')


def main(argv=None, prog=None):
    ap = argparse.ArgumentParser(prog=prog, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('cine_dir', help='cine/ folder written by decode_message_dump.py')
    ap.add_argument('--out', help='output folder (default: <cine_dir>/../videos)')
    ap.add_argument('--speed', type=float, default=1.0, help='playback speed (1 = real time)')
    ap.add_argument('--fps', type=int, default=25, help='output video frame rate')
    a = ap.parse_args(argv)
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.cine_dir.rstrip('/'))), 'videos')
    os.makedirs(out, exist_ok=True)

    runs = defaultdict(list)
    for f in sorted(glob.glob(os.path.join(a.cine_dir, 'cine_plane*_g*_*.mha'))):
        plane, g = map(int, re.search(r'cine_plane(\d+)_g(\d+)_', os.path.basename(f)).groups())
        arr, orient = read_mha(f)
        ts = np.load(f.replace('.mha', '_timestamps_us.npy'))
        runs[g].append((f'plane {plane} ({orient})', arr, ts))
    if not runs:
        raise SystemExit(f'No cine .mha files found in {a.cine_dir}')
    for g, stacks in sorted(runs.items()):
        tag = '_'.join(s[0].split()[1] for s in stacks)
        make_video(stacks, os.path.join(out, f'cine_run{g}_planes_{tag}_{a.speed:g}x.mp4'),
                   speed=a.speed, out_fps=a.fps)


if __name__ == '__main__':
    main()
