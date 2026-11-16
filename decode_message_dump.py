#!/usr/bin/env python3
"""
Decoder for ViewRay MRIdian ``Message.dump`` files (pre-A3i and A3i).

File layout (reverse engineered, little endian throughout)
=========================================================
The file is a flat stream of records, no global header:

    uint32  record_len          # bytes following this field (= 20 + payload_len)
    uint32  hdr_size            # always 12
    uint8   flag                # 0 = request/data, 1 = reply/ack
    uint8   category            # 1 = MR imaging, 2 = treatment/tracking, 3 = heartbeat, 255 = version
    uint16  msg_type
    uint64  timestamp_us        # unix epoch, microseconds
    uint32  payload_len
    uint8   payload[payload_len]

Image payloads (msg (0,1,4) = 3D volume slice, msg (0,1,3) = 2D cine frame) all share
a 195-byte geometry header, followed by an image block whose pixels sit at the end:

    off  0  int32   195 (geometry header size)
    off  4  uint8   x3  flags (byte 5: 1 = 3D slice, 0 = cine)
    off  7  double  P1[3]          P2 minus half a pixel along row_dir and col_dir (mm)
    off 31  double  P2[3]          centre of the field of view (mm); equals the plane position
                                   in the scan XML. Verified by reslicing the 3D volume along the
                                   cine plane (a 'pixel (0,0)' origin puts the plane outside the volume)
    off 55  double  normal[3]      slice normal
    off 79  double  aux[3]
    off 103 double  row_dir[3]     direction of increasing column index
    off 127 double  col_dir[3]     direction of increasing row index
    off 151 double  slice_thickness, pixel_spacing_col, pixel_spacing_row
    off 175 double  x2 (0), off 191 int32
    off 195 int32   ?
    off 199 int32   image_block_len (= img_hdr + 8 + ncol*nrow*2)
    off 203 int32   image_block_hdr
    off 207 int32   ncols
    off 211 int32   nrows
    off 215 int32   plane index (cine: 0/1 for the two interleaved planes)
    ...
    last ncols*nrows*2 bytes: uint16 pixels, row major (nrows, ncols)

Every cine frame (0,1,3) is preceded by a 10-byte notice (0,1,10):
    uint64 acquisition clock (ms), uint16 plane index
This clock is used for cine timing. The record timestamps are NOT reliable for it:
re-exported dumps carry the export time, with the whole file squeezed into a fraction
of a second. Where the record clock is real, the ms clock matches it to ~1 ms.

Tracking-result messages (A3i (1,2,12), pre-A3i (1,2,20)) embed the same image block
plus (A3i) a target mask/contour block; contour points are stored as
[int32 12][float32 x,y,z] triplets.

Outputs (in --out):
    records_index.csv          every record: offset, flag, category, type, timestamp, size
    text_messages.jsonl        every JSON / XML / plain-text payload
    volumes/volume_XX_*.mha    3D volumes (uint16) + volumes.csv
    cine/cine_planeN_*.mha     cine frames stacked over time, one file per plane + cine_frames.csv
    tracking/contours.npz      tracked contour points per frame (A3i)
    previews/*.png             quick-look images
"""
import argparse
import csv
import json
import os
import struct
from collections import Counter, defaultdict

import numpy as np

REC_HDR = struct.Struct('<IIBBHQI')  # len, hdr_size, flag, cat, type, ts, payload_len
GEOM_SIZE = 195

MSG_NAMES = {
    (0, 255, 0): 'version',
    (0, 1, 0): 'mr_scan_config_xml',
    (0, 1, 3): 'cine_frame',
    (0, 1, 4): 'volume_slice',
    (0, 1, 10): 'cine_frame_notice',
    (1, 2, 12): 'tracking_result_a3i',
    (1, 2, 20): 'tracking_result_pre_a3i',
    (1, 2, 25): 'volume_slice_processed_pre_a3i',
    (1, 3, 22): 'heartbeat',
    (0, 3, 23): 'heartbeat_ack',
}


# --------------------------------------------------------------------------- parsing
def iter_records(raw):
    p, n = 0, len(raw)
    while p + REC_HDR.size <= n:
        L, hs, flag, cat, mtype, ts, plen = REC_HDR.unpack_from(raw, p)
        if hs != 12 or plen != L - 20 or p + 4 + L > n:
            raise ValueError(f'Corrupt record at offset {p}: len={L} hdr={hs} payload={plen}')
        yield p, (flag, cat, mtype), ts, raw[p + 24:p + 4 + L]
        p += 4 + L
    if p != n:
        print(f'WARNING: {n - p} trailing bytes not parsed')


def parse_image(payload):
    """Decode a geometry-header image payload -> (meta dict, 2D uint16 array)."""
    hsize = struct.unpack_from('<i', payload, 0)[0]
    if hsize != GEOM_SIZE:
        raise ValueError(f'unexpected geometry header size {hsize}')
    d = struct.unpack_from('<21d', payload, 7)
    ncol, nrow, plane = struct.unpack_from('<3i', payload, 207)
    img = np.frombuffer(payload[len(payload) - ncol * nrow * 2:], '<u2').reshape(nrow, ncol)
    meta = dict(
        is3d=payload[5], P1=np.array(d[0:3]), P2=np.array(d[3:6]), normal=np.array(d[6:9]),
        aux=np.array(d[9:12]), row_dir=np.array(d[12:15]), col_dir=np.array(d[15:18]),
        thickness=d[18], sp_col=d[19], sp_row=d[20], ncol=ncol, nrow=nrow, plane=plane,
    )
    return meta, img


def parse_contours(blob):
    """Find runs of [int32 12][3 x float32] in a byte blob -> list of (N,3) arrays."""
    out, i, n = [], 0, len(blob)
    while i + 16 <= n:
        if struct.unpack_from('<i', blob, i)[0] == 12:
            pts, j = [], i
            while j + 16 <= n and struct.unpack_from('<i', blob, j)[0] == 12:
                pts.append(struct.unpack_from('<3f', blob, j + 4))
                j += 16
            if len(pts) >= 3:
                out.append(np.array(pts, np.float32))
                i = j
                continue
        i += 1
    return out


def text_of(payload):
    """Return payload as text if it is (mostly) JSON / XML / plain text."""
    for skip in (0, 4):  # some XML payloads have an extra int32 length prefix
        s = payload[skip:]
        if s[:1] in (b'{', b'<', b'[') or (s[:8].isascii() and s[:8].strip().isalnum()):
            try:
                t = s.decode('utf-8')
            except UnicodeDecodeError:
                continue
            if sum(c.isprintable() or c in '\r\n\t' for c in t) > 0.95 * len(t):
                return t
    return None


# --------------------------------------------------------------------------- writers
def pixel0_origin(m):
    """Centre of pixel (0,0): P2 is the field-of-view centre."""
    return (m['P2'] - (m['ncol'] / 2 - 0.5) * m['sp_col'] * m['row_dir']
            - (m['nrow'] / 2 - 0.5) * m['sp_row'] * m['col_dir'])


def write_mha(path, arr, spacing, origin, direction_cols):
    """arr: (nz, ny, nx) uint16; direction_cols: [x_axis, y_axis, z_axis] unit vectors."""
    arr = np.ascontiguousarray(arr, '<u2')
    nz, ny, nx = arr.shape
    tm = ' '.join(f'{v:.6g}' for axis in direction_cols for v in axis)
    hdr = (
        'ObjectType = Image\nNDims = 3\nBinaryData = True\nBinaryDataByteOrderMSB = False\n'
        'CompressedData = False\n'
        f'TransformMatrix = {tm}\n'
        f'Offset = {origin[0]:.6g} {origin[1]:.6g} {origin[2]:.6g}\n'
        'CenterOfRotation = 0 0 0\nAnatomicalOrientation = RAI\n'
        f'ElementSpacing = {spacing[0]:.6g} {spacing[1]:.6g} {spacing[2]:.6g}\n'
        f'DimSize = {nx} {ny} {nz}\nElementType = MET_USHORT\nElementDataFile = LOCAL\n'
    )
    with open(path, 'wb') as f:
        f.write(hdr.encode())
        f.write(arr.tobytes())


def save_preview(path, images, titles, suptitle):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig, axs = plt.subplots(1, len(images), figsize=(4 * len(images), 4.2), squeeze=False)
    for ax, im, t in zip(axs[0], images, titles):
        ax.imshow(im, cmap='gray', vmin=0, vmax=np.percentile(im, 99.5) or 1, aspect='equal')
        ax.set_title(t, fontsize=9)
        ax.axis('off')
    fig.suptitle(suptitle, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


# --------------------------------------------------------------------------- main
def convert(path_in, out_dir, previews=True):
    os.makedirs(out_dir, exist_ok=True)
    raw = open(path_in, 'rb').read()
    print(f'Reading {path_in} ({len(raw) / 1e6:.1f} MB)')

    counts = Counter()
    vol_slices = []          # (ts, meta, img, scan_id)
    cine = defaultdict(list)  # plane -> [(ts, meta, img)]
    contours = []            # (ts, [arrays])
    scan_id = 0
    pending_tick = None      # acquisition clock (ms) from the last (0,1,10) notice

    with open(os.path.join(out_dir, 'records_index.csv'), 'w', newline='') as fidx, \
            open(os.path.join(out_dir, 'text_messages.jsonl'), 'w') as ftxt:
        widx = csv.writer(fidx)
        widx.writerow(['offset', 'flag', 'category', 'type', 'name', 'timestamp_us', 'payload_len'])
        for off, key, ts, pl in iter_records(raw):
            counts[key] += 1
            name = MSG_NAMES.get(key, '')
            widx.writerow([off, *key, name, ts, len(pl)])

            if key == (0, 1, 0):          # new MR scan configuration -> new scan
                scan_id += 1
            if key == (0, 1, 4):
                meta, img = parse_image(pl)
                vol_slices.append((ts, meta, img, scan_id))
            elif key == (0, 1, 10) and len(pl) >= 10:
                pending_tick = struct.unpack_from('<Q', pl, 0)[0]
            elif key == (0, 1, 3):
                meta, img = parse_image(pl)
                # acquisition time in µs: ms clock if available, else record timestamp
                acq_us = pending_tick * 1000 if pending_tick is not None else ts
                meta['record_ts'] = ts
                pending_tick = None
                cine[meta['plane']].append((acq_us, meta, img))
            elif key == (1, 2, 12) and len(pl) > 16:
                blk = struct.unpack_from('<i', pl, 12)[0]
                cs = parse_contours(pl[16 + blk:])
                if cs:
                    contours.append((ts, cs))
            elif key not in ((1, 2, 20), (1, 2, 25), (0, 2, 5), (0, 2, 11), (1, 2, 26)):
                t = text_of(pl) if len(pl) > 2 else None
                if t is not None:
                    ftxt.write(json.dumps({'offset': off, 'type': list(key), 'name': name,
                                           'timestamp_us': ts, 'text': t}) + '\n')

    print(f'  {sum(counts.values())} records, {len(counts)} message types')

    # ---------------- volumes: split on scan change, size change or broken slice step
    volumes, cur = [], []
    for s in vol_slices:
        if cur:
            m0, m1 = cur[-1][1], s[1]
            step = float(np.dot(m1['P2'] - m0['P2'], m0['normal']))
            same = (s[3] == cur[-1][3] and (m0['ncol'], m0['nrow']) == (m1['ncol'], m1['nrow'])
                    and np.allclose(m0['normal'], m1['normal']) and step > 0
                    and (len(cur) < 2 or abs(step - cur_step) < 1e-3))
            if not same:
                volumes.append(cur)
                cur = []
            else:
                cur_step = step
        cur.append(s)
    if cur:
        volumes.append(cur)

    vdir = os.path.join(out_dir, 'volumes')
    os.makedirs(vdir, exist_ok=True)
    with open(os.path.join(vdir, 'volumes.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['file', 'scan', 'ncol', 'nrow', 'nslice', 'sp_col', 'sp_row', 'sp_slice',
                    'origin_x', 'origin_y', 'origin_z', 'row_dir', 'col_dir', 'normal',
                    'first_timestamp_us'])
        for i, v in enumerate(volumes):
            m = v[0][1]
            arr = np.stack([s[2] for s in v])
            sp_z = float(np.dot(v[1][1]['P2'] - m['P2'], m['normal'])) if len(v) > 1 else m['thickness']
            fn = f'volume_{i:02d}_{m["ncol"]}x{m["nrow"]}x{len(v)}.mha'
            write_mha(os.path.join(vdir, fn), arr, (m['sp_col'], m['sp_row'], sp_z), pixel0_origin(m),
                      [m['row_dir'], m['col_dir'], m['normal']])
            w.writerow([fn, v[0][3], m['ncol'], m['nrow'], len(v), m['sp_col'], m['sp_row'], sp_z,
                        *pixel0_origin(m), m['row_dir'].tolist(), m['col_dir'].tolist(), m['normal'].tolist(),
                        v[0][0]])
            print(f'  volume {fn}  spacing=({m["sp_col"]:.3f},{m["sp_row"]:.3f},{sp_z:.3f}) mm')
            if previews:
                os.makedirs(os.path.join(out_dir, 'previews'), exist_ok=True)
                nz, ny, nx = arr.shape
                save_preview(os.path.join(out_dir, 'previews', fn.replace('.mha', '.png')),
                             [arr[nz // 2], arr[:, ny // 2, :], arr[:, :, nx // 2]],
                             [f'slice {nz // 2}', f'row {ny // 2} (reformat)', f'col {nx // 2} (reformat)'],
                             fn)

    # ---------------- cine
    cdir = os.path.join(out_dir, 'cine')
    os.makedirs(cdir, exist_ok=True)
    with open(os.path.join(cdir, 'cine_frames.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['plane', 'frame', 'acq_clock_us', 'record_timestamp_us',
                    'center_x', 'center_y', 'center_z', 'mean_intensity'])
        for plane in sorted(cine):
            fr = cine[plane]
            for k, (ts, m, img) in enumerate(fr):
                w.writerow([plane, k, ts, m['record_ts'], *np.round(m['P2'], 3),
                            round(float(img.mean()), 2)])
            # one stack per plane and per geometry (FOV may change between runs)
            groups = defaultdict(list)
            for ts, m, img in fr:
                groups[(m['ncol'], m['nrow'], tuple(np.round(m['P2'], 2)))].append((ts, m, img))
            for gi, (g, frames) in enumerate(groups.items()):
                m = frames[0][1]
                arr = np.stack([x[2] for x in frames])
                ts = np.array([x[0] for x in frames])
                dt = np.median(np.diff(ts)) / 1e6 if len(ts) > 1 else 0
                fn = f'cine_plane{plane}_g{gi}_{m["ncol"]}x{m["nrow"]}x{len(frames)}.mha'
                # third axis = time (spacing = median frame interval in s)
                write_mha(os.path.join(cdir, fn), arr, (m['sp_col'], m['sp_row'], dt or 1), pixel0_origin(m),
                          [m['row_dir'], m['col_dir'], m['normal']])
                np.save(os.path.join(cdir, fn.replace('.mha', '_timestamps_us.npy')), ts)
                orient = {(1, 0, 0): 'sagittal', (0, 1, 0): 'coronal', (0, 0, 1): 'axial'}.get(
                    tuple(np.abs(np.round(m['normal'])).astype(int)), 'oblique')
                print(f'  cine {fn}  {orient}, {1 / dt if dt else 0:.2f} fps/plane, '
                      f'{m["sp_col"]:.3f} mm px, thickness {m["thickness"]} mm')
                if previews:
                    idx = np.linspace(0, len(arr) - 1, 4).astype(int)
                    save_preview(os.path.join(out_dir, 'previews', fn.replace('.mha', '.png')),
                                 [arr[i] for i in idx],
                                 [f'frame {i}, t={(ts[i] - ts[0]) / 1e6:.1f}s' for i in idx],
                                 f'{fn} ({orient})')

    # ---------------- tracking contours (A3i)
    if contours:
        tdir = os.path.join(out_dir, 'tracking')
        os.makedirs(tdir, exist_ok=True)
        np.savez_compressed(os.path.join(tdir, 'contours.npz'),
                            timestamps_us=np.array([c[0] for c in contours]),
                            **{f'frame_{i:05d}_contour_{j}': a
                               for i, (_, cs) in enumerate(contours) for j, a in enumerate(cs)})
        print(f'  tracking contours: {len(contours)} frames, '
              f'{Counter(len(a) for _, cs in contours for a in cs).most_common(3)} points/contour')

    with open(os.path.join(out_dir, 'message_type_counts.json'), 'w') as f:
        json.dump({f'{k}': {'name': MSG_NAMES.get(k, ''), 'count': c} for k, c in sorted(counts.items())},
                  f, indent=2)
    print(f'  outputs written to {out_dir}')


def main(argv=None, prog=None):
    ap = argparse.ArgumentParser(prog=prog, description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('dump', help='path to Message.dump')
    ap.add_argument('--out', help='output folder (default: <dump dir>/output_conversion)')
    ap.add_argument('--no-previews', action='store_true')
    a = ap.parse_args(argv)
    convert(a.dump, a.out or os.path.join(os.path.dirname(os.path.abspath(a.dump)), 'output_conversion'),
            previews=not a.no_previews)


if __name__ == '__main__':
    main()
