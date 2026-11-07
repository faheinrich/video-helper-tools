"""Builds the HDR (HLG, BT.2020, 10 bit) test video with colour gradients; `python tests/hdr_samples.py out.mov`.

Top: slowly moving soft colour gradients. Bottom: two grey ramps (linear and gamma 2.2), where banding shows first.
The result is example_resources/hdr-gradient-hlg.mov (about 240 KB).
"""
import subprocess
import sys


def make_hdr_gradient_clip(path, seconds=6):
    filters = (
        "[0:v]scale=out_color_matrix=bt2020:out_range=tv,format=yuv420p10le[g];"
        "[1:v][2:v]vstack[r];[g][r]vstack,setparams=colorspace=bt2020nc:color_primaries=bt2020:color_trc=arib-std-b67:range=tv"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"gradients=s=1920x720:d={seconds}:r=25:speed=0.04:nb_colors=5:seed=7",
         "-f", "lavfi", "-i", f"nullsrc=s=1920x180:d={seconds}:r=25,format=yuv420p10le,geq=lum='64+876*X/W':cb='512':cr='512'",
         "-f", "lavfi", "-i", f"nullsrc=s=1920x180:d={seconds}:r=25,format=yuv420p10le,geq=lum='64+876*pow(X/W\\,2.2)':cb='512':cr='512'",
         "-filter_complex", filters,
         "-c:v", "libx265", "-crf", "14", "-preset", "medium", "-pix_fmt", "yuv420p10le", "-tag:v", "hvc1",
         "-x265-params", "colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc:range=limited:log-level=error",
         str(path)],
        check=True)
    return path


if __name__ == "__main__":
    make_hdr_gradient_clip(sys.argv[1] if len(sys.argv) > 1 else "hdr-gradient-hlg.mov")
