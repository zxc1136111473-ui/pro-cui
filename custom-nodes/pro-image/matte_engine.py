"""商品抠图的引擎：本地跑 ONNX 小模型，只依赖 onnxruntime（不装 rembg：它连带 numba / scikit-image 等一大堆，在 4GB 内存的服务器上太占地方）。

预处理 / 后处理和 rembg 对 u2net / isnet 的做法一致（实测输出和 rembg 逐像素相差不超过 1）。模型文件第一次用时从 rembg 的发布页下载到
models/rembg/（环境变量 PRO_MATTE_MODELS 可以换地方），之后不再联网。

推理放在一个单独的小进程里跑（`python matte_engine.py 模型名 模型文件 输入图 输出图`）：模型 + 中间结果要占几百 MB，跑完进程退出就全部还给系统，
常驻的 ComfyUI 进程不会因此变胖；万一内存真不够，被杀的是这个小进程（它给自己调高了被系统优先杀掉的分数），ComfyUI 本身不受影响，节点只报一句人话错误。
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.request

import numpy as np
from PIL import Image

MODEL_BASE = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/"
_U2 = {"size": 320, "mean": (0.485, 0.456, 0.406), "std": (0.229, 0.224, 0.225)}
MATTE_MODELS = {
    "u2net": dict(_U2, file="u2net.onnx", md5="60024c5c889badc19c04ad937298a77b"),
    "u2netp": dict(_U2, file="u2netp.onnx", md5="8e83ca70e441ab06c318d82300c84806"),
    "silueta": dict(_U2, file="silueta.onnx", md5="55e59e0d8062d2f5d013f4725ee84782"),
    "isnet-general-use": {"size": 1024, "mean": (0.5, 0.5, 0.5), "std": (1.0, 1.0, 1.0), "file": "isnet-general-use.onnx", "md5": "fc16ebd8b0c10d971d3513d564d01e29"},
}
WORKER_TIMEOUT = 300       # 秒：两核的小服务器上 isnet 一张约几秒，留够余地
NO_ONNXRUNTIME = 3         # 小进程的退出码：没装 onnxruntime

_checked = set()


def _model_dir():
    d = os.environ.get("PRO_MATTE_MODELS")
    if not d:
        import folder_paths
        d = os.path.join(folder_paths.models_dir, "rembg")
    os.makedirs(d, exist_ok=True)
    return d


def _md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def model_path(name):
    """模型文件的路径；没有就下载（先下到临时文件、校验 md5 再改名，下到一半断了不会留下坏文件）。"""
    spec = MATTE_MODELS[name]
    try:
        folder = _model_dir()
    except OSError as e:
        raise RuntimeError(f"[商品抠图] 模型文件夹建不出来 / 不能写：{e}。可以用环境变量 PRO_MATTE_MODELS 指定一个能写的文件夹") from e
    path = os.path.join(folder, spec["file"])
    if os.path.exists(path):
        if path in _checked or _md5(path) == spec["md5"]:
            _checked.add(path)
            return path
        os.remove(path)                                        # 文件坏了（上次没下完 / 被改过）：重新下
    url = MODEL_BASE + spec["file"]
    tmp = path + ".part"
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        if _md5(tmp) != spec["md5"]:
            raise RuntimeError("下载下来的文件校验不对")
        os.replace(tmp, path)
    except Exception as e:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise RuntimeError(f"[商品抠图] 下载模型失败：{type(e).__name__} {str(e)[:100]}。可以手动下载 {url} 放到 {folder}/ 里再运行") from e
    _checked.add(path)
    return path


def open_session(path):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.enable_cpu_mem_arena = False                            # 不要让它为了「下次更快」一直占着内存：服务器内存紧
    so.enable_mem_pattern = False
    so.intra_op_num_threads = 2
    so.inter_op_num_threads = 1
    return ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])


def predict_alpha(img, spec, session):
    """原图（PIL）→ 模型给出的前景概率图（'L'，和原图一样大）。"""
    side = spec["size"]
    a = np.asarray(img.convert("RGB").resize((side, side), Image.LANCZOS), dtype=np.float32)
    a = a / max(float(a.max()), 1e-6)
    x = ((a - np.array(spec["mean"], np.float32)) / np.array(spec["std"], np.float32)).transpose(2, 0, 1)[None].astype(np.float32)
    out = session.run(None, {session.get_inputs()[0].name: x})[0][:, 0, :, :]
    lo, hi = float(out.min()), float(out.max())
    p = (out[0] - lo) / max(hi - lo, 1e-6)
    return Image.fromarray((p.clip(0, 1) * 255).astype(np.uint8), "L").resize(img.size, Image.LANCZOS)


def run_worker(img, name, timeout=WORKER_TIMEOUT):
    """在单独的小进程里抠一张图（PIL）→ 前景概率图（'L'）。模型文件没有会先下载。"""
    path = model_path(name)
    with tempfile.TemporaryDirectory(prefix="pro_matte_") as d:
        src, dst = os.path.join(d, "in.png"), os.path.join(d, "out.png")
        img.convert("RGB").save(src)
        try:
            r = subprocess.run([sys.executable, os.path.abspath(__file__), name, path, src, dst], capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"[商品抠图] 抠一张图超过 {timeout} 秒还没好，已经停掉了（服务器太忙？换「最省内存」的模型试试）")
        if r.returncode == NO_ONNXRUNTIME:
            raise RuntimeError("[商品抠图] 容器里没有 onnxruntime：Dockerfile 里已经写了，要重建镜像才会装上（bash deploy.sh --update，见 README「商品抠图」）")
        if r.returncode in (-9, 137):
            raise RuntimeError("[商品抠图] 抠图的小进程被系统杀掉了（服务器内存不够）：换「最省内存」的模型，或者先把图缩小一点再抠")
        if r.returncode != 0 or not os.path.exists(dst):
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-1:] or ["没有输出"]
            raise RuntimeError(f"[商品抠图] 抠图失败（退出码 {r.returncode}）：{tail[0][:200]}")
        with Image.open(dst) as m:
            return m.convert("L").copy()


def _worker(argv):
    """小进程：python matte_engine.py 模型名 模型文件 输入图 输出图"""
    name, model_file, src, dst = argv
    try:
        with open("/proc/self/oom_score_adj", "w") as f:       # 内存不够时让系统先杀我，而不是杀 ComfyUI
            f.write("1000")
    except OSError:
        pass
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return NO_ONNXRUNTIME
    with Image.open(src) as im:
        img = im.convert("RGB")
    predict_alpha(img, MATTE_MODELS[name], open_session(model_file)).save(dst)
    return 0


if __name__ == "__main__":
    sys.exit(_worker(sys.argv[1:]))
