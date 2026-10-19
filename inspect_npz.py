#!/usr/bin/env python3
import sys
import os
import glob
import numpy as np

def format_bytes(size):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"

def format_val(val):
    if isinstance(val, (np.floating, float)):
        return f"{val:.4g}"
    elif isinstance(val, (np.complexfloating, complex)):
        return f"{val.real:.3g}{val.imag:+.3g}j"
    return repr(val)

def preview_array(arr, n=3):
    if arr.size == 0:
        return "[]"
    
    flat = arr.flat
    if arr.size <= 2 * n:
        vals = [format_val(x) for x in flat]
        return f"[{', '.join(vals)}]"
    else:
        first = [format_val(x) for x in flat[:n]]
        last = [format_val(x) for x in flat[-n:]]
        return f"[{', '.join(first)}, ..., {', '.join(last)}]"

def print_row(k, arr, num_preview):
    shape_str = str(arr.shape)
    dtype_str = str(arr.dtype)
    mem_str = format_bytes(arr.nbytes)
    prev_str = preview_array(arr, n=num_preview)
    k_display = (k[:17] + '...') if len(k) > 20 else k
    print(f"{k_display:<20} | {shape_str:<16} | {dtype_str:<10} | {mem_str:<9} | {prev_str}")

def inspect_file(filepath, num_preview=3):
    try:
        file_size = format_bytes(os.path.getsize(filepath))
        ext = os.path.splitext(filepath)[1].lower()

        header = f"{'Key / Array':<20} | {'Shape':<16} | {'Dtype':<10} | {'Mem':<9} | {'Preview (First / Last ' + str(num_preview) + ')'}"
        sep = "-" * len(header)

        if ext == '.npy':
            arr = np.load(filepath, allow_pickle=True)
            print(f"\nArray File: {filepath} ({file_size})")
            print(sep)
            print(header)
            print(sep)
            print_row(os.path.basename(filepath), arr, num_preview)
            print(sep + "\n")
        else:
            with np.load(filepath, allow_pickle=True) as data:
                keys = data.files
                if not keys:
                    print(f"\n'{filepath}' is an empty .npz archive.")
                    return

                print(f"\nArchive: {filepath} ({file_size}, {len(keys)} arrays)")
                print(sep)
                print(header)
                print(sep)
                for k in keys:
                    print_row(k, data[k], num_preview)
                print(sep + "\n")

    except PermissionError:
        print(f"Error: Permission denied reading '{filepath}'. Check if the file is locked by another process (e.g., an active acquisition script).")
    except Exception as e:
        print(f"Error reading '{filepath}': {e}")

def inspect_target(target_path, num_preview=3):
    if not os.path.exists(target_path):
        # Fallback if the user omitted the .npz extension
        if os.path.exists(target_path + ".npz"):
            target_path += ".npz"
        else:
            print(f"Error: Path '{target_path}' not found.")
            sys.exit(1)

    if os.path.isdir(target_path):
        files = sorted(
            glob.glob(os.path.join(target_path, "*.npz")) + 
            glob.glob(os.path.join(target_path, "*.npy"))
        )
        if not files:
            print(f"Directory '{target_path}' contains no .npz or .npy files.")
            print("Contents:", os.listdir(target_path))
            return
        print(f"Found {len(files)} NumPy file(s) in directory '{target_path}':")
        for f in files:
            inspect_file(f, num_preview)
    else:
        inspect_file(target_path, num_preview)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <path_to_file_or_dir> [num_preview_items]")
        sys.exit(1)
    
    path = sys.argv[1]
    n_items = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    inspect_target(path, n_items)