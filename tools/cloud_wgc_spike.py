"""Phase 1 spike: verify ok-script's WGC stack can capture the cloud NTE window.

Read-only diagnostic for the cloud NTE adaptation (see
docs/zh-CN/development/云异环适配开发设计文档.md, Phase 1). It creates a WGC
session for one HWND using ok-script's rotypes/D3D11 stack without starting the
full ok-script runtime, grabs a few frames, saves the last one, and reports
sizes, latency and content ratios. It never sends input and never changes the
target window.

Usage:
    .venv/Scripts/python.exe tools/cloud_wgc_spike.py [--hwnd 12586332] \
        --output-dir <dir outside this repository>
"""

import argparse
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from tools import cloud_nte_probe as probe


def select_target_hwnd():
    records = probe.collect_windows()
    evaluations = probe.evaluate_windows(records)
    hwnds = probe.select_candidates(evaluations)["hwnds"]
    return hwnds[0] if hwnds else 0


def _capture_frames_impl(hwnd, count, include_cursor):
    from ok.capture.windows import d3d11
    from ok.rotypes.roapi import GetActivationFactory
    from ok.rotypes.Windows.Graphics.Capture import (
        Direct3D11CaptureFramePool,
        IGraphicsCaptureItem,
        IGraphicsCaptureItemInterop,
    )
    from ok.rotypes.Windows.Graphics.DirectX import DirectXPixelFormat
    from ok.rotypes.Windows.Graphics.DirectX.Direct3D11 import (
        CreateDirect3D11DeviceFromDXGIDevice,
        IDirect3DDxgiInterfaceAccess,
    )
    from ok.util.window import WGC_NO_BORDER_MIN_BUILD, WINDOWS_BUILD_NUMBER

    interop = GetActivationFactory("Windows.Graphics.Capture.GraphicsCaptureItem").astype(
        IGraphicsCaptureItemInterop
    )
    dxdevice = d3d11.ID3D11Device()
    immediatedc = d3d11.ID3D11DeviceContext()
    d3d11.D3D11CreateDevice(
        None,
        d3d11.D3D_DRIVER_TYPE_HARDWARE,
        None,
        d3d11.D3D11_CREATE_DEVICE_BGRA_SUPPORT,
        None,
        0,
        d3d11.D3D11_SDK_VERSION,
        ctypes.byref(dxdevice),
        None,
        ctypes.byref(immediatedc),
    )
    rtdevice = CreateDirect3D11DeviceFromDXGIDevice(dxdevice)
    item = interop.CreateForWindow(hwnd, IGraphicsCaptureItem.GUID)
    item_size = item.Size
    frame_pool = Direct3D11CaptureFramePool.CreateFreeThreaded(
        rtdevice, DirectXPixelFormat.B8G8R8A8UIntNormalized, 1, item_size
    )
    session = frame_pool.CreateCaptureSession(item)
    session.IsCursorCaptureEnabled = include_cursor
    border_result = "skipped"
    try:
        if WINDOWS_BUILD_NUMBER >= WGC_NO_BORDER_MIN_BUILD:
            session.IsBorderRequired = False
            border_result = "disabled"
        else:
            border_result = f"unsupported build {WINDOWS_BUILD_NUMBER}"
    except Exception as error:
        border_result = f"failed: {error!r}"
    session.StartCapture()

    frames = []
    cputex = None
    last_size = None
    deadline = time.time() + 15
    try:
        while len(frames) < count and time.time() < deadline:
            try:
                frame = frame_pool.TryGetNextFrame()
            except OSError:
                time.sleep(0.02)
                continue
            if frame is None:
                time.sleep(0.02)
                continue
            processed = False
            try:
                size = frame.ContentSize
                if last_size != (size.Width, size.Height):
                    last_size = (size.Width, size.Height)
                    if cputex is not None:
                        cputex.Release()
                        cputex = None
                tex = frame.Surface.astype(IDirect3DDxgiInterfaceAccess).GetInterface(
                    d3d11.ID3D11Texture2D.GUID
                ).astype(d3d11.ID3D11Texture2D)
                if cputex is None:
                    desc = tex.GetDesc()
                    desc.Usage = d3d11.D3D11_USAGE_STAGING
                    desc.CPUAccessFlags = d3d11.D3D11_CPU_ACCESS_READ
                    desc.BindFlags = 0
                    desc.MiscFlags = 0
                    cputex = dxdevice.CreateTexture2D(ctypes.byref(desc), None)
                immediatedc.CopyResource(cputex, tex)
                mapinfo = immediatedc.Map(cputex, 0, d3d11.D3D11_MAP_READ, 0)
                try:
                    img = np.ctypeslib.as_array(
                        ctypes.cast(mapinfo.pData, ctypes.POINTER(ctypes.c_ubyte)),
                        (last_size[1], mapinfo.RowPitch // 4, 4),
                    )[:, : last_size[0]].copy()
                finally:
                    immediatedc.Unmap(cputex, 0)
                tex.Release()
                frames.append((time.time(), img))
                processed = True
            except ValueError:
                # NULL COM pointer: TryGetNextFrame handed back an empty frame
                # before the pool produced its first real frame. ok-script
                # tolerates this inside its FrameArrived callback.
                pass
            finally:
                try:
                    frame.Close()
                except Exception:
                    pass
            if not processed:
                time.sleep(0.02)
    finally:
        frame_pool.Close()
        session.Close()
        if cputex is not None:
            cputex.Release()
        rtdevice.Release()
        dxdevice.Release()
        immediatedc.Release()
    return frames, (item_size.Width, item_size.Height), border_result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Phase 1 WGC spike for the cloud NTE window (read-only)."
    )
    parser.add_argument(
        "--hwnd", type=lambda value: int(value, 0),
        help="target HWND; default: probe top candidate",
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="directory for saved frames; must stay outside this repository",
    )
    parser.add_argument("--frames", type=int, default=8, help="number of frames to grab")
    args = parser.parse_args(argv)

    hwnd = args.hwnd or select_target_hwnd()
    if not hwnd:
        print("no candidate window found; is the cloud NTE client running?")
        return 1
    import win32gui

    try:
        class_name = win32gui.GetClassName(hwnd)
        title = win32gui.GetWindowText(hwnd)
        client = win32gui.GetClientRect(hwnd)
    except win32gui.error:
        print(f"hwnd {hwnd} no longer exists")
        return 1
    print(f"target hwnd={hwnd} class={class_name!r} title={title!r} client={tuple(client)}")

    output_dir = probe._ensure_untracked_output(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    started = time.time()
    frames, item_size, border_result = _capture_frames_impl(hwnd, max(1, args.frames), False)
    elapsed = time.time() - started
    if not frames:
        print("WGC produced no frames within 15 s")
        return 1

    print(f"item.Size={item_size} border_required={border_result}")
    fps = len(frames) / elapsed if elapsed else 0.0
    print(f"frames: {len(frames)} in {elapsed:.2f}s ({fps:.1f} fps avg incl. startup)")
    for index, (timestamp, img) in enumerate(frames):
        height, width = img.shape[:2]
        colored = int((img[:, :, :3].any(axis=2)).sum())
        ratio = colored / (width * height)
        print(f"  frame {index}: {width}x{height} content_ratio={ratio:.4f}")

    last_time, last_img = frames[-1]
    height, width = last_img.shape[:2]
    path = os.path.join(output_dir, f"wgc_hwnd_{hwnd}.png")
    import cv2

    cv2.imwrite(path, last_img)
    print(f"saved last frame: {path}")
    window_client = tuple(client)
    match = "MATCH" if (width, height) == window_client[2:] else "MISMATCH vs client"
    print(f"frame {width}x{height} vs client {window_client[2:]}: {match}")
    print("WGC capture: OK, orientation must be verified by human inspection of the saved frame")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
