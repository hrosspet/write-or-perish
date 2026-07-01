"""Recover a streaming-recording batch whose WebM container reports a shorter
duration than the audio data actually present in the file.

Background: MediaRecorder with a timeslice emits Matroska *fragments*, not
standalone WebM files. The frontend (useStreamingMediaRecorder.js) prepends
chunk 0's header to each subsequent chunk to pass basic parsing, then the
backend concatenates them via `ffmpeg -f concat -c copy`. The result is a file
whose bytes contain all the audio clusters, but whose SegmentInfo.Duration and
cluster timestamps only describe the first chunk. Playback and Whisper both
stop at ~15 s.

This script takes a `batch_XXXX-YYYY.webm.enc` file, decrypts it, then tries a
sequence of ffmpeg recipes that may walk past the broken metadata and expose
the buried audio. Each attempt is probed and reported; any that yield audio
longer than the declared input duration are kept.

Usage (on the production VM, from the repo root):

    python -m backend.scripts.recover_audio_batch /path/to/batch_0000-0013.webm.enc

Requires `ffmpeg` and `ffprobe` in PATH, plus the backend's normal env
(GCP_KMS_KEY_NAME, credentials) so decrypt_file() can unwrap the DEK.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile


EBML_MAGIC = b'\x1a\x45\xdf\xa3'       # EBML header element ID
SEGMENT_MAGIC = b'\x18\x53\x80\x67'    # Matroska Segment element ID
CLUSTER_MAGIC = b'\x1f\x43\xb6\x75'    # Matroska Cluster element ID


def find_all(data: bytes, magic: bytes):
    positions = []
    off = 0
    while True:
        i = data.find(magic, off)
        if i < 0:
            break
        positions.append(i)
        off = i + len(magic)
    return positions


def diagnose(decrypted_path: str):
    """Scan the decrypted file for Matroska structural elements and print a
    summary. Multiple EBML/Segment headers are the smoking gun for naive
    binary concatenation — ffmpeg stops reading after the first segment."""
    with open(decrypted_path, 'rb') as f:
        data = f.read()
    ebml = find_all(data, EBML_MAGIC)
    segs = find_all(data, SEGMENT_MAGIC)
    clusters = find_all(data, CLUSTER_MAGIC)
    print('\n--- diagnostic ---')
    print(f'  file size: {len(data)} bytes')
    print(f'  EBML headers found: {len(ebml)} at {ebml[:20]}'
          f'{"..." if len(ebml) > 20 else ""}')
    print(f'  Segment elements:   {len(segs)} at {segs[:20]}'
          f'{"..." if len(segs) > 20 else ""}')
    print(f'  Cluster elements:   {len(clusters)}'
          f'{" (first 20: " + str(clusters[:20]) + ")" if clusters else ""}')
    if len(ebml) > 1:
        print('  >>> multiple EBML headers — file is chained segments; '
              'Recipe E will split and recover each independently.')
    return ebml


def ffprobe_duration(path: str):
    try:
        out = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_format', '-show_streams',
             '-of', 'json', path],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return None, out.stderr.strip()[:300]
        data = json.loads(out.stdout)
        dur = data.get('format', {}).get('duration')
        if dur is None and data.get('streams'):
            dur = data['streams'][0].get('duration')
        return float(dur) if dur else None, None
    except Exception as e:
        return None, str(e)


def try_recipe(name: str, cmd: list, out_path: str):
    print(f'\n=== {name} ===')
    print('  $ ' + ' '.join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        print(f'  ffmpeg failed (rc={result.returncode})')
        # print last 20 lines of stderr
        tail = '\n'.join(result.stderr.strip().splitlines()[-20:])
        print('  stderr tail:')
        for line in tail.splitlines():
            print(f'    {line}')
        return None
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        print('  output missing or empty')
        return None
    dur, err = ffprobe_duration(out_path)
    size = os.path.getsize(out_path)
    if dur is None:
        print(f'  ffprobe could not read duration ({err}); size={size}')
        return None
    print(f'  OK: duration={dur:.2f}s, size={size} bytes -> {out_path}')
    return dur


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)

    enc_path = sys.argv[1]
    if not os.path.isfile(enc_path):
        print(f'Not a file: {enc_path}')
        sys.exit(1)

    # Lazy import so --help works without backend env configured
    from backend.utils.encryption import decrypt_file, is_encryption_enabled

    if not is_encryption_enabled():
        print('WARNING: ENCRYPTION is not enabled in this env. The input may '
              'not actually be encrypted, or KMS credentials are missing.')

    workdir = tempfile.mkdtemp(prefix='recover_batch_')
    print(f'Working in: {workdir}')

    # 1. Decrypt
    decrypted_path = os.path.join(workdir, 'input.webm')
    if enc_path.endswith('.enc'):
        print(f'Decrypting {enc_path} ...')
        plaintext = decrypt_file(enc_path)
        with open(decrypted_path, 'wb') as f:
            f.write(plaintext)
    else:
        shutil.copy2(enc_path, decrypted_path)

    input_size = os.path.getsize(decrypted_path)
    input_dur, err = ffprobe_duration(decrypted_path)
    print(f'\nDecrypted input: size={input_size} bytes, '
          f'ffprobe duration={input_dur}s (err={err})')

    ebml_positions = diagnose(decrypted_path)

    if input_dur is None:
        print('WARNING: ffprobe cannot read input at all. Recovery may still '
              'work via force-decode recipes below.')

    results = []

    # Recipe A: remux with regenerated timestamps (cheap, often enough)
    out_a = os.path.join(workdir, 'A_remux_genpts.webm')
    dur_a = try_recipe(
        'A: remux with genpts (-c copy)',
        ['ffmpeg', '-y', '-fflags', '+genpts',
         '-i', decrypted_path, '-c', 'copy', out_a],
        out_a,
    )
    if dur_a is not None:
        results.append((dur_a, out_a, 'A_remux_genpts'))

    # Recipe B: ignore errors + re-encode. Forces ffmpeg to decode every
    # cluster regardless of what the header claims, then repackage from scratch.
    out_b = os.path.join(workdir, 'B_reencode_ignore_err.webm')
    dur_b = try_recipe(
        'B: re-encode with ignore_err + large probe',
        ['ffmpeg', '-y',
         '-err_detect', 'ignore_err',
         '-fflags', '+genpts+igndts+discardcorrupt',
         '-analyzeduration', '200M', '-probesize', '200M',
         '-i', decrypted_path,
         '-c:a', 'libopus', '-b:a', '64k', out_b],
        out_b,
    )
    if dur_b is not None:
        results.append((dur_b, out_b, 'B_reencode_ignore_err'))

    # Recipe C: pipe through raw PCM. If any recipe can expose buried audio,
    # it's this one — ffmpeg decodes what it can and hands us the samples,
    # sidestepping every container-level duration field.
    out_c_wav = os.path.join(workdir, 'C_raw.wav')
    dur_c_wav = try_recipe(
        'C1: decode to WAV (force through, ignore container duration)',
        ['ffmpeg', '-y',
         '-err_detect', 'ignore_err',
         '-fflags', '+genpts+igndts+discardcorrupt',
         '-analyzeduration', '200M', '-probesize', '200M',
         '-i', decrypted_path,
         '-vn', '-ac', '1', '-ar', '48000', out_c_wav],
        out_c_wav,
    )
    if dur_c_wav is not None:
        out_c = os.path.join(workdir, 'C_wav_to_opus.webm')
        dur_c = try_recipe(
            'C2: re-encode the recovered WAV to opus/webm',
            ['ffmpeg', '-y', '-i', out_c_wav,
             '-c:a', 'libopus', '-b:a', '64k', out_c],
            out_c,
        )
        if dur_c is not None:
            results.append((dur_c, out_c, 'C_wav_roundtrip'))

    # Recipe E: split at EBML-header boundaries and recover each independently.
    # If the file is really 14 chained Matroska segments (naive concat output),
    # ffmpeg only reads the first one. By splitting on EBML magic, we can feed
    # each slice to ffmpeg separately, decode to PCM, and concat the samples.
    if len(ebml_positions) > 1:
        print(f'\n=== E: split at {len(ebml_positions)} EBML headers and '
              'recover each ===')
        with open(decrypted_path, 'rb') as f:
            raw = f.read()
        slice_dir = os.path.join(workdir, 'E_slices')
        os.makedirs(slice_dir, exist_ok=True)
        wav_paths = []
        for idx, start in enumerate(ebml_positions):
            end = ebml_positions[idx + 1] if idx + 1 < len(ebml_positions) else len(raw)
            slice_webm = os.path.join(slice_dir, f'slice_{idx:04d}.webm')
            with open(slice_webm, 'wb') as f:
                f.write(raw[start:end])
            slice_wav = os.path.join(slice_dir, f'slice_{idx:04d}.wav')
            r = subprocess.run(
                ['ffmpeg', '-y',
                 '-err_detect', 'ignore_err',
                 '-fflags', '+genpts+igndts+discardcorrupt',
                 '-i', slice_webm,
                 '-vn', '-ac', '1', '-ar', '48000', slice_wav],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0 and os.path.exists(slice_wav) \
                    and os.path.getsize(slice_wav) > 0:
                dur, _ = ffprobe_duration(slice_wav)
                print(f'  slice {idx:04d}: {end - start:6d} bytes in, '
                      f'{os.path.getsize(slice_wav):7d} bytes wav, '
                      f'{dur}s audio')
                if dur and dur > 0:
                    wav_paths.append(slice_wav)
            else:
                tail = '\n    '.join(r.stderr.strip().splitlines()[-5:])
                print(f'  slice {idx:04d}: decode FAILED\n    {tail}')

        if wav_paths:
            # Concat all WAV slices via ffmpeg concat demuxer (safe for PCM)
            concat_list = os.path.join(slice_dir, 'concat.txt')
            with open(concat_list, 'w') as f:
                for p in wav_paths:
                    esc = p.replace("'", "'\\''")
                    f.write(f"file '{esc}'\n")
            out_e_wav = os.path.join(workdir, 'E_concat.wav')
            r = subprocess.run(
                ['ffmpeg', '-y', '-f', 'concat', '-safe', '0',
                 '-i', concat_list, '-c', 'copy', out_e_wav],
                capture_output=True, text=True, timeout=120,
            )
            if r.returncode == 0:
                out_e = os.path.join(workdir, 'E_recovered.webm')
                dur_e = try_recipe(
                    'E: concat PCM slices -> opus/webm',
                    ['ffmpeg', '-y', '-i', out_e_wav,
                     '-c:a', 'libopus', '-b:a', '64k', out_e],
                    out_e,
                )
                if dur_e is not None:
                    results.append((dur_e, out_e, 'E_split_and_merge'))
            else:
                print('  PCM concat failed:')
                print('   ', r.stderr.strip().splitlines()[-5:])
        else:
            print('  no decodable slices — this file may have corruption '
                  'beyond simple chaining')

    # Recipe D: mkvmerge, if present. Very good at rebuilding broken Matroska.
    if shutil.which('mkvmerge'):
        out_d = os.path.join(workdir, 'D_mkvmerge.webm')
        dur_d = try_recipe(
            'D: mkvmerge rebuild',
            ['mkvmerge', '-o', out_d, decrypted_path],
            out_d,
        )
        if dur_d is not None:
            results.append((dur_d, out_d, 'D_mkvmerge'))
    else:
        print('\n=== D: mkvmerge ===\n  skipped (not installed)')

    # Summary
    print('\n\n' + '=' * 60)
    print('SUMMARY')
    print('=' * 60)
    print(f'Input duration reported by ffprobe: {input_dur}s')
    if not results:
        print('No recipe produced readable output. Consider copying the '
              'input.webm off the VM and trying desktop tools (VLC, '
              'Audacity import-raw, mkvtoolnix GUI).')
    else:
        results.sort(reverse=True)
        for dur, path, name in results:
            flag = ' <-- looks longer than input' if (
                input_dur is not None and dur > input_dur + 1) else ''
            print(f'  {name}: {dur:.2f}s  {path}{flag}')
        best_dur, best_path, best_name = results[0]
        print(f'\nBest candidate: {best_name} ({best_dur:.2f}s)')
        print(f'  {best_path}')

    print(f'\nAll outputs kept in: {workdir}')
    print('Play with: ffplay <path>  or copy off the VM and open in VLC.')


if __name__ == '__main__':
    main()
